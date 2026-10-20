"""Reader and bundle tests."""

import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perfdiag import bundle, canonical, deps  # noqa: E402
from perfdiag.bundle import BundleError  # noqa: E402
from perfdiag.model import HostData  # noqa: E402
from perfdiag.readers import kernel_log, pidstat, psar, sadf  # noqa: E402
from perfdiag.readers.manifest import parse_hardware  # noqa: E402
from perfdiag.timeutil import (TimeError, UTC, offset_zone, parse_duration, parse_iso, parse_when,  # noqa: E402
                               zone_from_manifest, zone_label)
from helpers import write_bytes, write_text  # noqa: E402

HERE = os.path.dirname(__file__)


class SadfReaderTests(unittest.TestCase):
    TWO_BLOCKS = (
        "# hostname;interval;timestamp;CPU;%usr;%idle\n"
        "vm;1;1000;-1;5.00;95.00\n"
        "vm;1;1000;0;10.00;90.00\n"
        "# hostname;interval;timestamp;DEV;tps;await\n"
        "vm;1;1001;vda;2.00;0.50\n"
    )

    def test_header_resets_columns_and_cpu_minus_one_is_all(self):
        blocks = sadf.parse_sadf_text(self.TWO_BLOCKS)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].key_name, "CPU")
        self.assertEqual(blocks[0].columns, ["%usr", "%idle"])
        self.assertEqual([row[2] for row in blocks[0].rows], ["all", "0"])
        self.assertEqual(blocks[1].key_name, "DEV")
        self.assertEqual(blocks[1].rows[0][3], [2.0, 0.5])

    def test_no_key_column(self):
        blocks = sadf.parse_sadf_text("# hostname;interval;timestamp;runq-sz;blocked\nvm;5;100;3;1\n")
        self.assertIsNone(blocks[0].key_name)
        self.assertEqual(blocks[0].rows[0], (100, 5.0, "", [3.0, 1.0]))

    def test_bad_values_become_nan(self):
        blocks = sadf.parse_sadf_text("# hostname;interval;timestamp;a\nvm;1;100;-nan\n")
        self.assertNotEqual(blocks[0].rows[0][3][0], blocks[0].rows[0][3][0])

    def test_real_capture_parses(self):
        directory = os.path.join(HERE, "fixtures", "real_96s", "vm_os_20261016_215025", "sadf")
        tables = sadf.read_sadf_directory(directory)
        for stem in ("cpu", "disk", "load", "memory", "psi_io"):
            self.assertIn(stem, tables)
        self.assertEqual(len(tables["load"][0].rows), 96)


class CanonicalTests(unittest.TestCase):
    def test_psar_disk_columns(self):
        columns = ["r_s", "w_s", "rkB_s", "wkB_s", "rareq-sz", "wareq-sz", "r_await", "w_await", "aqu-sz", "util_pct"]
        rows = [(100 + i, "vda", dict(zip(columns, [10.0, 5.0, 100.0, 50.0, 8.0, 4.0, 2.0, 3.0, 0.5, 20.0]))) for i in range(3)]
        series = {(s.metric, s.device): s for s in canonical.series_from_psar({"disk": (columns, rows)}, 5)}
        self.assertEqual(series[("disk.tps", "vda")].values[0], 15.0)
        self.assertEqual(series[("disk.r_await_ms", "vda")].values[0], 2.0)
        self.assertEqual(series[("disk.write_kBps", "vda")].source, "psar")

    def test_psar_cpu_is_device_all(self):
        columns = ["idle_pct", "run", "blkd", "cs_s"]
        rows = [(100, "", {"idle_pct": 90.0, "run": 2.0, "blkd": 1.0, "cs_s": 500.0})]
        series = {(s.metric, s.device) for s in canonical.series_from_psar({"cpu": (columns, rows)}, 5)}
        self.assertIn(("cpu.idle_pct", "all"), series)
        # host-wide series carry an empty device, as in the sadf export
        self.assertIn(("load.runq", ""), series)
        self.assertIn(("load.blocked", ""), series)
        self.assertIn(("cpu.cswch_ps", ""), series)

    def test_loopback_interface_skipped_in_psar_and_sadf(self):
        columns = ["rxkB_s", "txkB_s"]
        rows = [(100, "lo", {"rxkB_s": 1.0, "txkB_s": 1.0}), (100, "eth0", {"rxkB_s": 2.0, "txkB_s": 2.0})]
        devices = {s.device for s in canonical.series_from_psar({"net": (columns, rows)}, 5)}
        self.assertEqual(devices, {"eth0"})
        blocks = sadf.parse_sadf_text("# hostname;interval;timestamp;IFACE;rxkB/s;txkB/s\nvm;1;100;lo;1;1\nvm;1;100;eth0;2;2\n")
        host = HostData(host="vm")
        devices = {s.device for s in canonical.series_from_sadf({"net_dev": blocks}, 1, host)}
        self.assertEqual(devices, {"eth0"})


