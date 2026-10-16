"""Command line behaviour, findings.json and the real-capture regression."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perfdiag import analysis, bundle, cli, findings, loader  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from synth import Synth, hardware_for  # noqa: E402

HERE = os.path.dirname(__file__)
REAL = os.path.join(HERE, "fixtures", "real_96s")


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exit_request:
            code = exit_request.code
    return code, out.getvalue(), err.getvalue()


class RealCaptureRegression(unittest.TestCase):
    """The 96 s sadc capture with the load pattern of spec section 6.3."""

    def settings(self):
        settings = Settings()
        for key, value in (("analysis.sustain_s", 10), ("analysis.gap_samples", 2),
                           ("disk.write_kBps.warn", 300000), ("disk.write_kBps.crit", 600000)):
            settings.set(key, value, "test")
        return settings

    def analysed(self):
        with bundle.open_bundle(REAL) as opened:
            host = loader.load_host(opened, "vm")
            return analysis.run_analysis(host, self.settings())

    def test_sustained_write_found_at_injected_offset_and_duration(self):
        result = self.analysed()
        sustained = result.episodes_for("disk_write_kBps", "vda", "sustained")
        self.assertEqual(len(sustained), 1)
        self.assertAlmostEqual(sustained[0].duration_s, 45, delta=2)
        self.assertAlmostEqual(sustained[0].start - result.host.start, 32, delta=3)

    def test_short_burst_found_and_ranks_below(self):
        result = self.analysed()
        transient = result.episodes_for("disk_write_kBps", "vda", "transient")
        self.assertEqual(len(transient), 1)
        self.assertAlmostEqual(transient[0].duration_s, 5, delta=2)
        sustained = result.episodes_for("disk_write_kBps", "vda", "sustained")[0]
        self.assertGreater(sustained.score, transient[0].score * 3)
        order = [e.id for e in result.episodes if e.rule == "disk_write_kBps"]
        self.assertEqual(order[0], sustained.id)

    def test_util_not_a_saturation_signal_off_hdd(self):
        result = self.analysed()
        self.assertIsNone(result.indicator("disk_util", "vda"))
        self.assertEqual(self.hypotheses_for(result, "H1"), [])

    def hypotheses_for(self, result, code):
        return [h for h in result.hypotheses if h.id == code]

    def test_psi_io_pressure_visible_during_write(self):
        result = self.analysed()
        self.assertTrue(result.episodes_for("psi_io_full", kind="sustained"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        synth = Synth(self.tmp, samples=120, interval=10)
        disk = synth.disks["vda"]
        synth.fill(disk["await"], 10, 100, 25.0)
        synth.fill(disk["tps"], 10, 100, 300.0)
        synth.fill(synth.psi_io_some, 10, 100, 40.0)
        synth.hardware = hardware_for({"vda": {"class": "cloud-ebs"}})
        synth.write()

    def test_check_exits_zero_with_matplotlib(self):
        code, out, _ = run_cli("check")
        self.assertIn(code, (0, 1))
        self.assertIn("python", out)

    def test_inspect_lists_runs_disks_and_notes(self):
        code, out, _ = run_cli("inspect", self.tmp)
        self.assertEqual(code, 0)
        self.assertIn("Host db01", out)
        self.assertIn("db01_os_", out)
        self.assertIn("cloud-ebs", out)
        self.assertIn("sadf activities", out)

    def test_analyse_prints_hypothesis_and_writes_json(self):
        path = os.path.join(self.tmp, "findings.json")
        code, out, err = run_cli("analyse", self.tmp, "--json", path)
        self.assertEqual(code, 0, err)
        self.assertIn("H1 Storage device saturated", out)
        self.assertIn("confidence", out)
        self.assertIn("Next steps", out)
        with open(path) as handle:
            document = json.load(handle)
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["host"], "db01")
        self.assertEqual(document["window"]["interval_s"], 10)
        self.assertEqual(document["hypotheses"][0]["id"], "H1")
        primary_id = document["hypotheses"][0]["primary"]
        self.assertIn(primary_id, [e["id"] for e in document["episodes"]])
        self.assertIn("disk_await:vda", document["time_above"])
        stats = document["time_above"]["disk_await:vda"]
        for key in ("warn_pct", "crit_pct", "p50", "p95", "max"):
            self.assertIn(key, stats)
        for episode in document["episodes"]:
            self.assertIsInstance(episode["start"], int)

    def test_from_to_filter_excludes_the_event(self):
        code, out, _ = run_cli("analyse", self.tmp, "--from", "15:13", "--to", "15:15")
        self.assertEqual(code, 0)
        self.assertIn("No hypotheses raised", out)

    def test_tz_override_changes_clock_display(self):
        _, out_utc, _ = run_cli("analyse", self.tmp, "--tz", "UTC")
        _, out_plus, _ = run_cli("analyse", self.tmp, "--tz", "+01:00")
        self.assertIn("15:", out_utc)
        self.assertIn("16:", out_plus)

    def test_set_and_flags_change_thresholds(self):
        code, out, _ = run_cli("analyse", self.tmp, "--latency", "cloud-ebs=40")
        self.assertIn("No hypotheses raised", out)
        code, out, _ = run_cli("analyse", self.tmp, "--class", "vda=nvme")
        self.assertIn("2 ms", out)

    def test_bad_setting_is_exit_two_with_suggestion(self):
        code, _, err = run_cli("analyse", self.tmp, "--set", "cpu.stael.warn_pct=3")
        self.assertEqual(code, 2)
        self.assertIn("Did you mean", err)

    def test_bad_bundle_is_exit_one(self):
        code, _, err = run_cli("inspect", os.path.join(self.tmp, "missing.tar.gz"))
        self.assertEqual(code, 1)
        self.assertIn("ERROR", err)

    def test_t12_analyse_runs_without_matplotlib(self):
        with mock.patch.dict(sys.modules, {"matplotlib": None}):      # import raises ImportError
            code, out, _ = run_cli("analyse", self.tmp)
        self.assertEqual(code, 0)
        self.assertIn("H1", out)

    def test_plot_without_matplotlib_stops_with_install_steps(self):
        with mock.patch.dict(sys.modules, {"matplotlib": None}):
            code, _, err = run_cli("plot", self.tmp, "-o", os.path.join(self.tmp, "charts"))
        self.assertEqual(code, 1)
        self.assertIn("install", err)
        self.assertIn("matplotlib", err)

    def test_config_file_precedence(self):
        config = os.path.join(self.tmp, "p.ini")
        with open(config, "w") as handle:
            handle.write("[disk.latency]\ncloud-ebs.warn_ms = 8\n\n[analysis]\nsustain_s = 600\n")
        code, out, _ = run_cli("analyse", self.tmp, "--config", config, "--set", "analysis.sustain_s=120")
        self.assertEqual(code, 0)
        self.assertIn("sustain window 120 s", out)
        self.assertIn("8 ms", out)


class FindingTextTests(unittest.TestCase):
    def test_wording_is_indicators_not_causes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        synth = Synth(tmp, samples=120, interval=10)
        synth.fill(synth.disks["vda"]["await"], 10, 100, 25.0)
        synth.fill(synth.disks["vda"]["tps"], 10, 100, 300.0)
        synth.hardware = hardware_for({"vda": {"class": "cloud-ebs"}}, extra={
            "fs.1.mountpoint": "/var/lib/pgsql", "fs.1.source": "/dev/vda", "fs.1.fstype": "xfs", "fs.1.kname": "vda"})
        synth.write()
        with bundle.open_bundle(tmp) as opened:
            host = loader.load_host(opened, "db01")
            result = analysis.run_analysis(host, Settings())
        text = findings.hypothesis_text(result, result.hypotheses[0])
        self.assertIn("vda (/var/lib/pgsql)", text)
        self.assertRegex(text, r"Between \d\d:\d\d and \d\d:\d\d UTC \(\d+ min\)")
        self.assertIn("against a 10 ms warning threshold for cloud-ebs devices", text)
        self.assertIn("Not seen: no CPU saturation in this window.", text)
        self.assertNotIn("too slow", text)


if __name__ == "__main__":
    unittest.main()
