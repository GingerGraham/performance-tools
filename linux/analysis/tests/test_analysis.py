"""Analysis behaviour on synthetic and generated captures (spec tests T14-T20, T22)."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perfdiag import analysis, attribution, bundle, charts, findings, loader  # noqa: E402
from perfdiag.model import HostData, ProcessRow  # noqa: E402
from perfdiag.timeutil import clock_zone, zone_axis_label, zone_label  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from synth import BASE_EPOCH, Synth, hardware_for  # noqa: E402


def analyse_dir(root, settings=None, host="db01", include_partitions=False):
    settings = settings or Settings()
    with bundle.open_bundle(root) as opened:
        data = loader.load_host(opened, host, include_partitions=include_partitions)
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

    def test_f6_below_rules_are_not_outranked_by_unbounded_above_rules(self):
        synth = self.make(samples=480, interval=10)
        for key in synth.cpu_idle:
            synth.fill(synth.cpu_idle[key], 60, 420, 0.0)
        synth.fill(synth.disks["vda"]["await"], 60, 420, 6.0)      # 3x the NVMe warn of 2 ms
        synth.fill(synth.disks["vda"]["tps"], 60, 420, 300.0)
        synth.hardware = hardware_for({"vda": {"class": "nvme"}})
        synth.write()
        result = analyse_dir(self.tmp)
        order = [h.id for h in result.hypotheses]
        self.assertLess(order.index("H3"), order.index("H1"))
        h3 = self.hypotheses(result, "H3")[0]
        h1 = self.hypotheses(result, "H1")[0]
        self.assertAlmostEqual(h3.primary.score / h3.primary.duration_s, 1.33, delta=0.05)
        self.assertAlmostEqual(h1.primary.score / h1.primary.duration_s, 0.5, delta=0.05)

    def test_f6_exceedance_cap_is_a_setting(self):
        synth = self.make(samples=120, interval=10)
        synth.fill(synth.disks["vda"]["await"], 10, 100, 40.0)
        synth.fill(synth.disks["vda"]["tps"], 10, 100, 300.0)
        synth.hardware = hardware_for({"vda": {"class": "nvme"}})
        synth.write()
        default_score = analyse_dir(self.tmp).episodes_for("disk_await", "vda")[0]
        self.assertAlmostEqual(default_score.score / default_score.duration_s, 3.0, places=2)
        capped = analyse_dir(self.tmp, self.settings(analysis__exceedance_cap=1)).episodes_for("disk_await", "vda")[0]
        self.assertAlmostEqual(capped.score / capped.duration_s, 1.0, places=2)

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
        settings.set("device.vda.throughput_mibps", "800", "test")
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
        settings.set("device.vda.throughput_mibps", "800", "test")
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


class PartitionTests(AnalysisCase):
    """F1: partitions are folded into their parent disk."""

    def _bundle(self):
        synth = self.make(samples=120, interval=10)
        for name in ("sda", "sda1", "sda2", "rhel-root"):
            if name not in synth.disks:
                synth.add_disk(name)
            synth.fill(synth.disks[name]["await"], 10, 70, 35.0)
            synth.fill(synth.disks[name]["tps"], 10, 70, 100.0)
        synth.hardware = hardware_for(
            {"sda": {"class": "hdd", "parallelism": 1},
             "dm-0": {"class": "stacked", "slaves": ["sda"], "display": "rhel-root"}},
            extra={"part.sda1.parent": "sda", "part.sda2.parent": "sda"})
        synth.write()

    def test_partitions_are_not_analysed_and_are_noted(self):
        self._bundle()
        result = analyse_dir(self.tmp)
        subjects = {h.subject for h in result.hypotheses if h.id == "H1"}
        self.assertFalse(subjects & {"sda1", "sda2"})
        self.assertIn("sda", subjects)
        self.assertIn("partitions excluded from analysis (their parent disk is analysed): sda1, sda2",
                      result.host.notes)
        self.assertEqual(result.episodes_for("disk_await", "sda2"), [])

    def test_partition_names_fall_back_to_the_name_pattern_without_inventory(self):
        self._bundle()
        for entry in os.listdir(self.tmp):
            if "_hw_" in entry:
                shutil.rmtree(os.path.join(self.tmp, entry))
        result = analyse_dir(self.tmp)
        self.assertEqual(result.episodes_for("disk_await", "sda1"), [])

    def test_partition_whose_parent_has_no_data_is_kept(self):
        synth = self.make(samples=120, interval=10)
        synth.add_disk("sdb1")
        synth.fill(synth.disks["sdb1"]["await"], 10, 70, 35.0)
        synth.fill(synth.disks["sdb1"]["tps"], 10, 70, 100.0)
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertTrue(result.episodes_for("disk_await", "sdb1"))

    def test_include_partitions_judges_a_partition_as_its_disk(self):
        self._bundle()
        result = analyse_dir(self.tmp, include_partitions=True)
        episode = result.episodes_for("disk_await", "sda2")[0]
        self.assertEqual(episode.threshold["source"], "default:hdd")
        self.assertEqual(episode.threshold["warn"], 20)
        self.assertNotIn("partitions excluded", " ".join(result.host.notes))


class KernelEventTests(AnalysisCase):
    """F5: kernel events are listed apart from the measured hypotheses."""

    def _write(self, lines, hardware=None, extra_disks=(), saturate=None):
        synth = self.make(samples=120, interval=10)
        for name in extra_disks:
            synth.add_disk(name)
        if saturate:
            synth.fill(synth.disks[saturate]["await"], 10, 100, 25.0)
            synth.fill(synth.disks[saturate]["tps"], 10, 100, 300.0)
        synth.hardware = hardware or hardware_for({"vda": {"class": "virtual"}})
        synth.kernel_lines = lines
        synth.write()
        return analyse_dir(self.tmp)

    def test_removable_media_error_raises_nothing_and_is_noted(self):
        result = self._write(["2026-10-16T15:20:00+0000 db01 kernel: blk_update_request: I/O error, dev sr0, sector 0"])
        self.assertEqual(self.hypotheses(result, "H1"), [])
        self.assertEqual(self.hypotheses(result, "H11"), [])
        self.assertTrue([n for n in result.host.notes if "sr0" in n and "ignored" in n])

    def test_nvme_controller_timeout_becomes_h11_on_its_namespace(self):
        result = self._write(["2026-10-16T15:20:00+0000 db01 kernel: nvme nvme0: I/O 123 QID 4 timeout, aborting"],
                             extra_disks=("nvme0n1",))
        h11 = self.hypotheses(result, "H11")
        self.assertEqual([h.subject for h in h11], ["nvme0n1"])
        self.assertEqual(h11[0].confidence, "high")
        self.assertEqual(h11[0].origin, "kernel")
        self.assertEqual(self.hypotheses(result, "H1"), [])

    def test_error_without_a_device_is_unattributed_medium(self):
        result = self._write(["2026-10-16T15:20:00+0000 db01 kernel: Buffer I/O error on device, logical block 5"])
        h11 = self.hypotheses(result, "H11")
        self.assertEqual([(h.subject, h.confidence) for h in h11], [("unattributed device", "medium")])

    def test_partition_error_is_attributed_to_the_disk(self):
        result = self._write(["2026-10-16T15:20:00+0000 db01 kernel: Buffer I/O error on dev vda1, logical block 5"])
        self.assertEqual([h.subject for h in self.hypotheses(result, "H11")], ["vda"])

    def test_cgroup_oom_is_h12_not_h6(self):
        result = self._write([
            "2026-10-16T15:20:00+0000 db01 kernel: Memory cgroup out of memory: Killed process 4242 (java) total-vm:1kB"])
        h12 = self.hypotheses(result, "H12")
        self.assertEqual([h.subject for h in h12], ["java"])
        self.assertEqual(self.hypotheses(result, "H6"), [])
        self.assertTrue(any("memory.events" in step for step in h12[0].next_steps))

    def test_cgroup_oom_inside_measured_memory_pressure_is_noted_on_h6(self):
        synth = self.make(samples=120, interval=10)
        synth.fill(synth.swap_out, 10, 110, 400.0)
        synth.kernel_lines = [
            "2026-10-16T15:20:00+0000 db01 kernel: Memory cgroup out of memory: Killed process 4242 (java)"]
        synth.write()
        h6 = self.hypotheses(analyse_dir(self.tmp), "H6")
        self.assertEqual(len(h6), 1)
        self.assertIn("a cgroup OOM kill occurred in this window", h6[0].supporting_facts)

    def test_global_oom_is_h6_from_the_kernel_and_not_ranked(self):
        synth = self.make(samples=120, interval=10)
        synth.fill(synth.disks["vda"]["await"], 10, 100, 25.0)
        synth.fill(synth.disks["vda"]["tps"], 10, 100, 300.0)
        for key in synth.cpu_idle:
            synth.fill(synth.cpu_idle[key], 10, 100, 0.0)
        synth.hardware = hardware_for({"vda": {"class": "virtual"}})
        synth.kernel_lines = ["2026-10-16T15:20:00+0000 db01 kernel: Out of memory: Killed process 4242 (postgres)"]
        synth.write()
        result = analyse_dir(self.tmp)
        self.assertEqual([h.origin for h in result.hypotheses], ["measured", "measured", "kernel"])
        self.assertEqual(result.hypotheses[-1].id, "H6")
        text = findings.render_text(result, max_hypotheses=1)
        ranked, _, kernel_block = text.partition("Kernel log findings")
        self.assertNotIn("H6 Memory pressure", ranked)
        self.assertIn("H6 Memory pressure", kernel_block)
        self.assertIn("1 further hypotheses not shown: use --json FILE or the report.", ranked)
        self.assertIn("1 further hypotheses are in out.json.", findings.render_text(result, 1, "out.json"))

    def test_io_error_inside_a_latency_episode_raises_h1_high_and_lists_h11(self):
        result = self._write(["2026-10-16T15:20:00+0000 db01 kernel: blk_update_request: I/O error, dev vda, sector 2048"],
                             saturate="vda")
        h1 = self.hypotheses(result, "H1")[0]
        self.assertEqual(h1.confidence, "high")
        self.assertEqual(len(h1.kernel_events), 1)
        self.assertEqual([h.subject for h in self.hypotheses(result, "H11")], ["vda"])
        self.assertEqual(result.hypotheses[0].id, "H1")

    def test_oom_lines_for_one_kill_collapse(self):
        result = self._write([
            "2026-10-16T15:20:00+0000 db01 kernel: oom-kill:constraint=CONSTRAINT_NONE,task=postgres,pid=4242,uid=26",
            "2026-10-16T15:20:01+0000 db01 kernel: Out of memory: Killed process 4242 (postgres) total-vm:1kB"])
        h6 = self.hypotheses(result, "H6")
        self.assertEqual(len(h6), 1)
        self.assertEqual(len(h6[0].kernel_events), 1)
        self.assertEqual(h6[0].kernel_events[0].count, 2)

    def test_kernel_hypotheses_in_findings_json(self):
        result = self._write(["2026-10-16T15:20:00+0000 db01 kernel: Buffer I/O error on device, logical block 5"])
        document = findings.to_json(result)
        self.assertEqual(document["schema"], 1)
        self.assertEqual(document["hypotheses"][0]["origin"], "kernel")
        self.assertEqual(document["hypotheses"][0]["related"], [])


class StackedMergeTests(AnalysisCase):
    """F2: one hypothesis per physical problem, not per stacking layer."""

    def _dm_on_sda(self, slow):
        synth = self.make(samples=120, interval=10)
        for name in ("sda", "rhel-root"):
            if name not in synth.disks:
                synth.add_disk(name)
        for name in slow:
            synth.fill(synth.disks[name]["await"], 10, 100, 35.0)
            synth.fill(synth.disks[name]["tps"], 10, 100, 100.0)
            synth.fill(synth.disks[name]["wkB"], 10, 100, 50000.0)
        synth.fill(synth.dirty_kb, 10, 100, int(synth.mem_total_kb * 0.15))
        synth.hardware = hardware_for(
            {"sda": {"class": "virtual"}, "dm-0": {"class": "stacked", "slaves": ["sda"], "display": "rhel-root"}},
            extra={"fs.1.mountpoint": "/", "fs.1.source": "/dev/mapper/rhel-root", "fs.1.fstype": "xfs",
                   "fs.1.kname": "dm-0"})
        synth.write()
        return analyse_dir(self.tmp)

    def test_layer_over_one_slow_member_is_folded_into_the_member(self):
        result = self._dm_on_sda(["sda", "rhel-root"])
        for code in ("H1", "H7"):
            found = self.hypotheses(result, code)
            self.assertEqual([h.subject for h in found], ["sda"], code)
            self.assertEqual([device for device, _ in found[0].related], ["rhel-root"], code)
        text = findings.hypothesis_text(result, self.hypotheses(result, "H1")[0])
        self.assertIn("Also seen on: rhel-root (/), await averaged 35.0 ms over the same window.", text)
        document = findings.to_json(result)
        h1 = [h for h in document["hypotheses"] if h["id"] == "H1"][0]
        self.assertEqual(h1["related"][0]["device"], "rhel-root")
        self.assertTrue(h1["related"][0]["episode"].startswith("e"))

    def test_related_device_without_a_mount_shows_its_kernel_name(self):
        result = self._dm_on_sda(["sda", "rhel-root"])
        result.host.hardware.filesystems.clear()
        text = findings.hypothesis_text(result, self.hypotheses(result, "H1")[0])
        self.assertIn("Also seen on: rhel-root (dm-0), await averaged 35.0 ms", text)

    def test_raid_with_every_member_slow_keeps_the_array(self):
        synth = self.make(samples=120, interval=10)
        for name in ("md0", "sdb", "sdc"):
            if name not in synth.disks:
                synth.add_disk(name)
            synth.fill(synth.disks[name]["await"], 10, 100, 35.0)
            synth.fill(synth.disks[name]["tps"], 10, 100, 100.0)
            synth.fill(synth.disks[name]["wkB"], 10, 100, 50000.0)
        synth.fill(synth.dirty_kb, 10, 100, int(synth.mem_total_kb * 0.15))
        synth.hardware = hardware_for({
            "sdb": {"class": "ssd"}, "sdc": {"class": "ssd"},
            "md0": {"class": "stacked", "slaves": ["sdb", "sdc"]}})
        synth.write()
        result = analyse_dir(self.tmp)
        for code in ("H1", "H7"):
            found = self.hypotheses(result, code)
            self.assertEqual([h.subject for h in found], ["md0"], code)
            self.assertEqual(sorted(device for device, _ in found[0].related), ["sdb", "sdc"], code)

    def test_latency_added_above_a_normal_member_stays_on_the_layer(self):
        result = self._dm_on_sda(["rhel-root"])
        for code in ("H1", "H7"):
            found = self.hypotheses(result, code)
            self.assertEqual([h.subject for h in found], ["rhel-root"], code)
            self.assertEqual(found[0].related, [], code)

    def test_two_layers_fold_down_to_the_physical_disk(self):
        synth = self.make(samples=120, interval=10)
        for name in ("sda", "dm-0", "luks-1"):
            if name not in synth.disks:
                synth.add_disk(name)
            synth.fill(synth.disks[name]["await"], 10, 100, 35.0)
            synth.fill(synth.disks[name]["tps"], 10, 100, 100.0)
        synth.hardware = hardware_for({
            "sda": {"class": "virtual"}, "dm-0": {"class": "stacked", "slaves": ["sda"]},
            "luks-1": {"class": "stacked", "slaves": ["dm-0"]}})
        synth.hardware["disk.luks-1.display"] = ""
        synth.write()
        h1 = self.hypotheses(analyse_dir(self.tmp), "H1")
        self.assertEqual([h.subject for h in h1], ["sda"])
        self.assertEqual(sorted(device for device, _ in h1[0].related), ["dm-0", "luks-1"])


class PlateauRoleTests(AnalysisCase):
    """F10: plateaus on local disks are evidence for H1, not findings of their own."""

    def _hdd(self, media_class):
        synth = self.make(samples=120, interval=10)
        disk = synth.disks["vda"]
        synth.fill(disk["await"], 30, 80, 35.0)          # a minority of the run, so the run median stays low
        synth.fill(disk["tps"], 30, 80, 300.0)
        synth.fill(disk["wkB"], 30, 80, 80000.0)
        synth.hardware = hardware_for({"vda": {"class": media_class, "parallelism": 1}})
        synth.write()
        return analyse_dir(self.tmp)

    def test_plateau_on_a_local_disk_is_supporting_evidence_for_h1(self):
        result = self._hdd("hdd")
        plateaus = result.episodes_for("disk_plateau", "vda")
        self.assertTrue(plateaus)
        self.assertEqual({e.role for e in plateaus}, {"supporting"})
        h1 = self.hypotheses(result, "H1")[0]
        referenced = [e for e in h1.supporting if e.rule == "disk_plateau"]
        self.assertEqual(len(referenced), 1)
        self.assertTrue(any("throughput was flat at" in fact and "the device was working at its limit" in fact
                            for fact in h1.supporting_facts))
        self.assertEqual(self.hypotheses(result, "H2"), [])

    def test_timeline_keeps_referenced_plateaus_and_drops_the_rest(self):
        result = self._hdd("hdd")
        referenced = [e for e in self._h1(result).supporting if e.rule == "disk_plateau"][0]
        unreferenced = [e for e in result.episodes_for("disk_plateau", "vda") if e is not referenced]
        self.assertTrue(unreferenced)
        drawn = charts.timeline_episodes(result)
        self.assertIn(referenced, drawn)
        for episode in unreferenced:
            self.assertNotIn(episode, drawn)

    def _h1(self, result):
        return self.hypotheses(result, "H1")[0]

    def test_plateau_values_carry_units(self):
        result = self._hdd("hdd")
        throughput = [e for e in result.episodes_for("disk_plateau", "vda") if e.threshold["unit"] == "kB/s"][0]
        self.assertIn("78.1 MiB/s", throughput.notes[0])
        text = findings.render_text(result)
        self.assertIn("MiB/s", text.split("Sustained episodes")[1])
        self.assertIn("[supporting]", text)

    def test_plateau_on_a_virtual_disk_stays_primary(self):
        result = self._hdd("virtual")
        self.assertEqual({e.role for e in result.episodes_for("disk_plateau", "vda")}, {"primary"})


class AttributionTests(unittest.TestCase):
    """F8: processes are grouped by program, and thread rows are not double counted."""

    def host_with(self, rows):
        host = HostData(host="h")
        host.process_rows = rows
        return host

    @staticmethod
    def row(time, pid, command, cpu=0.0, wr=0.0, rd=0.0, tgid=None, is_thread=False):
        return ProcessRow(time=time, pid=pid, cpu_pct=cpu, rss_kb=0.0, majflt_ps=0.0, rd_kBps=rd, wr_kBps=wr,
                          iodelay=0.0, command=command, tgid=tgid if tgid is not None else pid, is_thread=is_thread)

    def test_process_key_table(self):
        expected = {
            "postgres: app db 10.0.0.5(51234) idle": "postgres",
            "/usr/sbin/mysqld --defaults-file=/etc/my.cnf": "mysqld",
            "python3 /opt/app/worker.py --queue a": "python3 worker.py",
            "java -Xmx4g -cp /x.jar com.acme.Main": "java com.acme.Main",
            "java -jar /opt/app.jar": "java app.jar",
            "kworker/0:1H-kblockd": "kworker",
            "[kthreadd]": "kthreadd",
            "|__java": "java",
            "python3": "python3",
            "": "",
        }
        for command, key in expected.items():
            self.assertEqual(attribution.process_key(command), key, command)

    def test_many_backends_with_distinct_titles_group_into_one_entry(self):
        rows = []
        for time_value in (10, 20, 30):
            for pid, title in ((101, "postgres: app db 10.0.0.5(51234) idle"),
                               (102, "postgres: app db 10.0.0.6(40001) UPDATE"),
                               (103, "postgres: checkpointer")):
                rows.append(self.row(time_value, pid, title, wr=20 * 1024.0))
            rows.append(self.row(time_value, 200, "rsync -a /src /dst", wr=30 * 1024.0))
        entries = attribution.top_processes(self.host_with(rows), 0, 100, "disk")
        self.assertEqual(entries[0]["command"], "postgres")
        self.assertEqual(entries[0]["processes"], 3)
        self.assertAlmostEqual(entries[0]["wr_kBps"] / 1024.0, 60.0, places=3)
        self.assertEqual(entries[1]["command"], "rsync")
        self.assertEqual(entries[1]["processes"], 1)
        self.assertEqual(entries[1]["example"], "rsync -a /src /dst")

    def test_text_names_the_group_and_its_largest_member(self):
        rows = []
        for time_value in (10, 20):
            rows.append(self.row(time_value, 101, "postgres: checkpointer", wr=60 * 1024.0))
            rows.append(self.row(time_value, 102, "postgres: app db idle", wr=10 * 1024.0))
        entries = attribution.top_processes(self.host_with(rows), 0, 100, "disk")
        hypothesis = analysis_hypothesis(entries)
        text = findings.process_lines(hypothesis)[0]
        self.assertEqual(text, "postgres 70.0 MiB/s writes (100 % of total process IO; 2 processes, largest postgres: checkpointer 60.0 MiB/s)")
        single = analysis_hypothesis(attribution.top_processes(
            self.host_with([self.row(10, 5, "rsync -a /src /dst", wr=1024.0)]), 0, 100, "disk"))
        self.assertTrue(findings.process_lines(single)[0].startswith("rsync -a /src /dst 1.00 MiB/s writes"))
        self.assertNotIn("processes", findings.process_lines(single)[0])

    def test_thread_rows_are_not_added_to_their_process(self):
        rows = []
        for time_value in (10, 20):
            rows.append(self.row(time_value, 1234, "java -jar /opt/app.jar", cpu=100.0))
            rows.append(self.row(time_value, 1235, "|__java", cpu=50.0, tgid=1234, is_thread=True))
            rows.append(self.row(time_value, 1236, "|__java", cpu=50.0, tgid=1234, is_thread=True))
        entries = attribution.top_processes(self.host_with(rows), 0, 100, "cpu")
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0]["cpu_pct"], 100.0)

    def test_single_hot_process_names_the_hot_thread(self):
        rows = []
        for time_value in (10, 20):
            rows.append(self.row(time_value, 1234, "java -jar /opt/app.jar", cpu=100.0))
            rows.append(self.row(time_value, 1235, "|__GC Thread", cpu=97.0, tgid=1234, is_thread=True))
            rows.append(self.row(time_value, 1236, "|__main", cpu=3.0, tgid=1234, is_thread=True))
        hot = attribution.single_hot_process(self.host_with(rows), 0, 100)
        self.assertEqual(hot["command"], "thread |__GC Thread (TGID 1234)")
        self.assertAlmostEqual(hot["cpu_pct"], 97.0)

    def test_single_hot_process_without_thread_rows_is_unchanged(self):
        rows = [self.row(10, 7, "python3 /opt/x.py --a", cpu=95.0), self.row(20, 7, "python3 /opt/x.py --a", cpu=95.0)]
        hot = attribution.single_hot_process(self.host_with(rows), 0, 100)
        self.assertEqual(hot["command"], "python3 x.py")


def analysis_hypothesis(entries):
    from perfdiag.model import Hypothesis
    return Hypothesis(id="H1", title="t", subject="vda", confidence="high", primary=None, top_processes=entries)


class RecurringTests(AnalysisCase):
    """F7: recurring bursts need to add up, and read correctly over several days."""

    def _cpu_bursts(self, count, spacing=10):
        synth = self.make(samples=2880, interval=60)            # 2 days at 60 s
        for index in range(count):
            at = 10 + index * spacing
            for key in synth.cpu_idle:
                synth.cpu_idle[key][at] = 1.0
            synth.psi_cpu[at] = 60.0                             # the burst mattered
        synth.write()
        return analyse_dir(self.tmp)

    def test_three_blips_over_two_days_are_not_a_recurring_finding(self):
        result = self._cpu_bursts(3)
        self.assertEqual(self.hypotheses(result, "H3"), [])
        self.assertTrue(result.impactful)

    def test_many_bursts_are_recurring_and_read_below_with_dates(self):
        result = self._cpu_bursts(200)
        h3 = self.hypotheses(result, "H3")
        self.assertEqual(len(h3), 1)
        self.assertTrue(h3[0].recurring)
        text = findings.hypothesis_text(result, h3[0])
        self.assertRegex(text, r"all-CPU idle fell below 20 % in 200 bursts \(total 3 h 20 min, longest 60 s\)")
        self.assertNotIn("exceeded", text.split("Next steps")[0])
        dates = __import__("re").findall(r"\d{4}-\d{2}-\d{2}", text.split("Next steps")[0])
        self.assertGreaterEqual(len(set(dates)), 2)
        self.assertRegex(text, r"Worst burst: \d{4}-\d{2}-\d{2} \d\d:\d\d to \d\d:\d\d \(60 s\), lowest 1\.00 %, 1 of 1 samples below the threshold\.")
        self.assertNotIn("averaged 43", text)

    def test_recurring_threshold_is_a_setting(self):
        result = analyse_dir_with(self, 20)
        self.assertEqual(self.hypotheses(result, "H3"), [])
        settings = self.settings(analysis__recurring_min_pct=0.1)
        self.assertTrue(self.hypotheses(analyse_dir(self.tmp, settings), "H3"))

    def test_above_rules_still_say_exceeded(self):
        synth = self.make(samples=2880, interval=60)
        synth.hardware = hardware_for({"vda": {"class": "nvme"}})
        for index in range(200):
            at = 10 + index * 10
            synth.disks["vda"]["await"][at] = 50.0
            synth.disks["vda"]["tps"][at] = 200.0
        synth.write()
        result = analyse_dir(self.tmp)
        h1 = self.hypotheses(result, "H1")
        self.assertEqual(len(h1), 1)
        text = findings.hypothesis_text(result, h1[0])
        self.assertIn("vda await exceeded 2 ms in 200 bursts", text)

    def test_range_text_adds_dates_only_when_needed(self):
        from perfdiag.timeutil import UTC, range_text
        day = 1792163600
        self.assertEqual(range_text(UTC, day, day + 600, day, day + 3600), ("15:13", "15:23"))
        self.assertEqual(range_text(UTC, day, day + 600, day, day + 3 * 86400),
                         ("2026-10-16 15:13", "15:23"))
        self.assertEqual(range_text(UTC, day, day + 86400, day, day + 2 * 3600),
                         ("2026-10-16 15:13", "2026-10-17 15:13"))


def analyse_dir_with(case, bursts):
    """Same bundle as RecurringTests._cpu_bursts with a different burst count, in a clean directory."""
    shutil.rmtree(case.tmp)
    os.makedirs(case.tmp)
    return case._cpu_bursts(bursts)


class TimeZoneTests(AnalysisCase):
    """F12: a named zone follows daylight-saving changes."""

    UK_CHANGE_START = 1792882800          # 2026-10-24 23:00 UTC; the UK clocks go back at 01:00 UTC on the 25th

    def _bundle(self, **kwargs):
        synth = self.make(samples=240, interval=60, start=self.UK_CHANGE_START - 60, **kwargs)
        for key in synth.cpu_idle:
            synth.fill(synth.cpu_idle[key], 90, 150, 0.0)       # 00:30 to 01:30 UTC
        synth.write()
        return analyse_dir(self.tmp)

    def test_times_follow_the_change(self):
        result = self._bundle(timezone="BST +0100", tz_name="Europe/London")
        zone = result.host.zone
        at_0030 = self.UK_CHANGE_START + 90 * 60
        at_0230 = self.UK_CHANGE_START + 210 * 60
        self.assertEqual(clock_zone(at_0030, zone), "01:30 BST")
        self.assertEqual(clock_zone(at_0230, zone), "02:30 GMT")
        self.assertEqual(zone_label(zone, at_0030), "BST +0100")
        self.assertEqual(zone_label(zone, at_0230), "GMT +0000")
        self.assertEqual(zone_axis_label(zone, at_0230), "Europe/London")
        self.assertIn("the window crosses a daylight-saving change on 2026-10-25; times are shown in local time on "
                      "each side of it", result.host.notes)

    def test_a_finding_across_the_change_shows_each_side(self):
        result = self._bundle(timezone="BST +0100", tz_name="Europe/London")
        text = findings.render_text(result)
        self.assertRegex(text, r"Between 01:\d\d BST and 01:\d\d GMT")

    def test_without_tz_name_nothing_changes(self):
        result = self._bundle(timezone="BST +0100")
        self.assertEqual(zone_label(result.host.zone), "BST +0100")
        self.assertFalse([n for n in result.host.notes if "daylight" in n])
        self.assertEqual(clock_zone(self.UK_CHANGE_START + 210 * 60, result.host.zone), "03:30 BST")

    def test_unknown_zone_name_falls_back_to_the_fixed_offset_with_a_note(self):
        result = self._bundle(timezone="BST +0100", tz_name="Not/AZone")
        self.assertEqual(zone_label(result.host.zone), "BST +0100")
        self.assertIn("time zone Not/AZone is not in this machine's time zone database; using the fixed offset "
                      "+0100 (pip install tzdata)", result.host.notes)

    def test_window_inside_one_offset_has_no_change_note(self):
        synth = self.make(samples=120, interval=10, timezone="BST +0100", tz_name="Europe/London")
        synth.write()
        self.assertFalse([n for n in analyse_dir(self.tmp).host.notes if "daylight" in n])

    def test_from_to_cut_the_change_out_of_the_note(self):
        synth = self.make(samples=240, interval=60, start=self.UK_CHANGE_START - 60, timezone="BST +0100",
                          tz_name="Europe/London")
        synth.write()
        with bundle.open_bundle(self.tmp) as opened:
            host = loader.load_host(opened, "db01")
            self.assertTrue([n for n in host.notes if "daylight" in n])
            loader.apply_window(host, self.UK_CHANGE_START + 150 * 60, None)
            self.assertFalse([n for n in host.notes if "daylight" in n])


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
        # The columns line up: available is free + buffers + cached, dirty is the kbdirty column.
        self.assertEqual(host.get("mem.avail_kB").values[0], int(synth.mem_total_kb * 0.8))
        self.assertEqual(host.get("mem.dirty_kB").values[0], 1000)

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