class PsarReaderTests(unittest.TestCase):
    def test_reads_csv_with_offset_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "disk.csv"), "w") as handle:
                handle.write("ts,device,r_s,w_s,rkB_s,wkB_s\n")
                handle.write("2026-10-16T14:57:49+01:00,vda,1.0,2.0,3.0,4.0\n")
                handle.write("2026-10-16T14:57:54+01:00,vda,1.5,2.5,3.5,4.5\n")
            tables = psar.read_psar_directory(directory)
        columns, rows = tables["disk"]
        self.assertEqual(rows[1][0] - rows[0][0], 5)
        self.assertEqual(rows[0][1], "vda")
        self.assertEqual(rows[0][0], parse_iso("2026-10-16T13:57:49+00:00"))


class PidstatReaderTests(unittest.TestCase):
    TEXT = (
        "Linux 6.1 (db01) \t2026-10-16 \t_x86_64_\t(2 CPU)\n\n"
        "# Time        UID       PID    %usr %system  %guest   %wait    %CPU   CPU  minflt/s  majflt/s     VSZ     RSS   %MEM   kB_rd/s   kB_wr/s kB_ccwr/s iodelay   cswch/s nvcswch/s  Command\n"
        "1792186348      0       100    1.00    0.00    0.00    0.00    1.00     0      0.00      2.00    1000     500   0.01     10.00     90.00      0.00       3      1.00      0.00  postgres: checkpointer process\n"
        "1792186348      0       101    0.50    0.00    0.00    0.00    0.50     1      0.00      0.00    1000     100   0.01      0.00      0.00      0.00       0      1.00      0.00  sshd -D\n"
    )

    def test_fixed_columns_then_command(self):
        rows = pidstat.parse_pidstat_text(self.TEXT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].command, "postgres: checkpointer process")
        self.assertEqual(rows[0].wr_kBps, 90.0)
        self.assertEqual(rows[0].rss_kb, 500.0)
        self.assertEqual(rows[0].pid, 100)

    def test_command_truncated_to_sixty_characters(self):
        long_command = "x" * 100
        text = self.TEXT.replace("sshd -D", long_command)
        self.assertEqual(len(pidstat.parse_pidstat_text(text)[1].command), 60)

    def test_clock_times_need_zone_and_date(self):
        text = self.TEXT.replace("1792186348", "10:00:00")
        self.assertEqual(pidstat.parse_pidstat_text(text), [])
        rows = pidstat.parse_pidstat_text(text, UTC)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].time % 86400, 10 * 3600)

    def test_midnight_rollover(self):
        text = (self.TEXT.split("# Time")[0] + "# Time UID PID %CPU Command\n"
                "23:59:59 0 1 5.0 a\n00:00:01 0 1 5.0 a\n")
        rows = pidstat.parse_pidstat_text(text, UTC)
        self.assertEqual(rows[1].time - rows[0].time, 2)


