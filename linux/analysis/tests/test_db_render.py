"""Database findings and the Databases section in the Markdown, HTML and JSON reports (spec 12.2, 12.4, 12.5).

Scenarios come from synth_db.py (database runs) and synth.py (the operating-system run). The
"no database" tests use the storage incident of test_report.py: the output of a host without
databases must not mention databases at all.
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    __import__("matplotlib")
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

from perfdiag import analysis, bundle, cli, findings, loader, report, report_common  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from helpers import read_text  # noqa: E402
from synth import Synth  # noqa: E402
from synth_db import DbSynth, host_hardware, write_pidstat  # noqa: E402
from test_report import storage_incident  # noqa: E402

SAMPLES = 120
WINDOW = (20, 100)
REMOTE_NOTE = ("the database server is remote (dbhost.example.com:5432); the OS data in this bundle is from db01, "
               "the collecting machine, and is not used as evidence")
# The top-level keys of findings.json before schema 2 added `databases`.
SCHEMA_1_KEYS = {"schema", "host", "window", "settings", "episodes", "transients", "hypotheses", "time_above", "notes"}
SCHEMA_1_HYPOTHESIS_KEYS = {
    "id", "title", "subject", "confidence", "origin", "primary", "supporting", "contradicting", "other_primary",
    "related", "recurring", "supporting_facts", "contradicting_facts", "kernel_events", "top_processes", "next_steps"}


def busy(value, quiet=0.0, window=WINDOW):
    """A per-sample list: `value` inside the window, `quiet` outside."""
    return [value if window[0] <= index < window[1] else quiet for index in range(SAMPLES)]


def section_between(text, start, end):
    """The part of a report from the `start` marker up to the `end` marker."""
    first = text.index(start)
    return text[first:text.index(end, first)]


class RenderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-dbrender-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "out")
        self.db = DbSynth(self.tmp)

    def os_host(self, **options):
        """An OS run with one NVMe disk that holds /var/lib/pgsql, /u01 and /var/opt/mssql."""
        synth = Synth(self.tmp, **options)
        synth.add_disk("nvme1n1")
        synth.hardware = host_hardware({"nvme1n1": "nvme"}, {
            "/var/lib/pgsql": "nvme1n1", "/var/lib/mysql": "nvme1n1", "/u01/oradata": "nvme1n1",
            "/var/opt/mssql": "nvme1n1"})
        return synth

    def analyse(self):
        settings = Settings()
        settings.set("analysis.sustain_s", "10", "test")
        settings.set("analysis.gap_samples", "2", "test")
        with bundle.open_bundle(self.tmp) as opened:
            host = loader.load_host(opened, "db01", settings=settings)
            return analysis.run_analysis(host, settings)

    def write_reports(self, result, fmt="both"):
        report.write_reports([result], self.out, fmt)
        texts = {}
        for name in ("report.md", "report.html", "findings.json"):
            path = os.path.join(self.out, name)
            if os.path.exists(path):
                texts[name] = read_text(path, encoding="utf-8")
        return texts

    def of(self, result, code, subject=None):
        found = [h for h in result.hypotheses if h.id == code and (subject is None or h.subject == subject)]
        return found[0] if found else None


# ---------------------------------------------------------------------------
# A host without databases is untouched
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class NoDatabaseTests(RenderCase):
    def setUp(self):
        super().setUp()
        storage_incident(self.tmp)
        self.result = self.analyse()
        self.texts = self.write_reports(self.result)

    def test_markdown_html_and_text_do_not_mention_a_database(self):
        for name in ("report.md", "report.html"):
            self.assertNotRegex(self.texts[name], r"(?i)database", name)
            self.assertNotIn("See also:", self.texts[name], name)
        self.assertNotRegex(findings.render_text(self.result), r"(?i)database")

    def test_no_databases_fact_and_no_section_link(self):
        self.assertNotIn("Databases", [key for key, _ in report_common.header_facts(self.result)])
        self.assertNotIn('href="#databases"', self.texts["report.html"])
        self.assertNotIn('id="databases"', self.texts["report.html"])

    def test_coverage_lines_are_the_host_lines_only(self):
        self.assertEqual(report_common.database_coverage_lines(self.result), [])

    def test_json_differs_only_in_the_schema_and_the_empty_additions(self):
        document = json.loads(self.texts["findings.json"])
        self.assertEqual(document["schema"], 2)
        self.assertEqual(document["databases"], [])
        self.assertEqual(set(document), SCHEMA_1_KEYS | {"databases"})
        self.assertTrue(document["hypotheses"])
        for hypothesis in document["hypotheses"]:
            self.assertEqual(set(hypothesis), SCHEMA_1_HYPOTHESIS_KEYS | {"database_context", "links"})
            self.assertEqual(hypothesis["database_context"], [])
            self.assertEqual(hypothesis["links"], [])

    def test_the_charts_of_a_host_finding_are_the_same_as_before(self):
        catalogue = report_common.catalogue_for(self.result)
        h1 = self.of(self.result, "H1")
        self.assertEqual(report_common.charts_for(self.result, h1, catalogue),
                         ["disk_latency_vda", "disk_throughput_vda", "disk_iops_vda", "psi"])


# ---------------------------------------------------------------------------
# The Databases section, one scenario per engine
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class DatabasesSectionTests(RenderCase):
    def postgresql(self):
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data")
        run.write()

    def mysql(self):
        run = self.db.mysql(data_directory="/var/lib/mysql", with_file_io=False)
        run.write()

    def oracle(self):
        run = self.db.oracle(datafile_directory="/u01/oradata")
        run.write()

    def mssql(self):
        run = self.db.mssql(extra_manifest={"path_namespace": "container"},
                            paths={"data": ["/var/opt/mssql/data"], "log": ["/var/opt/mssql/data"]})
        run.write()

    # engine -> (builder name, label, the part of this engine's coverage line that is free of markup, a recorded setting)
    ENGINES = {
        "postgresql": ("postgresql", "PostgreSQL 16.4 (localhost:5432)",
                       "PostgreSQL pg:5432: wait events are sampled every 1 s, not averaged", "track_io_timing"),
        "mysql": ("mysql", "MySQL 8.0.36 (localhost:3306)",
                  "MySQL my:3306: performance_schema is off, so InnoDB file IO latency was not measured.",
                  "max_connections"),
        "oracle": ("oracle", "Oracle 19.3.0.0.0 (ORCL/PDB1)",
                   "Oracle ORCL/PDB1: connected to a PDB; instance-wide figures need a CDB$ROOT connection.",
                   "db_block_size"),
        "mssql": ("mssql", "SQL Server 16.0.4125.3 (MSSQLSERVER)",
                  "SQL Server MSSQLSERVER: file paths are inside the container and were mapped to host devices",
                  "max_workers_count"),
    }

    def test_every_engine_gets_a_databases_section_in_markdown_and_html(self):
        for engine, (builder, label, coverage_line, setting) in self.ENGINES.items():
            with self.subTest(engine=engine):
                shutil.rmtree(self.tmp)
                os.makedirs(self.tmp)
                self.os_host().write()
                getattr(self, builder)()
                texts = self.write_reports(self.analyse())
                markdown, page = texts["report.md"], texts["report.html"]
                # The header fact, and the section between Findings and Transient events.
                self.assertIn("- **Databases:** " + label, markdown)
                self.assertIn("<dt>Databases</dt><dd>%s</dd>" % label, page)
                self.assertLess(markdown.index("## Findings"), markdown.index("## Databases"))
                self.assertLess(markdown.index("## Databases"), markdown.index("## Transient events"))
                self.assertLess(page.index('<h2 id="findings">'), page.index('<h2 id="databases">'))
                self.assertLess(page.index('<h2 id="databases">'), page.index('<h2 id="transients">'))
                self.assertIn('<a href="#databases">Databases</a>', page)
                databases = section_between(markdown, "## Databases", "## Transient events")
                database_page = section_between(page, '<h2 id="databases">', '<h2 id="transients">')
                self.assertIn("### " + label, databases)
                self.assertIn("**Engine and version:**", databases)
                # File placement on the NVMe device, activity summary, recorded settings, coverage.
                self.assertIn("| Role | Path | Device | Media class |", databases)
                self.assertIn("| nvme1n1 | nvme |", databases)
                self.assertIn("<th>Media class</th>", database_page)
                self.assertIn("<td>nvme1n1</td><td>nvme</td>", database_page)
                self.assertIn("| Metric | Basis | Median | p95 |", databases)
                self.assertIn("<h4>Activity summary</h4>", database_page)
                self.assertIn("**Recorded settings**", databases)
                self.assertIn("| %s |" % setting, databases)
                self.assertIn("<td>%s</td>" % setting, database_page)
                self.assertIn("- " + coverage_line, databases)
                self.assertIn("<li>%s" % coverage_line, database_page)

    def test_the_engine_coverage_lines_reach_the_data_coverage_section_too(self):
        self.os_host().write()
        self.oracle()
        result = self.analyse()
        line = "Oracle ORCL/PDB1: connected to a PDB; instance-wide figures need a CDB$ROOT connection."
        self.assertIn(line, report_common.coverage_lines(result))
        markdown = self.write_reports(result)["report.md"]
        self.assertIn("- " + line, section_between(markdown, "## Data coverage and limits", "## Analysis settings"))

    def test_the_2_0_0_sentence_appears_once_per_list(self):
        self.os_host().write()
        self.db.postgresql_v2().write()
        result = self.analyse()
        coverage = report_common.coverage_lines(result)
        sentence = [line for line in coverage if "Re-run with 3.0.0." in line]
        self.assertEqual(len(sentence), 1, coverage)
        self.assertTrue(sentence[0].startswith("PostgreSQL pg:5432: collected with collect-postgresql-metrics.sh 2.0.0."))
        markdown = self.write_reports(result)["report.md"]
        self.assertEqual(markdown.count("Re-run with 3.0.0."), 2)          # the coverage section and the Databases section
        self.assertNotRegex(markdown, r"NOTE: [^\n]*Re-run with 3\.0\.0\.")

    def test_a_process_only_instance_has_a_section_with_its_coverage_line(self):
        synth = self.os_host()
        write_pidstat(synth.write(), [(11, "mysqld", 5.0, 0.0, busy(1024.0))])
        result = self.analyse()
        self.assertEqual([item.source for item in result.host.databases], ["process"])
        texts = self.write_reports(result)
        databases = section_between(texts["report.md"], "## Databases", "## Transient events")
        self.assertIn("mysqld: process data only. Database-level findings need collect-mysql-metrics.sh.", databases)
        self.assertIn("process data only (no collector run)", databases)
        self.assertIn("No activity figures were recorded for this database.", databases)

    def test_the_html_section_has_unique_ids_and_links_back_to_charts_shown_in_findings(self):
        text = CheckpointBurst(self).page()
        ids = re.findall(r'\bid="([^"]+)"', text)
        self.assertEqual(len(ids), len(set(ids)))
        database_page = section_between(text, '<h2 id="databases">', '<h2 id="transients">')
        self.assertIn('<a href="#chart-db_latency_pg_5432">the chart above</a>', database_page)
        self.assertIn('id="chart-db_cache_pg_5432"', database_page)


class CheckpointBurst:
    """The PostgreSQL write burst of test_db_hypotheses: D6 on pg:5432 linked to H1 and H7 on nvme1n1."""

    def __init__(self, case):
        self.case = case
        synth = case.os_host()
        disk = synth.disks["nvme1n1"]
        synth.fill(disk["await"], 20, 100, 40.0)
        synth.fill(disk["tps"], 20, 100, 400.0)
        synth.fill(disk["wkB"], 20, 100, 200000.0)
        synth.fill(synth.psi_io_full, 20, 100, 10.0)
        write_pidstat(synth.write(), [(11, "postgres: checkpointer", 5.0, 0.0, busy(61440.0))])
        run = case.db.postgresql(data_directory="/var/lib/pgsql/16/data", settings={"max_wal_size": "1GB"})
        run.fill("backend_fsyncs_ps", 20, 100, 5.0)
        run.fill("backend_writes_ps", 20, 100, 80.0)
        run.write()
        self.result = case.analyse()
        self.texts = case.write_reports(self.result)

    def page(self):
        return self.texts["report.html"]


# ---------------------------------------------------------------------------
# D findings, H findings with database context, and the JSON
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class LinkedFindingTests(RenderCase):
    def setUp(self):
        super().setUp()
        self.burst = CheckpointBurst(self)
        self.result = self.burst.result
        self.markdown = self.burst.texts["report.md"]
        self.page = self.burst.texts["report.html"]

    def finding_markdown(self, heading):
        """The Findings-section text of one finding, from its heading to the next ### heading."""
        findings_text = section_between(self.markdown, "## Findings", "## Databases")
        start = findings_text.index("### " + heading)
        end = findings_text.find("\n### ", start + 1)
        return findings_text[start:end if end >= 0 else len(findings_text)]

    def finding_html(self, anchor):
        start = self.page.index('<article class="finding" id="%s">' % anchor)
        return self.page[start:self.page.index("</article>", start)]

    def test_the_d_finding_lists_its_links_and_no_process_line(self):
        d6 = self.finding_markdown("D6 ")
        self.assertIn("See also: H1 nvme1n1, H7 nvme1n1.", d6)
        self.assertNotIn("Top IO:", d6)
        self.assertNotIn("Database pg:5432", d6)                      # database lines belong to the host findings
        block = self.finding_html("finding-d6-pg_5432")
        self.assertIn("<p>See also: H1 nvme1n1, H7 nvme1n1.</p>", block)

    def test_the_host_finding_links_back_and_shows_the_database_line_after_the_processes(self):
        line = ("Database pg:5432, PostgreSQL 16.4 (localhost:5432): 51.0 transactions/s; waits cpu 1.00 sessions; "
                "read latency 0.5 ms; 0.4 % of the device's writes; checkpointer 60.0 MiB/s writes.")
        h7 = self.finding_markdown("H7 ")
        self.assertIn(line, h7)
        self.assertIn("See also: D6 pg:5432.", h7)
        self.assertLess(h7.index("Top IO:"), h7.index(line))
        self.assertLess(h7.index(line), h7.index("See also: D6 pg:5432."))
        block = self.finding_html("finding-h7-nvme1n1")
        self.assertIn("<p>%s</p>" % line.replace("'", "&#x27;"), block)
        self.assertLess(block.index("Top IO:"), block.index("Database pg:5432"))
        self.assertIn("<p>See also: D6 pg:5432.</p>", block)

    def test_the_summary_cards_carry_the_same_lines(self):
        summary = section_between(self.markdown, "## Summary", "## Indicator timeline")
        self.assertIn("See also: D6 pg:5432.", summary)
        self.assertIn("Database pg:5432, PostgreSQL 16.4", summary)
        self.assertIn("See also:", self.finding_html("finding-h1-nvme1n1-summary"))

    def test_d_charts_and_the_latency_chart_of_a_linked_host_finding(self):
        catalogue = report_common.catalogue_for(self.result)
        d6 = report_common.charts_for(self.result, self.of(self.result, "D6"), catalogue)
        self.assertTrue(d6)
        self.assertTrue(all(name.startswith("db_") and name.endswith("_pg_5432") for name in d6), d6)
        h7 = report_common.charts_for(self.result, self.of(self.result, "H7"), catalogue)
        self.assertIn("db_latency_pg_5432", h7)
        self.assertEqual(h7[-1], "db_latency_pg_5432")
        self.assertIn("disk_latency_nvme1n1", h7)

    def test_a_chart_is_embedded_once_and_the_png_exists(self):
        self.assertEqual(self.markdown.count("](charts/db_latency_pg_5432.png)"), 1)
        for link in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", self.markdown):
            self.assertTrue(os.path.isfile(os.path.join(self.out, link)), link)
        self.assertEqual(self.page.count('id="chart-db_latency_pg_5432"'), 1)

    def test_the_databases_section_facts_and_header_fact(self):
        databases = section_between(self.markdown, "## Databases", "## Transient events")
        for expected in ("- **Source:** collector run", "- **Location:** on this host (socket)",
                         "- **Collector:** collect-postgresql-metrics.sh 3.0.0, schema 3",
                         "- **Samples:** 120 samples, every 1 s", "- **Resets in window:** none",
                         "| data | /var/lib/pgsql/16/data | nvme1n1 | nvme |",
                         "| Transactions per second | time-averaged | 51.0 /s | 51.0 /s |",
                         "| max_wal_size | 1GB |"):
            self.assertIn(expected, databases)
        self.assertIn("- **Databases:** PostgreSQL 16.4 (localhost:5432)", self.markdown)

    def test_findings_json_database_additions(self):
        document = json.loads(self.burst.texts["findings.json"], parse_constant=self.refuse_constant)
        self.assertEqual(document["schema"], 2)
        self.assertEqual(len(document["databases"]), 1)
        entry = document["databases"][0]
        self.assertEqual(set(entry), {"id", "short", "engine", "label", "source", "local", "version_num",
                                      "collector_schema", "devices", "resets", "settings", "notes"})
        self.assertEqual((entry["id"], entry["short"], entry["engine"], entry["source"], entry["local"]),
                         ("postgresql:localhost:5432", "pg:5432", "postgresql", "collector", True))
        self.assertEqual(entry["version_num"], 160004)
        self.assertEqual(entry["devices"]["data"], ["nvme1n1"])
        self.assertEqual(entry["settings"]["max_wal_size"], "1GB")
        by_code = {h["id"]: h for h in document["hypotheses"]}
        self.assertEqual(by_code["D6"]["links"], [{"code": "H1", "subject": "nvme1n1"},
                                                  {"code": "H7", "subject": "nvme1n1"}])
        self.assertEqual(by_code["D6"]["database_context"], [])
        self.assertEqual(by_code["H7"]["links"], [{"code": "D6", "subject": "pg:5432"}])
        context = by_code["H7"]["database_context"][0]
        self.assertEqual((context["instance"], context["engine"], context["source"]), ("pg:5432", "postgresql", "collector"))
        self.assertAlmostEqual(context["transactions_ps"], 51.0, delta=0.5)
        self.assertEqual(context["waits"], [{"category": "cpu", "sessions": 1.0}])
        self.assertEqual(context["roles"][0]["role"], "checkpointer")
        self.assertEqual(context["roles_by"], "io")
        # The D hypothesis is in the file with its episode, and every episode id it names is listed.
        listed = {episode["id"] for episode in document["episodes"]}
        self.assertIn(by_code["D6"]["primary"], listed)
        self.assertTrue(all(episode_id in listed for episode_id in by_code["D6"]["supporting"]))

    @staticmethod
    def refuse_constant(name):
        raise AssertionError("findings.json holds the non-JSON constant " + name)


