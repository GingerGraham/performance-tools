"""Analysis behaviour on synthetic and generated captures (spec tests T14-T20, T22)."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perfdiag import analysis, bundle, loader  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from synth import BASE_EPOCH, Synth, hardware_for  # noqa: E402


def analyse_dir(root, settings=None, host="db01"):
    settings = settings or Settings()
    with bundle.open_bundle(root) as opened:
        data = loader.load_host(opened, host)
        return analysis.run_analysis(data, settings)


class AnalysisCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make(self, **kwargs):
        return Synth(self.tmp, **kwargs)

    def settings(self, **pairs):
        settings = Settings()
        for key, value in pairs.items():
            settings.set(key.replace("__", "."), value, "test")
        return settings

    def hypotheses(self, result, code):
        return [h for h in result.hypotheses if h.id == code]


class RankingTests(AnalysisCase):
    def test_t14_sustained_ranks_above_taller_burst(self):
        synth = self.make(samples=120, interval=1)
        disk = synth.disks["vda"]
        synth.fill(disk["wkB"], 20, 25, 600000.0)       # 5 s at 2x
        synth.fill(disk["wkB"], 40, 85, 450000.0)       # 45 s at 1.5x
        synth.write()
        settings = self.settings(analysis__sustain_s=10, analysis__gap_samples=2,
                                 disk__write_kBps__warn=300000, disk__write_kBps__crit=600000)
        result = analyse_dir(self.tmp, settings)
        episodes = result.episodes_for("disk_write_kBps", "vda")
        self.assertEqual([e.kind for e in sorted(episodes, key=lambda e: e.start)], ["transient", "sustained"])
        ranked = [e for e in result.episodes if e.rule == "disk_write_kBps"]
        self.assertEqual(ranked[0].kind, "sustained")
        self.assertAlmostEqual(ranked[0].score, 22.5, places=1)
        self.assertAlmostEqual(ranked[1].score, 5.0, places=1)
        self.assertEqual(ranked[0].duration_s, 45)
        self.assertEqual(ranked[1].duration_s, 5)

    def test_median_not_max_decides_severity(self):
        synth = self.make(samples=60, interval=1)
        disk = synth.disks["vda"]
        synth.fill(disk["wkB"], 10, 40, 350000.0)
        disk["wkB"][20] = 5000000.0                       # one extreme sample
        synth.write()
        settings = self.settings(analysis__sustain_s=10, disk__write_kBps__warn=300000, disk__write_kBps__crit=600000)
        episode = analyse_dir(self.tmp, settings).episodes_for("disk_write_kBps")[0]
        self.assertEqual(episode.severity, "warn")

    def test_gap_tolerance_joins_a_dip(self):
        synth = self.make(samples=60, interval=1)
        disk = synth.disks["vda"]
        synth.fill(disk["wkB"], 10, 40, 350000.0)
        disk["wkB"][25] = 0.0
        synth.write()
        settings = self.settings(analysis__sustain_s=10, analysis__gap_samples=2, disk__write_kBps__warn=300000)
        episodes = analyse_dir(self.tmp, settings).episodes_for("disk_write_kBps")
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].duration_s, 30)

    def test_data_gap_closes_a_run(self):
        synth = self.make(samples=80, interval=1)
        disk = synth.disks["vda"]
        synth.fill(disk["wkB"], 10, 70, 350000.0)
        synth.skip_ranges = [(30, 49)]                    # 20 s hole in the data
        synth.write()
        settings = self.settings(analysis__sustain_s=10, analysis__gap_samples=5, disk__write_kBps__warn=300000)
        episodes = analyse_dir(self.tmp, settings).episodes_for("disk_write_kBps")
        self.assertEqual(len(episodes), 2)


class AbsorbedTests(AnalysisCase):
    def test_t15_high_throughput_normal_latency_is_not_a_finding(self):
        synth = self.make(samples=120, interval=1)
        disk = synth.disks["vda"]
        synth.fill(disk["wkB"], 20, 28, 900000.0)         # 8 s burst well over 80 % of a declared limit
        synth.fill(disk["wkB"], 60, 66, 900000.0)
        synth.hardware = hardware_for({"vda": {"class": "nvme"}})
        synth.write()
        settings = self.settings(analysis__sustain_s=10)
        settings.set("device.vda.throughput_mbps", "800", "test")
        result = analyse_dir(self.tmp, settings)
        self.assertEqual(result.hypotheses, [])
        self.assertEqual(result.sustained(), [])
        self.assertEqual(result.impactful, [])
        self.assertEqual(result.absorbed.get("disk_throughput:vda"), 2)

    def test_throughput_burst_with_latency_is_impactful(self):
        synth = self.make(samples=120, interval=1)
        disk = synth.disks["vda"]
        synth.fill(disk["wkB"], 20, 28, 900000.0)
        synth.fill(disk["await"], 20, 28, 40.0)           # latency spike during the burst
        synth.fill(disk["tps"], 20, 28, 500.0)
        synth.hardware = hardware_for({"vda": {"class": "nvme"}})
        synth.write()
        settings = self.settings(analysis__sustain_s=10)
        settings.set("device.vda.throughput_mbps", "800", "test")
        result = analyse_dir(self.tmp, settings)
        rules = {e.rule for e in result.impactful}
        self.assertIn("disk_throughput", rules)
        self.assertIn("disk_await", rules)

    def test_low_tps_samples_do_not_count_for_latency(self):
        synth = self.make(samples=120, interval=1)
        disk = synth.disks["vda"]
        synth.fill(disk["await"], 10, 100, 500.0)         # huge await but only 1 IO/s
        synth.write()
        result = analyse_dir(self.tmp, self.settings(analysis__sustain_s=10))
        self.assertEqual(result.episodes_for("disk_await"), [])


class CpuTests(AnalysisCase):
    def test_t16_one_busy_cpu_is_h5_not_h3(self):
        synth = self.make(samples=120, interval=10, cpus=2)
        synth.fill(synth.cpu_idle["0"], 10, 110, 0.0)
        synth.fill(synth.cpu_idle["all"], 10, 110, 50.0)
        synth.hardware = hardware_for({"vda": {}})
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertEqual(len(self.hypotheses(result, "H5")), 1)
        self.assertEqual(self.hypotheses(result, "H3"), [])
        self.assertEqual(self.hypotheses(result, "H5")[0].subject, "CPU 0")

    def test_all_cpus_busy_is_h3(self):
        synth = self.make(samples=120, interval=10, cpus=2)
        synth.fill(synth.cpu_idle["all"], 10, 110, 2.0)
        synth.fill(synth.cpu_idle["0"], 10, 110, 2.0)
        synth.fill(synth.cpu_idle["1"], 10, 110, 2.0)
        synth.fill(synth.psi_cpu, 10, 110, 60.0)
        synth.write()
        result = analyse_dir(self.tmp)
        h3 = self.hypotheses(result, "H3")
        self.assertEqual(len(h3), 1)
        self.assertEqual(h3[0].primary.rule, "cpu_busy")
        self.assertEqual(h3[0].primary.severity, "crit")
        self.assertEqual([e.rule for e in h3[0].supporting], ["psi_cpu"])
        self.assertEqual(self.hypotheses(result, "H5"), [])

    def test_steal_is_h4(self):
        synth = self.make(samples=120, interval=10)
        synth.fill(synth.steal, 10, 100, 20.0)
        synth.hardware = hardware_for({"vda": {}}, extra={"cloud.provider": "aws", "cloud.instance_type": "t3.medium"})
        synth.write()
        h4 = self.hypotheses(analyse_dir(self.tmp), "H4")
        self.assertEqual(len(h4), 1)
        self.assertTrue(any("burstable" in fact for fact in h4[0].supporting_facts))
        self.assertTrue(any("CPUCreditBalance" in step for step in h4[0].next_steps))

    def test_t17_blocked_load_is_h8_not_h3(self):
        synth = self.make(samples=120, interval=10, cpus=2)
        synth.fill(synth.avg1, 10, 110, 6.0)              # load 3 x CPUs
        synth.fill(synth.blocked, 10, 110, 5)
        synth.fill(synth.cpu_idle["all"], 10, 110, 90.0)
        synth.write()
        result = analyse_dir(self.tmp)
        h8 = self.hypotheses(result, "H8")
        self.assertEqual(len(h8), 1)
        self.assertEqual(self.hypotheses(result, "H3"), [])
        self.assertIn("blocked", [e.rule for e in h8[0].supporting])
        self.assertTrue(h8[0].supporting_facts)
        self.assertEqual(h8[0].confidence, "high")

    def test_load_with_busy_cpu_counts_against_h8(self):
        synth = self.make(samples=120, interval=10, cpus=2)
        synth.fill(synth.avg1, 10, 110, 6.0)
        synth.fill(synth.cpu_idle["all"], 10, 110, 2.0)
        synth.write()
        result = analyse_dir(self.tmp)
        h8 = self.hypotheses(result, "H8")
        self.assertEqual(len(h8), 1)
        self.assertEqual(h8[0].confidence, "low")
        self.assertTrue(h8[0].contradicting)


class MemoryTests(AnalysisCase):
    def test_t18_low_available_memory_alone_is_not_a_finding(self):
        synth = self.make(samples=120, interval=10)
        synth.fill(synth.mem_avail_pct, 10, 110, 3.0)
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertEqual(self.hypotheses(result, "H6"), [])
        self.assertTrue(result.episodes_for("mem_avail", kind="sustained"))

    def test_swapping_with_low_memory_is_h6(self):
        synth = self.make(samples=120, interval=10)
        synth.fill(synth.mem_avail_pct, 10, 110, 3.0)
        synth.fill(synth.swap_out, 10, 110, 400.0)
        synth.fill(synth.majflt, 10, 110, 900.0)
        synth.write()
        h6 = self.hypotheses(analyse_dir(self.tmp), "H6")
        self.assertEqual(len(h6), 1)
        self.assertEqual(h6[0].confidence, "high")
        self.assertEqual(h6[0].primary.rule, "swap_out")

    def test_oom_event_raises_h6_alone(self):
        synth = self.make(samples=120, interval=10)
        synth.kernel_lines = ["2026-10-16T15:20:00+0000 db01 kernel: Out of memory: Killed process 4242 (postgres)"]
        synth.write()
        h6 = self.hypotheses(analyse_dir(self.tmp), "H6")
        self.assertEqual(len(h6), 1)
        self.assertIsNone(h6[0].primary)
        self.assertEqual(h6[0].confidence, "high")
        self.assertEqual(h6[0].kernel_events[0].category, "oom")


class DiskTests(AnalysisCase):
    def _saturated(self, name="vda", cls="cloud-ebs", extra_hw=None):
        synth = self.make(samples=120, interval=10)
        disk = synth.disks[name]
        synth.fill(disk["await"], 10, 100, 25.0)
        synth.fill(disk["tps"], 10, 100, 300.0)
        synth.fill(disk["wkB"], 10, 100, 80000.0)
        synth.fill(synth.psi_io_some, 10, 100, 40.0)
        synth.hardware = hardware_for({name: {"class": cls}}, extra=extra_hw)
        return synth

    def test_h1_with_class_threshold_and_supporting_psi(self):
        self._saturated().write()
        result = analyse_dir(self.tmp)
        h1 = self.hypotheses(result, "H1")
        self.assertEqual(len(h1), 1)
        self.assertEqual(h1[0].subject, "vda")
        self.assertEqual(h1[0].primary.threshold["warn"], 10)
        self.assertEqual(h1[0].primary.threshold["source"], "default:cloud-ebs")
        self.assertIn("psi_io_some", [e.rule for e in h1[0].supporting])

    def test_class_override_changes_threshold(self):
        self._saturated().write()
        settings = self.settings()
        settings.set("device.vda.class", "nvme", "test")
        h1 = self.hypotheses(analyse_dir(self.tmp, settings), "H1")
        self.assertEqual(h1[0].primary.threshold["warn"], 2)
        self.assertEqual(h1[0].primary.threshold["source"], "override:nvme")

    def test_san_device_uses_san_thresholds_and_no_hdd_rules(self):  # F3
        synth = self._saturated(cls="san")
        synth.fill(synth.disks["vda"]["await"], 10, 100, 15.0)
        synth.fill(synth.disks["vda"]["queue"], 10, 100, 30.0)
        synth.fill(synth.disks["vda"]["util"], 10, 100, 99.0)
        synth.write()
        result = analyse_dir(self.tmp)
        h1 = self.hypotheses(result, "H1")
        self.assertEqual(len(h1), 1)
        self.assertEqual(h1[0].primary.threshold["warn"], 10)
        self.assertEqual(h1[0].primary.threshold["source"], "default:san")
        self.assertIsNone(result.indicator("disk_util", "vda"))
        self.assertIsNone(result.indicator("disk_queue", "vda"))
        self.assertTrue(any("multipath -ll" in step for step in h1[0].next_steps))

    def test_san_step_also_shown_for_a_stacked_device_on_a_san_lun(self):  # F3
        synth = self.make(samples=120, interval=10)
        synth.add_disk("dm-0")
        synth.fill(synth.disks["dm-0"]["await"], 10, 100, 25.0)
        synth.fill(synth.disks["dm-0"]["tps"], 10, 100, 100.0)
        synth.hardware = hardware_for({
            "vda": {"class": "san"}, "dm-0": {"class": "stacked", "slaves": ["vda"], "display": "mpatha"}})
        synth.write()
        h1 = self.hypotheses(analyse_dir(self.tmp), "H1")
        self.assertEqual(len(h1), 1)
        self.assertTrue(any("multipath -ll" in step for step in h1[0].next_steps))

    def test_non_san_device_has_no_san_step(self):  # F3
        self._saturated().write()
        h1 = self.hypotheses(analyse_dir(self.tmp), "H1")
        self.assertFalse(any("multipath -ll" in step for step in h1[0].next_steps))

    def test_latency_flag_style_override(self):
        self._saturated().write()
        settings = self.settings()
        settings.set("disk.latency.cloud-ebs.warn_ms", "30", "test")
        result = analyse_dir(self.tmp, settings)
        self.assertEqual(self.hypotheses(result, "H1"), [])

    def test_hdd_queue_uses_parallelism(self):
        synth = self.make(samples=120, interval=10)
        disk = synth.disks["vda"]
        synth.fill(disk["await"], 10, 100, 60.0)
        synth.fill(disk["tps"], 10, 100, 200.0)
        synth.fill(disk["queue"], 10, 100, 7.0)
        synth.fill(disk["util"], 10, 100, 99.0)
        synth.hardware = hardware_for({"vda": {"class": "hdd", "parallelism": 1}})
        synth.write()
        result = analyse_dir(self.tmp)
        queue = result.episodes_for("disk_queue", "vda", "sustained")
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].severity, "crit")          # 7 > 4 x 1 and await 60 >= crit 50
        h1 = self.hypotheses(result, "H1")[0]
        self.assertEqual(h1.confidence, "high")
        self.assertIn("disk_queue", [e.rule for e in h1.supporting])
        self.assertIn("disk_util", [e.rule for e in h1.supporting])

    def test_hdd_queue_not_raised_with_more_spindles(self):
        synth = self.make(samples=120, interval=10)
        disk = synth.disks["vda"]
        synth.fill(disk["await"], 10, 100, 60.0)
        synth.fill(disk["tps"], 10, 100, 200.0)
        synth.fill(disk["queue"], 10, 100, 7.0)
        synth.hardware = hardware_for({"vda": {"class": "hdd", "parallelism": 4}})
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertEqual(result.episodes_for("disk_queue"), [])

    def test_hw_raid_parallelism_unknown_label_and_spindles(self):
        synth = self.make(samples=120, interval=10)
        disk = synth.disks["vda"]
        synth.fill(disk["await"], 10, 100, 60.0)
        synth.fill(disk["tps"], 10, 100, 200.0)
        synth.fill(disk["queue"], 10, 100, 7.0)
        synth.hardware = hardware_for({"vda": {"class": "hw-raid"}})
        synth.write()
        episode = analyse_dir(self.tmp).episodes_for("disk_queue", "vda", "sustained")[0]
        self.assertIn("parallelism unknown", episode.notes)
        settings = self.settings()
        settings.set("device.vda.spindles", "8", "test")
        self.assertEqual(analyse_dir(self.tmp, settings).episodes_for("disk_queue"), [])

    def test_util_is_only_an_indicator_for_hdd(self):
        synth = self.make(samples=120, interval=10)
        disk = synth.disks["vda"]
        synth.fill(disk["util"], 10, 100, 99.0)
        synth.hardware = hardware_for({"vda": {"class": "nvme"}})
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertIsNone(result.indicator("disk_util", "vda"))
        self.assertEqual(result.hypotheses, [])

    def test_plateau_on_cloud_volume_raises_h2(self):
        synth = self.make(samples=400, interval=10)
        disk = synth.disks["vda"]
        for index in range(20, 120):
            disk["wkB"][index] = 125000.0 + (index % 3)         # flat at the cap
            disk["tps"][index] = 2000.0
            disk["await"][index] = 20.0
        synth.hardware = hardware_for({"vda": {"class": "cloud-ebs"}},
                                      extra={"cloud.provider": "aws", "cloud.instance_type": "m5.large"})
        synth.write()
        result = analyse_dir(self.tmp)
        h2 = self.hypotheses(result, "H2")
        self.assertEqual(len(h2), 1)
        self.assertEqual(h2[0].primary.rule, "disk_plateau")
        self.assertTrue(any("m5.large" in fact for fact in h2[0].supporting_facts))
        self.assertTrue(any("CloudWatch" in step for step in h2[0].next_steps))

    def test_steady_workload_with_normal_latency_is_not_a_plateau(self):
        synth = self.make(samples=400, interval=10)
        disk = synth.disks["vda"]
        for index in range(20, 120):
            disk["wkB"][index] = 125000.0
            disk["tps"][index] = 2000.0
        synth.hardware = hardware_for({"vda": {"class": "cloud-ebs"}})
        synth.write()
        self.assertEqual(analyse_dir(self.tmp).episodes_for("disk_plateau"), [])

    def test_sum_plateau_across_cloud_volumes(self):
        synth = self.make(samples=400, interval=10)
        synth.add_disk("vdb")
        for name in ("vda", "vdb"):
            disk = synth.disks[name]
            for index in range(20, 120):
                disk["wkB"][index] = 60000.0
                disk["tps"][index] = 1000.0
                disk["await"][index] = 20.0 if name == "vda" else 1.0
        # Each volume alone is flat too, but only the sum must be reported with both volumes named.
        synth.hardware = hardware_for({"vda": {"class": "cloud-ebs"}, "vdb": {"class": "cloud-ebs"}})
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertTrue(result.episodes_for("disk_plateau", "cloud-sum"))
        self.assertTrue(any("vda, vdb" in note for e in result.episodes_for("disk_plateau", "cloud-sum") for note in e.notes))

    def test_h7_needs_dirty_or_full_stall_evidence(self):
        synth = self._saturated(cls="ssd")
        synth.write()
        self.assertEqual(self.hypotheses(analyse_dir(self.tmp), "H7"), [])
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        synth = self._saturated(cls="ssd")
        synth.fill(synth.dirty_kb, 10, 100, int(synth.mem_total_kb * 0.15))
        synth.write()
        h7 = self.hypotheses(analyse_dir(self.tmp), "H7")
        self.assertEqual(len(h7), 1)
        self.assertTrue(any("writes" in fact for fact in h7[0].supporting_facts))

    def test_h7_not_raised_when_read_dominant(self):
        synth = self._saturated(cls="ssd")
        synth.fill(synth.dirty_kb, 10, 100, int(synth.mem_total_kb * 0.15))
        synth.fill(synth.disks["vda"]["rkB"], 10, 100, 900000.0)
        synth.write()
        self.assertEqual(self.hypotheses(analyse_dir(self.tmp), "H7"), [])

    def test_kernel_io_error_raises_h1_high(self):
        synth = self.make(samples=120, interval=10)
        synth.hardware = hardware_for({"vda": {"class": "virtual"}})
        synth.kernel_lines = ["2026-10-16T15:20:00+0000 db01 kernel: blk_update_request: I/O error, dev vda, sector 2048"]
        synth.write()
        h1 = self.hypotheses(analyse_dir(self.tmp), "H1")
        self.assertEqual(len(h1), 1)
        self.assertEqual(h1[0].subject, "vda")
        self.assertEqual(h1[0].confidence, "high")

    def test_stacked_device_uses_lowest_slave_threshold(self):
        synth = self.make(samples=120, interval=10)
        synth.add_disk("dm-0")
        synth.add_disk("vdb")
        for name in ("dm-0", "vda"):
            synth.fill(synth.disks[name]["await"], 10, 100, 8.0)
            synth.fill(synth.disks[name]["tps"], 10, 100, 100.0)
        synth.hardware = hardware_for({
            "vda": {"class": "nvme"}, "vdb": {"class": "ssd"},
            "dm-0": {"class": "stacked", "slaves": ["vda", "vdb"], "display": "vg-data"}})
        synth.write()
        profile = analyse_dir(self.tmp).profile_for("dm-0")
        self.assertEqual((profile.warn_ms, profile.crit_ms), (2, 10))

    def test_stack_overhead(self):
        synth = self.make(samples=120, interval=10)
        synth.add_disk("dm-0")
        synth.fill(synth.disks["dm-0"]["await"], 10, 100, 30.0)
        synth.fill(synth.disks["dm-0"]["tps"], 10, 100, 100.0)
        synth.fill(synth.disks["vda"]["await"], 10, 100, 4.0)
        synth.fill(synth.disks["vda"]["tps"], 10, 100, 100.0)
        synth.hardware = hardware_for({
            "vda": {"class": "virtual"}, "dm-0": {"class": "stacked", "slaves": ["vda"], "dm_target": "crypt"}})
        synth.write()
        result = analyse_dir(self.tmp)
        h10 = self.hypotheses(result, "H10")
        self.assertEqual(len(h10), 1)
        self.assertTrue(any("crypt" in fact for fact in h10[0].supporting_facts))


class SamplingTests(AnalysisCase):
    def test_t20_coarse_interval_is_interval_limited(self):
        synth = self.make(samples=12, interval=600)
        synth.fill(synth.cpu_idle["all"], 2, 10, 1.0)
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertTrue(result.interval_limited)
        self.assertEqual(result.sustain_s, 1800)
        self.assertTrue(any("interval-limited" in note for note in result.notes))
        busy = result.episodes_for("cpu_busy", "all")
        self.assertTrue(all(e.interval_limited for e in busy))

    def test_default_five_minute_window_at_ten_second_interval(self):
        synth = self.make(samples=120, interval=10)
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertFalse(result.interval_limited)
        self.assertEqual(result.sustain_s, 300)
        self.assertEqual(result.gap_samples, 5)

    def test_gap_samples_formula(self):
        from perfdiag.episodes import default_gap_samples
        self.assertEqual(default_gap_samples(300, 10), 5)       # round(6) capped at 5
        self.assertEqual(default_gap_samples(300, 60), 1)
        self.assertEqual(default_gap_samples(10, 1), 2)
        self.assertEqual(default_gap_samples(1800, 600), 1)


class CanonicalTests(AnalysisCase):
    def test_t19_old_sysstat_sectors_become_kilobytes(self):
        synth = self.make(samples=30, interval=10, old_sysstat=True)
        synth.fill(synth.disks["vda"]["wkB"], 0, 30, 2048.0)
        synth.write()
        with bundle.open_bundle(self.tmp) as opened:
            host = loader.load_host(opened, "db01")
        write = host.get("disk.write_kBps", "vda")
        self.assertAlmostEqual(write.values[0], 2048.0)
        self.assertTrue(any("sectors" in note for note in host.notes))
        self.assertIsNotNone(host.get("disk.queue", "vda"))
        # kbavail is missing in old files: the approximation is used and noted.
        self.assertIsNotNone(host.get("mem.avail_kB"))
        self.assertTrue(any("kbavail" in note for note in host.notes))

    def test_missing_psi_noted_once(self):
        synth = self.make(samples=30, interval=10, psi=False)
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertTrue(any("PSI not available" in note for note in result.notes))


class PsarBundleTests(AnalysisCase):
    """A bundle whose only data comes from psar CSV files."""

    def write_psar(self, samples=240, interval=5):
        import datetime
        run = os.path.join(self.tmp, "db01_psar_20261016_151320")
        os.makedirs(run)
        with open(os.path.join(run, "manifest.txt"), "w") as handle:
            handle.write("script=psar.sh\nhostname=db01\ncpus=2\nmem_total_kb=8388608\ntimezone=UTC +0000\n"
                         "interval_s=%d\nsamples=%d\npsi=no\nnotes=\n" % (interval, samples))
        start = datetime.datetime(2026, 10, 16, 15, 13, 20, tzinfo=datetime.timezone.utc)

        def stamp(index):
            return (start + datetime.timedelta(seconds=(index + 1) * interval)).isoformat(timespec="seconds")
        busy = range(60, 140)        # 400 s: sustained, and under half the run so the cswch median stays low
        with open(os.path.join(run, "cpu.csv"), "w") as handle:
            handle.write("ts,usr_pct,sys_pct,irq_pct,soft_pct,iowait_pct,steal_pct,idle_pct,run,blkd,cs_s\n")
            for index in range(samples):
                hot = index in busy
                handle.write("%s,90,5,0,0,0,0,%s,%s,%s,%s\n" % (stamp(index), "2.0" if hot else "95.0",
                                                                "6" if hot else "0", "4" if hot else "0",
                                                                "9000" if hot else "1000"))
        with open(os.path.join(run, "disk.csv"), "w") as handle:
            handle.write("ts,device,r_s,w_s,rkB_s,wkB_s,rareq-sz,wareq-sz,r_await,w_await,aqu-sz,util_pct\n")
            for index in range(samples):
                hot = index in busy
                handle.write("%s,vda,10,%s,100,%s,8,8,1,%s,%s,10\n" % (
                    stamp(index), "400" if hot else "5", "50000" if hot else "40", "30" if hot else "0.5",
                    "3" if hot else "0.1"))
        with open(os.path.join(run, "load.csv"), "w") as handle:
            handle.write("ts,1m,5m,15m\n")
            for index in range(samples):
                handle.write("%s,%s,1,1\n" % (stamp(index), "6.0" if index in busy else "0.1"))
        with open(os.path.join(run, "mem.csv"), "w") as handle:
            handle.write("ts,usedkB,availkB,used_pct,dirtykB,wbackkB,swusedkB,swpin_s,swpout_s,majflt_s\n")
            for index in range(samples):
                handle.write("%s,1000,7000000,10,100,0,0,0,0,0\n" % stamp(index))
        with open(os.path.join(run, "net.csv"), "w") as handle:
            handle.write("ts,iface,rxkB_s,txkB_s,rxpck_s,txpck_s,rxerr_s,txerr_s,rxdrop_s,txdrop_s\n")
            for index in range(samples):
                handle.write("%s,eth0,1,1,1,1,0,0,0,0\n" % stamp(index))

    def test_psar_only_bundle_gets_cpu_run_queue_and_latency_findings(self):
        self.write_psar()
        result = analyse_dir(self.tmp)
        host = result.host
        self.assertEqual(host.interval_s, 5)
        # the host-wide series must be found by the rules
        for rule in ("cpu_busy", "runq", "load", "blocked", "cswch"):
            self.assertIsNotNone(result.indicator(rule, "all" if rule == "cpu_busy" else ""), rule)
        self.assertTrue(result.episodes_for("runq", kind="sustained"))
        self.assertTrue(result.episodes_for("blocked", kind="sustained"))
        self.assertTrue(result.episodes_for("cswch", kind="sustained"))
        # psar's separate read and write latency drive the disk rules
        self.assertTrue(result.episodes_for("disk_w_await", "vda", "sustained"))
        self.assertIsNone(result.indicator("disk_await", "vda"))
        self.assertEqual([h.id for h in result.hypotheses if h.id == "H3"], ["H3"])

    def test_psar_bundle_without_hardware_notes_it(self):
        self.write_psar()
        result = analyse_dir(self.tmp)
        self.assertTrue(any("no hardware inventory" in note for note in result.host.notes))


class MultiRunTests(AnalysisCase):
    def test_t22_two_runs_one_host_with_a_gap(self):
        first = Synth(self.tmp, samples=60, interval=10, tag_stamp="20261016_151320", start=BASE_EPOCH)
        first.write()
        second = Synth(self.tmp, samples=60, interval=10, tag_stamp="20261016_171320", start=BASE_EPOCH + 7200)
        second.write()
        with bundle.open_bundle(self.tmp) as opened:
            self.assertEqual(opened.hosts(), ["db01"])
            host = loader.load_host(opened, "db01")
        series = host.get("cpu.idle_pct", "all")
        self.assertEqual(len(series), 120)
        self.assertEqual(len(host.gaps), 1)
        start, end = host.gaps[0]
        self.assertGreater(end - start, 3600)

    def test_episode_does_not_span_the_gap(self):
        first = Synth(self.tmp, samples=60, interval=10, tag_stamp="20261016_151320", start=BASE_EPOCH)
        first.fill(first.cpu_idle["all"], 20, 60, 1.0)
        first.write()
        second = Synth(self.tmp, samples=60, interval=10, tag_stamp="20261016_171320", start=BASE_EPOCH + 7200)
        second.fill(second.cpu_idle["all"], 0, 40, 1.0)
        second.write()
        episodes = analyse_dir(self.tmp).episodes_for("cpu_busy", "all")
        self.assertEqual(len(episodes), 2)


if __name__ == "__main__":
    unittest.main()