class PidstatThreadTests(unittest.TestCase):
    HEADER = ("# Time UID TGID TID %usr %system %guest %wait %CPU CPU minflt/s majflt/s VSZ RSS %MEM "
              "kB_rd/s kB_wr/s kB_ccwr/s iodelay cswch/s nvcswch/s Command\n")

    @staticmethod
    def row(tgid, tid, cpu, command):
        return "1792186348 0 %s %s 1.0 0.0 0.0 0.0 %.1f 0 0.0 0.0 1000 500 0.01 0.0 0.0 0.0 0 1.0 0.0 %s\n" % (
            tgid, tid, cpu, command)

    def test_process_and_thread_rows(self):
        text = (self.HEADER + self.row(1234, "-", 100.0, "java -Xmx4g -jar /opt/app.jar") +
                self.row("-", 1235, 50.0, "|__java") + self.row("-", 1236, 50.0, "|__java") +
                self.row(2000, "-", 1.0, "sshd -D"))
        rows = pidstat.parse_pidstat_text(text)
        self.assertEqual([(r.pid, r.tgid, r.is_thread) for r in rows],
                         [(1234, 1234, False), (1235, 1234, True), (1236, 1234, True), (2000, 2000, False)])
        self.assertEqual(rows[1].command, "|__java")

    def test_header_without_tgid_keeps_pid_as_tgid(self):
        rows = pidstat.parse_pidstat_text(PidstatReaderTests.TEXT)
        self.assertEqual([(r.pid, r.tgid, r.is_thread) for r in rows], [(100, 100, False), (101, 101, False)])


