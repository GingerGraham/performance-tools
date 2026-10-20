"""Charts and report output."""

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

from perfdiag import analysis, bundle, loader, report  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from synth import Synth, hardware_for  # noqa: E402


def storage_incident(root, host="db01", command_text=None):
    synth = Synth(root, host=host, samples=120, interval=10)
    disk = synth.disks["vda"]
    synth.fill(disk["await"], 10, 100, 25.0)
    synth.fill(disk["tps"], 10, 100, 300.0)
    synth.fill(disk["wkB"], 10, 100, 80000.0)
    synth.fill(synth.psi_io_some, 10, 100, 40.0)
    synth.hardware = hardware_for({"vda": {"class": "cloud-ebs"}}, extra={
        "fs.1.mountpoint": "/var/lib/pgsql", "fs.1.source": "/dev/vda", "fs.1.fstype": "xfs", "fs.1.kname": "vda"})
    synth.write()
    return synth


def analyse(root, host="db01"):
    with bundle.open_bundle(root) as opened:
        data = loader.load_host(opened, host)
        return analysis.run_analysis(data, Settings())


@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-report-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "out")

    def test_html_is_self_contained(self):  # T21 (offline, no scripts)
        storage_incident(self.tmp)
        paths = report.write_reports([analyse(self.tmp)], self.out, "html")
        text = open(os.path.join(self.out, "report.html"), encoding="utf-8").read()
        self.assertIn("<svg", text)
        self.assertNotIn("<script", text.lower())
        self.assertNotRegex(text, r'(src|href)="https?://')
        self.assertNotIn("@import", text)
        self.assertNotRegex(text, r"url\(\s*['\"]?https?:")
        self.assertIn("prefers-color-scheme: dark", text)
        self.assertIn("@media print", text)
        self.assertIn("H1 Storage device saturated", text)
        self.assertIn("vda (/var/lib/pgsql)", text)
        for heading in ("Summary", "Indicator timeline", "Findings", "Transient events", "Time above threshold",
                        "Host and storage layout", "Data coverage and limits", "Analysis settings"):
            self.assertIn(heading, text)
        self.assertTrue(any(p.endswith("findings.json") for p in paths))

    def test_kernel_findings_are_a_separate_block_and_related_devices_are_shown(self):
        synth = storage_incident(self.tmp)
        synth.add_disk("rhel-root")
        synth.fill(synth.disks["rhel-root"]["await"], 10, 100, 25.0)
        synth.fill(synth.disks["rhel-root"]["tps"], 10, 100, 300.0)
        synth.hardware.update({"disk.dm-0.class": "stacked", "disk.dm-0.display": "rhel-root",
                               "disk.dm-0.slaves": "vda"})
        synth.kernel_lines = ["2026-10-16T15:20:00+0000 db01 kernel: Memory cgroup out of memory: Killed process 4242 (java)"]
        synth.write()
        report.write_reports([analyse(self.tmp)], self.out, "both")
        with open(os.path.join(self.out, "report.html"), encoding="utf-8") as handle:
            text = handle.read()
        with open(os.path.join(self.out, "report.md"), encoding="utf-8") as handle:
            markdown = handle.read()
        self.assertIn("Kernel log findings", text)
        self.assertIn("H12 Cgroup memory limit reached", text)
        self.assertIn("Also seen on: rhel-root", text)
        self.assertIn("### Kernel log findings", markdown)
        self.assertIn("Also seen on: rhel-root", markdown)
        with open(os.path.join(self.out, "findings.json"), encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual([h["origin"] for h in document["hypotheses"]], ["measured", "kernel"])

    def test_throughput_chart_is_labelled_in_mebibytes(self):
        storage_incident(self.tmp)
        from perfdiag import charts
        svg = charts.throughput_chart(analyse(self.tmp), "vda")
        text = charts.render(svg, "svg")
        self.assertIn("MiB/s", text)
        self.assertNotIn("MB/s", text.replace("MiB/s", ""))

    def test_svg_ids_are_unique_across_inline_charts(self):
        storage_incident(self.tmp)
        report.write_reports([analyse(self.tmp)], self.out, "html")
        text = open(os.path.join(self.out, "report.html"), encoding="utf-8").read()
        ids = re.findall(r'\bid="([^"]+)"', text)
        self.assertEqual(len(ids), len(set(ids)))

    def test_bundle_text_is_escaped(self):
        synth = storage_incident(self.tmp, host="db01")
        manifest = os.path.join(self.tmp, "db01_os_20261016_151320", "manifest.txt")
        text = open(manifest).read().replace("distro=Test Linux 1.0", "distro=<script>alert(1)</script>")
        open(manifest, "w").write(text)
        synth.kernel_lines = ["2026-10-16T15:20:00+0000 db01 kernel: blk_update_request: I/O error, dev vda <img src=x onerror=alert(1)>"]
        synth.write()
        open(manifest, "w").write(text)
        report.write_reports([analyse(self.tmp)], self.out, "html")
        page = open(os.path.join(self.out, "report.html"), encoding="utf-8").read()
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;", page)

    def test_markdown_and_png_charts(self):
        storage_incident(self.tmp)
        report.write_reports([analyse(self.tmp)], self.out, "md")
        text = open(os.path.join(self.out, "report.md"), encoding="utf-8").read()
        self.assertIn("## Findings", text)
        for link in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
            self.assertTrue(os.path.isfile(os.path.join(self.out, link)), link)
        self.assertFalse(os.path.exists(os.path.join(self.out, "report.html")))

    def test_findings_json_written_next_to_report(self):
        storage_incident(self.tmp)
        report.write_reports([analyse(self.tmp)], self.out, "both")
        document = json.load(open(os.path.join(self.out, "findings.json")))
        self.assertEqual(document["schema"], 1)

    def test_no_findings_still_produces_a_valid_report(self):
        Synth(self.tmp, samples=60, interval=10).write()
        report.write_reports([analyse(self.tmp)], self.out, "html")
        text = open(os.path.join(self.out, "report.html"), encoding="utf-8").read()
        self.assertIn("No hypothesis was raised", text)

    def test_two_hosts_give_two_reports_and_an_index(self):
        for host in ("alpha", "beta"):
            storage_incident(self.tmp, host=host)
        results = [analyse(self.tmp, "alpha"), analyse(self.tmp, "beta")]
        report.write_reports(results, self.out, "html")
        self.assertTrue(os.path.isfile(os.path.join(self.out, "alpha", "report.html")))
        self.assertTrue(os.path.isfile(os.path.join(self.out, "beta", "report.html")))
        index = open(os.path.join(self.out, "index.html"), encoding="utf-8").read()
        self.assertIn("alpha/report.html", index)

    def test_t22_line_breaks_at_gap(self):
        from perfdiag import charts
        times, values = charts.with_gaps([10, 20, 30, 4000, 4010], [1, 2, 3, 4, 5], 10)
        self.assertEqual(len(times), 6)
        self.assertTrue(values[3] != values[3])               # NaN inserted

    def test_downsample_keeps_peaks(self):
        from perfdiag import charts
        times = list(range(20000))
        values = [0.0] * 20000
        values[12345] = 99.0
        out_times, out_values = charts.downsample(times, values, 5000)
        self.assertLessEqual(len(out_times), 5100)
        self.assertEqual(max(out_values), 99.0)

    def test_plot_catalogue_files(self):
        storage_incident(self.tmp)
        result = analyse(self.tmp)
        from perfdiag import charts
        os.makedirs(self.out)
        names = {os.path.basename(p) for p in charts.write_all(result, self.out, "svg")}
        for expected in ("timeline.svg", "disk_latency_vda.svg", "disk_throughput_vda.svg", "cpu.svg", "memory.svg", "psi.svg"):
            self.assertIn(expected, names)
        svg = open(os.path.join(self.out, "disk_latency_vda.svg"), encoding="utf-8").read()
        self.assertIn("<text", svg)                          # text stays selectable


if __name__ == "__main__":
    unittest.main()