# ---------------------------------------------------------------------------
# Remote instances, database-only bundles, escaping, the command line
# ---------------------------------------------------------------------------
@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class SpecialBundleTests(RenderCase):
    def test_a_remote_instance_renders_its_note_and_places_no_files(self):
        self.os_host().write()
        run = self.db.postgresql(instance="dbhost.example.com:5432", local=False,
                                 paths={"data": ["/var/lib/pgsql/16/data"]})
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()
        result = self.analyse()
        self.assertEqual(self.of(result, "D1").notes, [REMOTE_NOTE])
        texts = self.write_reports(result)
        markdown, page = texts["report.md"], texts["report.html"]
        self.assertIn("**Note:** " + REMOTE_NOTE + ".", markdown)
        self.assertEqual(markdown.count(REMOTE_NOTE), 2)               # the summary card and the finding
        self.assertIn('<p class="note">%s</p>' % REMOTE_NOTE, page)
        databases = section_between(markdown, "## Databases", "## Transient events")
        self.assertIn("- **Location:** remote", databases)
        self.assertIn("File placement is not shown: remote server: its file paths belong to the database host",
                      databases)
        self.assertNotIn("| Role | Path |", databases)
        self.assertIn("File placement is not shown: remote server:", page)

    def test_a_database_only_bundle_reports_and_says_there_is_no_os_data(self):
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data")
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()
        result = self.analyse()
        texts = self.write_reports(result)
        markdown, page = texts["report.md"], texts["report.html"]
        note = "no os or psar run in the bundle: only database collector data is available for this host"
        self.assertIn("NOTE: " + note, markdown)
        self.assertIn("NOTE: " + note, page)
        self.assertIn("D1 ", markdown)
        self.assertIn("## Databases", markdown)
        self.assertIn('<h2 id="databases">Databases</h2>', page)
        databases = section_between(markdown, "## Databases", "## Transient events")
        self.assertIn("File placement", databases)
        self.assertIn("no hardware inventory: files could not be placed on devices", databases)
        self.assertIn("| data | /var/lib/pgsql/16/data | not placed | - |", databases)
        self.assertEqual(json.loads(texts["findings.json"])["databases"][0]["short"], "pg:5432")

    def test_a_database_name_with_markup_and_a_comma_is_escaped_in_html(self):
        name = "OR<CL,1>/PDB<1>"
        self.os_host().write()
        self.db.oracle(instance=name, datafile_directory="/u01/oradata").write()
        result = self.analyse()
        self.assertEqual(result.host.databases[0].label, "Oracle 19.3.0.0.0 (%s)" % name)
        texts = self.write_reports(result)
        page = texts["report.html"]
        self.assertNotIn("OR<CL", page)
        self.assertNotIn("PDB<1>", page)
        escaped = "OR&lt;CL,1&gt;/PDB&lt;1&gt;"
        self.assertIn("<dt>Databases</dt><dd>Oracle 19.3.0.0.0 (%s)</dd>" % escaped, page)
        self.assertIn("<h3 id=\"database-1\">Oracle 19.3.0.0.0 (%s)</h3>" % escaped, page)
        self.assertIn("<li>Oracle %s: connected to a PDB" % escaped.replace("Oracle ", ""), page)
        # The label survives into the JSON, and Markdown keeps the text as it is.
        self.assertEqual(json.loads(texts["findings.json"])["databases"][0]["label"], "Oracle 19.3.0.0.0 (%s)" % name)
        self.assertIn("### Oracle 19.3.0.0.0 (%s)" % name, texts["report.md"])

    def test_a_pipe_in_a_database_name_does_not_break_a_markdown_table(self):
        self.os_host().write()
        self.db.oracle(datafile_directory="/u01/oradata/a|b").write()
        databases = section_between(self.write_reports(self.analyse())["report.md"], "## Databases", "## Transient events")
        self.assertIn("/u01/oradata/a\\|b", databases)

    def test_perfdiag_report_both_runs_end_to_end_on_a_database_scenario(self):
        CheckpointBurst(self)
        shutil.rmtree(self.out, ignore_errors=True)
        with contextlib.redirect_stdout(io.StringIO()):            # the command lists the files it wrote
            code = cli.main(["report", self.tmp, "--format", "both", "-o", self.out, "--sustain", "10s"])
        self.assertEqual(code, 0)
        for name in ("report.html", "report.md", "findings.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name)), name)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "charts", "db_waits_pg_5432.png")))
        self.assertIn("## Databases", read_text(os.path.join(self.out, "report.md"), encoding="utf-8"))
        self.assertEqual(json.loads(read_text(os.path.join(self.out, "findings.json")))["schema"], 2)


if __name__ == "__main__":
    unittest.main()