class KernelLogTests(unittest.TestCase):
    def test_formats_and_categories(self):
        text = "\n".join([
            "# source=journalctl",
            "2026-10-16T14:57:49+0100 db01 kernel: Out of memory: Killed process 1234 (java)",
            "2026-10-16T14:58:49.123456+01:00 db01 kernel: nvme nvme1: I/O 12 QID 3 timeout, reset controller",
            "Oct 16 15:00:00 db01 kernel: blk_update_request: I/O error, dev sdb, sector 8",
            "Oct 16 15:01:00 db01 kernel: INFO: task jbd2/dm-3-8:421 blocked for more than 120 seconds.",
            "Oct 16 15:02:00 db01 kernel: unrelated line about nothing",
        ])
        source, events = kernel_log.parse_kernel_log(text, offset_zone("+0100"), 2026, now_epoch=2000000000)
        self.assertEqual(source, "journalctl")
        self.assertEqual([e.category for e in events], ["oom", "timeout_reset", "io_error", "hung_task"])
        self.assertEqual(events[2].device, "sdb")
        self.assertEqual(events[3].device, "dm-3")
        self.assertEqual(events[0].time, parse_iso("2026-10-16T14:57:49+01:00"))
        self.assertEqual(events[2].time, parse_iso("2026-10-16T15:00:00+01:00"))

    def test_nvme_controller_name_is_the_device(self):
        text = "2026-10-16T14:58:49+01:00 db01 kernel: nvme nvme1: I/O 12 QID 3 timeout, reset controller\n"
        _, events = kernel_log.parse_kernel_log(text, UTC, 2026)
        self.assertEqual(events[0].device, "nvme1")

    def test_device_extraction(self):
        expected = {
            "blk_update_request: I/O error, dev sr0, sector 0": "sr0",
            "blk_update_request: I/O error, dev loop3, sector 0": "loop3",
            "Buffer I/O error on dev sda1, logical block 9": "sda1",
            "EXT4-fs error (device nvme0n1p3): bad": "nvme0n1p3",
            "I/O error, dev mmcblk0p1": "mmcblk0p1",
            "sdhci-pci 0000:00:1f.0: I/O error timeout": "",
            "Buffer I/O error on device, logical block 5": "",
        }
        for text, device in expected.items():
            self.assertEqual(kernel_log.device_in(text), device, text)

    def test_ignored_devices(self):
        text = "\n".join(["2026-10-16T14:58:49+01:00 db01 kernel: blk_update_request: I/O error, dev sr0, sector 0",
                          "2026-10-16T14:58:50+01:00 db01 kernel: blk_update_request: I/O error, dev sda, sector 0"])
        _, events = kernel_log.parse_kernel_log(text, UTC, 2026)
        self.assertEqual([e.ignored for e in events], [True, False])

    def test_cgroup_oom_category_is_separate(self):
        self.assertEqual(kernel_log.categorise("Memory cgroup out of memory: Killed process 1 (a)"), "oom_cgroup")
        self.assertEqual(kernel_log.categorise("oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null)"), "oom_cgroup")
        self.assertEqual(kernel_log.categorise("Out of memory: Killed process 1 (a)"), "oom")

    def test_one_kill_printed_on_several_lines_is_one_event(self):
        text = "\n".join([
            "2026-10-16T14:58:49+01:00 db01 kernel: oom-kill:constraint=CONSTRAINT_NONE,task=postgres,pid=4242,uid=26",
            "2026-10-16T14:58:50+01:00 db01 kernel: Out of memory: Killed process 4242 (postgres) total-vm:1kB",
            "2026-10-16T14:58:51+01:00 db01 kernel: oom_reaper: reaped process 4242 (postgres), now anon-rss:0kB",
            "2026-10-16T14:59:51+01:00 db01 kernel: Out of memory: Killed process 5000 (redis) total-vm:1kB"])
        _, events = kernel_log.parse_kernel_log(text, UTC, 2026)
        self.assertEqual([(e.count) for e in events], [3, 1])
        self.assertIn("(postgres)", events[0].text)

    def test_identical_lines_within_five_seconds_collapse(self):
        line = "kernel: Buffer I/O error on dev sda, logical block 9"
        text = "\n".join(["2026-10-16T14:58:49+01:00 db01 " + line, "2026-10-16T14:58:52+01:00 db01 " + line,
                          "2026-10-16T14:59:52+01:00 db01 " + line])
        _, events = kernel_log.parse_kernel_log(text, UTC, 2026)
        self.assertEqual([e.count for e in events], [2, 1])

    def test_dmesg_uses_boot_epoch(self):
        text = "# source=dmesg\n# boot_epoch=1000\n[  12.500000] EXT4-fs error (device sda1): bad\n[ 20.0] ext4-fs error: x\n"
        _, events = kernel_log.parse_kernel_log(text, UTC, 2026)
        self.assertEqual([e.time for e in events], [1012, 1020])

    def test_december_line_in_january_belongs_to_previous_year(self):
        january_first = parse_iso("2027-01-01T00:00:00+00:00")
        _, events = kernel_log.parse_kernel_log("Dec 31 23:59:00 h kernel: Out of memory\n", UTC, 2027, january_first)
        self.assertEqual(events[0].time, parse_iso("2026-12-31T23:59:00+00:00"))


class HardwareParseTests(unittest.TestCase):
    def test_disks_filesystems_and_lookup_by_display(self):
        raw = {
            "cpu.logical": "8", "mem.total_kb": "1000",
            "disk.nvme1n1.class": "cloud-ebs", "disk.nvme1n1.size_bytes": "1000", "disk.nvme1n1.holders": "dm-2",
            "disk.dm-2.class": "stacked", "disk.dm-2.display": "pg-data", "disk.dm-2.slaves": "nvme1n1",
            "disk.cciss/c0d0.class": "hw-raid", "disk.cciss/c0d0.parallelism": "",
            "fs.1.mountpoint": "/var/lib/pgsql", "fs.1.source": "/dev/mapper/pg-data", "fs.1.fstype": "xfs", "fs.1.kname": "dm-2",
        }
        hardware = parse_hardware(raw)
        self.assertEqual(sorted(hardware.disks), ["cciss/c0d0", "dm-2", "nvme1n1"])
        self.assertEqual(hardware.find_disk("pg-data").kname, "dm-2")
        self.assertEqual(hardware.disks["dm-2"].slaves, ["nvme1n1"])
        self.assertIsNone(hardware.disks["cciss/c0d0"].parallelism)
        self.assertEqual([fs.mountpoint for fs in hardware.mounts_on("nvme1n1")], ["/var/lib/pgsql"])
        self.assertEqual(hardware.cpu_logical, 8)


