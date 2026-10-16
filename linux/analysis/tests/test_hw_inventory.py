"""Tests for linux/os/collect-hw-inventory.sh against a fake sysfs tree."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from fakesys import FakeSysfs

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "os", "collect-hw-inventory.sh")


def parse_hardware(path):
    values = {}
    with open(path) as handle:
        for line in handle:
            key, _, value = line.rstrip("\n").partition("=")
            values[key] = value
    return values


class InventoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hwinv-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sysfs = FakeSysfs(os.path.join(self.tmp, "sys"))
        self.bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin_dir)
        self.mountinfo = os.path.join(self.tmp, "mountinfo")
        with open(self.mountinfo, "w") as handle:
            handle.write("")
        self.outdir = os.path.join(self.tmp, "out")

    def stub_command(self, name, body):
        path = os.path.join(self.bin_dir, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def run_script(self, *args):
        env = dict(os.environ)
        env["HW_INVENTORY_SYSFS"] = self.sysfs.root
        env["HW_INVENTORY_MOUNTINFO"] = self.mountinfo
        env["PATH"] = self.bin_dir + os.pathsep + env["PATH"]
        result = subprocess.run(["bash", SCRIPT, "-o", self.outdir] + list(args),
                                capture_output=True, text=True, env=env)
        return result

    def hardware(self):
        runs = [d for d in os.listdir(self.outdir) if "_hw_" in d]
        self.assertEqual(len(runs), 1)
        return parse_hardware(os.path.join(self.outdir, runs[0], "hardware.txt"))

    def run_dir(self):
        return os.path.join(self.outdir, [d for d in os.listdir(self.outdir) if "_hw_" in d][0])


class ClassTests(InventoryCase):
    def test_virtio_disk_is_virtual_despite_rotational(self):
        self.sysfs.add_disk("vda", rotational=1, vendor="0x1af4", bus="virtio1")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        hw = self.hardware()
        self.assertEqual(hw["disk.vda.class"], "virtual")
        self.assertEqual(hw["disk.vda.rotational"], "1")
        self.assertEqual(hw["disk.vda.transport_hint"], "virtio")
        self.assertEqual(hw["disk.vda.hw_queues"], "1")
        self.assertEqual(hw["disk.vda.size_bytes"], str(2097152 * 512))

    def test_ebs_model_wins_over_nvme_name(self):
        self.sysfs.add_disk("nvme1n1", rotational=0, model="Amazon Elastic Block Store", bus="nvme/nvme1")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.hardware()["disk.nvme1n1.class"], "cloud-ebs")

    def test_instance_store_is_nvme(self):
        self.sysfs.add_disk("nvme2n1", rotational=0, model="Amazon EC2 NVMe Instance Storage", bus="nvme/nvme2")
        self.run_script()
        self.assertEqual(self.hardware()["disk.nvme2n1.class"], "nvme")

    def test_azure_by_vendor_and_by_model(self):
        self.sysfs.add_disk("sda", vendor="Msft", model="Virtual Disk", bus="vmbus", scsi_host=0)
        self.sysfs.add_disk("sdb", vendor="ATA", model="Microsoft Disk", bus="vmbus2", scsi_host=1)
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.sda.class"], "cloud-azure")
        self.assertEqual(hw["disk.sdb.class"], "cloud-azure")

    def test_rotational_decides_hdd_and_ssd(self):
        self.sysfs.add_disk("sdc", rotational=1, vendor="ATA", model="ST4000", bus="ata1", scsi_host=2)
        self.sysfs.add_disk("sdd", rotational=0, vendor="ATA", model="Samsung SSD", bus="ata2", scsi_host=3)
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.sdc.class"], "hdd")
        self.assertEqual(hw["disk.sdc.parallelism"], "1")
        self.assertEqual(hw["disk.sdd.class"], "ssd")
        self.assertEqual(hw["disk.sdd.parallelism"], "")

    def test_hardware_raid_by_driver(self):
        self.sysfs.add_disk("sde", rotational=1, vendor="DELL", model="Volume", bus="pci", scsi_host=4)
        self.sysfs.set_scsi_proc_name(4, "megaraid_sas")
        result = self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.sde.hw_raid"], "suspected")
        self.assertEqual(hw["disk.sde.class"], "hw-raid")
        self.assertEqual(hw["disk.sde.parallelism"], "")
        self.assertIn("parallelism unknown", result.stderr)

    def test_hardware_raid_by_model(self):
        self.sysfs.add_disk("sdf", rotational=1, vendor="DELL", model="PERC H730P", bus="pci2", scsi_host=5)
        self.run_script()
        self.assertEqual(self.hardware()["disk.sdf.class"], "hw-raid")

    def test_mpt3sas_is_a_plain_hba(self):
        self.sysfs.add_disk("sdg", rotational=1, vendor="ATA", model="WDC", bus="pci3", scsi_host=6)
        self.sysfs.set_scsi_proc_name(6, "mpt3sas")
        self.run_script()
        self.assertEqual(self.hardware()["disk.sdg.class"], "hdd")

    def test_unknown_when_rotational_unreadable(self):
        self.sysfs.add_disk("sdh", vendor="X", model="Y", bus="pci4", scsi_host=7)
        os.remove(os.path.join(os.path.realpath(os.path.join(self.sysfs.root, "block", "sdh")), "queue", "rotational"))
        self.run_script()
        self.assertEqual(self.hardware()["disk.sdh.class"], "unknown")


class StackingTests(InventoryCase):
    def test_md_raid10_four_disks(self):  # T8
        for name, scsi_host in (("sdb", 1), ("sdc", 2), ("sdd", 3), ("sde", 4)):
            self.sysfs.add_disk(name, rotational=1, vendor="ATA", model="ST", bus="ata%d" % scsi_host, scsi_host=scsi_host)
        self.sysfs.add_md("md0", "raid10", ["sdb", "sdc", "sdd", "sde"])
        self.assertEqual(self.run_script().returncode, 0)
        hw = self.hardware()
        self.assertEqual(hw["disk.md0.class"], "stacked")
        self.assertEqual(hw["disk.md0.md_level"], "raid10")
        self.assertEqual(hw["disk.md0.md_raid_disks"], "4")
        self.assertEqual(hw["disk.md0.parallelism"], "2")
        self.assertEqual(hw["disk.md0.md_chunk_kb"], "512")
        self.assertEqual(hw["disk.md0.slaves"], "sdb,sdc,sdd,sde")
        self.assertEqual(hw["disk.sdb.holders"], "md0")

    def test_md_parallelism_by_level(self):
        for number, (level, expected) in enumerate((("raid0", "4"), ("raid1", "4"), ("raid5", "3"), ("raid6", "2"))):
            names = ["s%d%s" % (number, letter) for letter in "abcd"]
            for index, name in enumerate(names):
                self.sysfs.add_disk(name, rotational=1, vendor="ATA", model="ST", bus="ata_%s" % name, scsi_host=10 * number + index)
            self.sysfs.add_md("md%d" % number, level, names)
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.md0.parallelism"], "4")
        self.assertEqual(hw["disk.md1.parallelism"], "4")
        self.assertEqual(hw["disk.md2.parallelism"], "3")
        self.assertEqual(hw["disk.md3.parallelism"], "2")

    def test_lvm_on_luks_on_partition(self):  # T9
        self.sysfs.add_disk("sda", rotational=1, vendor="ATA", model="ST", bus="ata1", scsi_host=1)
        self.sysfs.add_partition("sda", "sda3", devno="8:3")
        self.sysfs.add_dm("dm-0", "luks-1234", ["sda3"], devno="253:0")
        self.sysfs.add_dm("dm-1", "vg0-pgdata", ["dm-0"], devno="253:1")
        self.stub_command("dmsetup", 'case "$2" in luks-1234) echo "0 100 crypt aes-xts-plain64 :64:logon:cryptsetup:x 0 8:3 4096";; vg0-pgdata) echo "0 100 linear 253:0 2048";; esac')
        with open(self.mountinfo, "w") as handle:
            handle.write("40 1 253:1 / /var/lib/pgsql rw,relatime shared:1 - xfs /dev/mapper/vg0-pgdata rw\n")
            handle.write("41 1 8:3 / /mnt/raw rw - ext4 /dev/sda3 rw\n")
            handle.write("42 1 0:25 / /proc rw - proc proc rw\n")
        self.assertEqual(self.run_script().returncode, 0)
        hw = self.hardware()
        self.assertEqual(hw["disk.dm-0.display"], "luks-1234")
        self.assertEqual(hw["disk.dm-0.dm_target"], "crypt")
        self.assertEqual(hw["disk.dm-1.dm_target"], "linear")
        self.assertEqual(hw["disk.dm-0.slaves"], "sda")
        self.assertEqual(hw["disk.dm-1.slaves"], "dm-0")
        self.assertEqual(hw["disk.dm-1.parallelism"], "1")
        self.assertEqual(hw["fs.1.mountpoint"], "/var/lib/pgsql")
        self.assertEqual(hw["fs.1.kname"], "dm-1")
        self.assertEqual(hw["fs.2.kname"], "sda")
        self.assertNotIn("fs.3.mountpoint", hw)

    def test_dm_striped_uses_stripe_count(self):
        for index, name in enumerate(("sdb", "sdc", "sdd")):
            self.sysfs.add_disk(name, rotational=1, vendor="ATA", model="ST", bus="ata%d" % index, scsi_host=index)
        self.sysfs.add_dm("dm-0", "vg-striped", ["sdb", "sdc", "sdd"])
        self.stub_command("dmsetup", 'echo "0 600 striped 3 128 8:16 0 8:32 0 8:48 0"')
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.dm-0.dm_target"], "striped")
        self.assertEqual(hw["disk.dm-0.dm_stripes"], "3")
        self.assertEqual(hw["disk.dm-0.parallelism"], "3")

    def test_dm_over_ssd_has_no_parallelism(self):
        self.sysfs.add_disk("sdb", rotational=0, vendor="ATA", model="SSD", bus="ata1", scsi_host=1)
        self.sysfs.add_dm("dm-0", "vg-lv", ["sdb"])
        self.stub_command("dmsetup", 'echo "0 100 linear 8:16 0"')
        self.run_script()
        self.assertEqual(self.hardware()["disk.dm-0.parallelism"], "")

    def test_exclusions_and_translation(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        for name in ("loop0", "ram0", "zram0", "sr0", "fd0"):
            self.sysfs.add_disk(name)
        self.run_script()
        hw = self.hardware()
        self.assertIn("disk.vda.class", hw)
        for name in ("loop0", "ram0", "zram0", "sr0", "fd0"):
            self.assertNotIn("disk.%s.class" % name, hw)

    def test_cciss_name_translated(self):
        self.sysfs.add_disk("cciss!c0d0", rotational=1, vendor="HP", model="LOGICAL VOLUME", bus="cciss", scsi_host=9)
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.cciss/c0d0.class"], "hw-raid")


class RunBehaviourTests(InventoryCase):
    def test_non_root_note_and_exit_zero(self):  # T10 (simulated by an unprivileged uid when possible)
        self.sysfs.add_disk("vda", vendor="0x1af4")
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        if os.geteuid() != 0:
            self.assertIn("not root", result.stderr)
            manifest = open(os.path.join(self.run_dir(), "manifest.txt")).read()
            self.assertIn("root=no", manifest)

    def test_cloud_fields_from_dmi(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.sysfs.set_dmi("Amazon EC2", "m5.large", "i-0abc")
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["cloud.provider"], "aws")
        self.assertEqual(hw["cloud.product_name"], "m5.large")
        self.assertEqual(hw["cloud.instance_type"], "")

    def test_azure_requires_virtual_machine_product(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.sysfs.set_dmi("Microsoft Corporation", "Surface Laptop")
        self.run_script()
        self.assertEqual(self.hardware()["cloud.provider"], "")

    def test_imds_not_called_unless_asked(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.sysfs.set_dmi("Amazon EC2", "m5.large")
        marker = os.path.join(self.tmp, "curl_called")
        self.stub_command("curl", 'touch "%s"; exit 22' % marker)
        self.run_script()
        self.assertFalse(os.path.exists(marker))

    def test_imds_aws_v2(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.sysfs.set_dmi("Amazon EC2", "m5.large")
        self.stub_command("curl", 'case "$*" in *api/token*) echo TOKEN123;; *instance-type*) case "$*" in *TOKEN123*) echo t3.medium;; *) exit 22;; esac;; esac')
        result = self.run_script("--imds")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hardware()["cloud.instance_type"], "t3.medium")

    def test_imds_failure_is_a_note(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.sysfs.set_dmi("Amazon EC2", "m5.large")
        self.stub_command("curl", "exit 28")
        result = self.run_script("--imds")
        self.assertEqual(result.returncode, 0)
        self.assertIn("NOTE:", result.stderr)
        self.assertEqual(self.hardware()["cloud.instance_type"], "")

    def test_imds_rejects_odd_characters(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.sysfs.set_dmi("Microsoft Corporation", "Virtual Machine")
        self.stub_command("curl", "echo 'Standard_D2s_v3;rm -rf'")
        self.run_script("--imds")
        self.assertEqual(self.hardware()["cloud.instance_type"], "")

    def test_values_are_single_line(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.run_script()
        run_dir = self.run_dir()
        with open(os.path.join(run_dir, "hardware.txt")) as handle:
            for line in handle:
                self.assertIn("=", line)

    def test_cpu_counts_are_consistent(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.run_script()
        hw = self.hardware()
        self.assertGreaterEqual(int(hw["cpu.logical"]), int(hw["cpu.physical_cores"]))
        self.assertGreaterEqual(int(hw["cpu.physical_cores"]), 1)
        self.assertGreaterEqual(int(hw["cpu.threads_per_core"]), 1)

    def test_swap_and_vm_keys(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.run_script()
        hw = self.hardware()
        self.assertIn("vm.swappiness", hw)
        self.assertIn("mem.total_kb", hw)

    def test_usage_error_exit_two(self):
        result = subprocess.run(["bash", SCRIPT, "--bogus"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        result = subprocess.run(["bash", SCRIPT, "-o"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_version_and_help(self):
        self.assertEqual(subprocess.run(["bash", SCRIPT, "-V"], capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(["bash", SCRIPT, "--help"], capture_output=True).returncode, 0)

    def test_manifest_has_analyser_keys(self):
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.run_script()
        manifest = parse_hardware(os.path.join(self.run_dir(), "manifest.txt"))
        for key in ("hostname", "cpus", "mem_total_kb", "timezone", "start"):
            self.assertIn(key, manifest)
        self.assertTrue(os.path.isdir(os.path.join(self.run_dir(), "raw")))


if __name__ == "__main__":
    unittest.main()