class PartitionTests(unittest.TestCase):
    def test_parse_hardware_reads_partition_parents(self):
        hardware = parse_hardware({"disk.sda.class": "hdd", "part.sda1.parent": "sda", "part.cciss/c0d0p1.parent": "cciss/c0d0"})
        self.assertEqual(hardware.parent_of("sda1"), "sda")
        self.assertEqual(hardware.parent_of("cciss/c0d0p1"), "cciss/c0d0")
        self.assertIsNone(hardware.parent_of("sda"))
        self.assertEqual(sorted(hardware.disks), ["sda"])

    def test_partition_parent_by_name_without_hardware(self):
        from perfdiag.loader import partition_parent
        expected = {"sdb3": "sdb", "xvda1": "xvda", "nvme0n1p2": "nvme0n1", "mmcblk0p1": "mmcblk0",
                    "md0p1": "md0", "md127": None, "rhel-root": None, "dm-3": None, "sda": None, "nvme0n1": None}
        for name, parent in expected.items():
            self.assertEqual(partition_parent(name, None), parent, name)

    def test_inventory_wins_over_the_name_pattern(self):
        from perfdiag.loader import partition_parent
        hardware = parse_hardware({"part.weird1.parent": "weird"})
        self.assertEqual(partition_parent("weird1", hardware), "weird")


class TimeTests(unittest.TestCase):
    def test_zone_from_manifest(self):
        zone = zone_from_manifest("BST +0100")
        self.assertEqual(zone_label(zone), "BST +0100")
        self.assertEqual(zone_label(zone_from_manifest("UTC +0000")), "UTC")
        self.assertEqual(zone_from_manifest(""), UTC)

    def test_parse_when_forms(self):
        zone = offset_zone("+01:00")
        anchor = parse_iso("2026-10-16T09:00:00+01:00")
        self.assertEqual(parse_when("10:30", zone, anchor), parse_iso("2026-10-16T10:30:00+01:00"))
        self.assertEqual(parse_when("2026-10-17 01:05", zone, anchor), parse_iso("2026-10-17T01:05:00+01:00"))
        self.assertEqual(parse_when("1792166062", zone, anchor), 1792166062)
        with self.assertRaises(TimeError):
            parse_when("lunchtime", zone, anchor)

    def test_durations_and_zones(self):
        self.assertEqual(parse_duration("10m"), 600)
        self.assertEqual(parse_duration("45"), 45)
        with self.assertRaises(TimeError):
            parse_duration("soon")
        with self.assertRaises(TimeError):
            offset_zone("Mars/Base")


def make_tar(path, members):
    """members: list of (name, bytes_or_None, type) with type in 'file', 'dir', 'symlink'."""
    with tarfile.open(path, "w:gz") as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))


MANIFEST = b"script=collect-os-metrics.sh\nhostname=db01\ncpus=2\ninterval_s=10\n"


class BundleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-bundle-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_t13_parent_directory_member_refused_before_extraction(self):
        path = os.path.join(self.tmp, "evil.tar.gz")
        make_tar(path, [("host_gather_1/", None, "dir"), ("host_gather_1/../../escaped.txt", b"x", "file")])
        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaises(BundleError) as caught:
            with bundle.open_bundle(path):
                pass
        self.assertIn("climbs out", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escaped.txt")))
        leftovers = [n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith("perfdiag-") and "bundle" not in n]
        self.assertEqual(leftovers, [])

    def test_absolute_member_symlink_and_fifo_refused(self):
        for member in (("/etc/evil", b"x", "file"), ("link", None, "symlink"), ("pipe", None, "fifo")):
            path = os.path.join(self.tmp, "bad.tar.gz")
            make_tar(path, [member])
            with self.assertRaises(BundleError, msg=member[0]):
                with bundle.open_bundle(path):
                    pass

    def test_zip_member_outside_target_refused(self):
        path = os.path.join(self.tmp, "evil.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("../escaped.txt", "x")
        with self.assertRaises(BundleError):
            with bundle.open_bundle(path):
                pass

    def test_good_tarball_opens_and_is_cleaned_up(self):
        path = os.path.join(self.tmp, "ok.tar.gz")
        make_tar(path, [
            ("db01_gather_1/", None, "dir"), ("db01_gather_1/db01_os_20261016_100000/", None, "dir"),
            ("db01_gather_1/db01_os_20261016_100000/manifest.txt", MANIFEST, "file"),
        ])
        with bundle.open_bundle(path) as opened:
            root = opened.root
            self.assertEqual(opened.hosts(), ["db01"])
            self.assertTrue(os.path.isdir(root))
        self.assertFalse(os.path.exists(root))

    def test_keep_extracted(self):
        path = os.path.join(self.tmp, "ok.tar.gz")
        make_tar(path, [("db01_os_20261016_100000/manifest.txt", MANIFEST, "file")])
        notes = []
        with bundle.open_bundle(path, keep_extracted=True, announce=notes.append) as opened:
            root = opened.root
        self.assertTrue(os.path.isdir(root))
        self.assertTrue(notes)
        __import__("shutil").rmtree(os.path.dirname(root) if os.path.basename(root).startswith("db01_os") else root)

    def test_directory_forms(self):
        run = os.path.join(self.tmp, "db01_os_20261016_100000")
        os.makedirs(run)
        write_bytes(os.path.join(run, "manifest.txt"), MANIFEST)
        with bundle.open_bundle(run) as single:
            self.assertEqual(len(single.runs), 1)
        with bundle.open_bundle(self.tmp) as parent:
            self.assertEqual(len(parent.runs), 1)

    def test_unknown_type_and_missing_path(self):
        path = os.path.join(self.tmp, "notes.txt")
        write_text(path, "hello")
        with self.assertRaises(BundleError):
            with bundle.open_bundle(path):
                pass
        with self.assertRaises(BundleError):
            with bundle.open_bundle(os.path.join(self.tmp, "nope.tar.gz")):
                pass


class DepsTests(unittest.TestCase):
    def test_platform_detection(self):
        self.assertEqual(deps.detect_platform({"ID": "fedora"}, "6.1.0", "linux"), "fedora")
        self.assertEqual(deps.detect_platform({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}, "5.14", "linux"), "fedora")
        self.assertEqual(deps.detect_platform({"ID": "ubuntu", "ID_LIKE": "debian"}, "6.8", "linux"), "debian")
        self.assertEqual(deps.detect_platform({"ID": "ubuntu"}, "5.15.0-microsoft-standard-WSL2", "linux"), "wsl-debian")
        self.assertEqual(deps.detect_platform({}, "23.0.0", "darwin"), "macos")

    def test_hints(self):
        self.assertEqual(deps.matplotlib_hint("fedora"), "sudo dnf install python3-matplotlib")
        self.assertEqual(deps.matplotlib_hint("debian"), "sudo apt install python3-matplotlib")
        self.assertIn("venv", deps.matplotlib_hint("macos"))
        self.assertIn("Re-run gather", deps.sadf_hint("macos"))

    def test_t11_check_without_matplotlib_lists_venv_and_exits_one(self):
        original = deps.matplotlib_version
        deps.matplotlib_version = lambda: None
        original_detect = deps.detect_platform
        deps.detect_platform = lambda *a, **k: "macos"
        try:
            out = io.StringIO()
            code = deps.run_check(out)
        finally:
            deps.matplotlib_version = original
            deps.detect_platform = original_detect
        self.assertEqual(code, 1)
        self.assertIn("python3 -m venv ~/.venvs/perfdiag", out.getvalue())
        self.assertIn("MISSING", out.getvalue())


if __name__ == "__main__":
    unittest.main()
