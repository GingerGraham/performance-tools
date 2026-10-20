"""Tests for linux/os/collect-hw-inventory.sh against a fake sysfs tree."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from fakesys import FakeProc, FakeSysfs
from helpers import read_text  # noqa: E402

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
        self.proc = FakeProc(os.path.join(self.tmp, "proc"))
        self.bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin_dir)
        self.mountinfo = os.path.join(self.tmp, "mountinfo")
        with open(self.mountinfo, "w") as handle:
            handle.write("")
        self.outdir = os.path.join(self.tmp, "out")
        self.cpuinfo = os.path.join(self.tmp, "cpuinfo")
        self.set_hypervisor(False)

    def set_hypervisor(self, present):
        """Writes the cpuinfo the script reads for the hypervisor flag."""
        flags = "fpu vme sse4_2 hypervisor" if present else "fpu vme sse4_2"
        with open(self.cpuinfo, "w") as handle:
            handle.write("processor\t: 0\nmodel name\t: Test CPU\nflags\t\t: %s\n" % flags)

    def stub_command(self, name, body):
        path = os.path.join(self.bin_dir, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def set_users(self, users):
        """Stubs getent so the uid in a fake status file maps to a name: {26: "postgres"}."""
        cases = "".join('%d) echo "%s:x:%d:%d::/home/%s:/bin/bash";; ' % (uid, name, uid, uid, name)
                        for uid, name in users.items())
        self.stub_command("getent", 'case "$2" in %s*) exit 2;; esac' % cases)

    def run_script(self, *args):
        env = dict(os.environ)
        env["HW_INVENTORY_SYSFS"] = self.sysfs.root
        env["HW_INVENTORY_PROC"] = self.proc.root
        env["HW_INVENTORY_MOUNTINFO"] = self.mountinfo
        env["HW_INVENTORY_CPUINFO"] = self.cpuinfo
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


class VirtualAndSanClassTests(InventoryCase):
    """F3: virtual and SAN SCSI disks are not spindles."""

    def test_vmware_virtual_disk(self):
        self.set_hypervisor(True)
        self.sysfs.add_disk("sda", rotational=1, vendor="VMware", model="Virtual disk", bus="pci", scsi_host=0)
        self.run_script()
        self.assertEqual(self.hardware()["disk.sda.class"], "virtual")

    def test_qemu_harddisk(self):
        self.sysfs.add_disk("sdb", rotational=1, vendor="QEMU", model="QEMU HARDDISK", bus="pci2", scsi_host=1)
        self.run_script()
        self.assertEqual(self.hardware()["disk.sdb.class"], "virtual")

    def test_iscsi_lun_is_san_with_transport_hint(self):
        self.sysfs.add_disk("sdc", rotational=1, vendor="LIO-ORG", model="lun0", bus="platform/host3/session1", scsi_host=3)
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.sdc.class"], "san")
        self.assertEqual(hw["disk.sdc.transport_hint"], "iscsi")

    def test_fc_lun_is_san_with_transport_hint(self):
        self.sysfs.add_disk("sdd", rotational=1, vendor="ACME", model="LUN", bus="pci/host2/rport-2:0-1", scsi_host=2)
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.sdd.class"], "san")
        self.assertEqual(hw["disk.sdd.transport_hint"], "fc")

    def test_array_vendor_without_iscsi_or_fc_path(self):
        self.sysfs.add_disk("sde", rotational=1, vendor="NETAPP", model="LUN C-Mode", bus="pci5", scsi_host=5)
        self.run_script()
        self.assertEqual(self.hardware()["disk.sde.class"], "san")

    def test_ata_spindle_on_physical_host_stays_hdd(self):
        self.sysfs.add_disk("sdf", rotational=1, vendor="ATA", model="ST4000", bus="ata1", scsi_host=6)
        self.run_script()
        self.assertEqual(self.hardware()["disk.sdf.class"], "hdd")

    def test_unlisted_scsi_disk_in_a_vm_is_virtual(self):
        self.set_hypervisor(True)
        self.sysfs.add_disk("sdg", rotational=1, vendor="ACME", model="Disk", bus="pci6", scsi_host=7)
        self.run_script()
        self.assertEqual(self.hardware()["disk.sdg.class"], "virtual")

    def test_hypervisor_does_not_change_ata_disk_names_outside_sd_hd(self):
        self.set_hypervisor(True)
        self.sysfs.add_disk("sdh", rotational=0, vendor="ATA", model="Samsung SSD", bus="ata7", scsi_host=8)
        self.sysfs.add_disk("mmcblk0", rotational=0, vendor="X", model="Y", bus="mmc")
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.sdh.class"], "virtual")
        self.assertEqual(hw["disk.mmcblk0.class"], "ssd")

    def test_gcp_persistent_disk_on_nvme_is_virtual_but_local_ssd_is_nvme(self):
        self.sysfs.add_disk("nvme0n1", rotational=0, model="nvme_card-pd", bus="nvme/nvme0")
        self.sysfs.add_disk("nvme1n1", rotational=0, model="nvme_card", bus="nvme/nvme1")
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.nvme0n1.class"], "virtual")
        self.assertEqual(hw["disk.nvme1n1.class"], "nvme")

    def test_san_lun_has_no_parallelism(self):
        self.sysfs.add_disk("sdi", rotational=1, vendor="DGC", model="VRAID", bus="pci8", scsi_host=9)
        self.run_script()
        hw = self.hardware()
        self.assertEqual(hw["disk.sdi.class"], "san")
        self.assertEqual(hw["disk.sdi.parallelism"], "")


class PartitionTests(InventoryCase):
    """F1: partitions are recorded as part.<name>.parent and never as disks."""

    def test_partition_recorded_with_its_parent(self):
        self.sysfs.add_disk("sda", rotational=1, vendor="ATA", model="ST", bus="ata1", scsi_host=1)
        self.sysfs.add_partition("sda", "sda1")
        self.sysfs.add_partition("sda", "sda2")
        self.assertEqual(self.run_script().returncode, 0)
        hw = self.hardware()
        self.assertEqual(hw["part.sda1.parent"], "sda")
        self.assertEqual(hw["part.sda2.parent"], "sda")
        self.assertFalse([key for key in hw if key.startswith("disk.sda1.")])

    def test_nvme_partition_name_with_bang_is_translated(self):
        self.sysfs.add_disk("nvme0n1", rotational=0, model="X", bus="nvme/nvme0")
        self.sysfs.add_partition("nvme0n1", "nvme0n1p1")
        self.run_script()
        self.assertEqual(self.hardware()["part.nvme0n1p1.parent"], "nvme0n1")

    def test_subdirectory_without_partition_file_is_not_a_partition(self):
        self.sysfs.add_disk("sdb", rotational=1, vendor="ATA", model="ST", bus="ata2", scsi_host=2)
        # mq/ and queue/ exist under every disk and must not be mistaken for partitions
        self.run_script()
        hw = self.hardware()
        self.assertFalse([key for key in hw if key.startswith("part.")])


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


class DatabaseProcessTests(InventoryCase):
    """Spec 7.3: dbproc.N.* for the main process of each database server."""

    def setUp(self):
        super().setUp()
        self.sysfs.add_disk("vda", vendor="0x1af4")
        self.set_users({26: "postgres", 27: "mysql", 54321: "oracle", 998: "mssql"})

    def dbproc(self):
        self.assertEqual(self.run_script().returncode, 0)
        hw = self.hardware()
        return hw, {key: value for key, value in hw.items() if key.startswith("dbproc.")}

    def test_postgresql_main_process_only(self):
        self.proc.add_process(1234, ["/usr/pgsql-16/bin/postgres", "-D", "/var/lib/pgsql/16/data"], ppid=1, uid=26,
                              exe="/usr/pgsql-16/bin/postgres", cwd="/var/lib/pgsql/16/data")
        self.proc.add_process(1240, ["postgres: checkpointer"], ppid=1234, uid=26, comm="postgres")
        self.proc.add_process(1241, ["postgres: postgres appdb [local] idle"], ppid=1234, uid=26, comm="postgres")
        _, keys = self.dbproc()
        self.assertEqual(keys, {
            "dbproc.1.engine": "postgresql", "dbproc.1.pid": "1234", "dbproc.1.user": "postgres",
            "dbproc.1.exe": "/usr/pgsql-16/bin/postgres", "dbproc.1.cwd": "/var/lib/pgsql/16/data",
            "dbproc.1.args_datadir": "/var/lib/pgsql/16/data", "dbproc.1.instance": "",
        })

    def test_postgresql_postmaster_and_attached_datadir_form(self):
        self.proc.add_process(500, ["/usr/bin/postmaster", "-D/srv/pg"], ppid=1, uid=26, cwd="/srv/pg")
        _, keys = self.dbproc()
        self.assertEqual(keys["dbproc.1.args_datadir"], "/srv/pg")

    def test_two_postgresql_clusters_numbered_by_pid(self):
        self.proc.add_process(1000, ["/usr/lib/postgresql/16/bin/postgres", "-D", "/b"], uid=26, cwd="/b")
        self.proc.add_process(999, ["/usr/lib/postgresql/14/bin/postgres", "-D", "/a"], uid=26, cwd="/a")
        _, keys = self.dbproc()
        self.assertEqual(keys["dbproc.1.pid"], "999")
        self.assertEqual(keys["dbproc.2.pid"], "1000")
        self.assertEqual(keys["dbproc.2.cwd"], "/b")

    def test_mysql_and_mariadb_but_not_wrapper_or_exporter(self):
        self.proc.add_process(2000, ["/bin/sh", "/usr/bin/mysqld_safe"], uid=27)
        self.proc.add_process(2001, ["/usr/sbin/mysqld", "--basedir=/usr", "--datadir=/var/lib/mysql", "--user=mysql"],
                              ppid=2000, uid=27, exe="/usr/sbin/mysqld", cwd="/var/lib/mysql")
        self.proc.add_process(2002, ["/usr/bin/mysqld_exporter"], uid=27)
        self.proc.add_process(2100, ["/usr/sbin/mariadbd", "--datadir", "/data/maria"], uid=27, cwd="/data/maria")
        _, keys = self.dbproc()
        self.assertEqual(keys["dbproc.1.engine"], "mysql")
        self.assertEqual(keys["dbproc.1.pid"], "2001")
        self.assertEqual(keys["dbproc.1.user"], "mysql")
        self.assertEqual(keys["dbproc.1.args_datadir"], "/var/lib/mysql")
        self.assertEqual(keys["dbproc.2.pid"], "2100")
        self.assertEqual(keys["dbproc.2.args_datadir"], "/data/maria")
        self.assertNotIn("dbproc.3.engine", keys)

    def test_oracle_instance_from_args_and_cwd_left_empty(self):
        # comm is cut at 15 characters; the SID must come from the command line.
        self.proc.add_process(3000, ["ora_pmon_ORCLCDB"], uid=54321, comm="ora_pmon_ORCLCD",
                              exe="/u01/app/oracle/product/19c/bin/oracle", cwd="/u01/app/oracle/product/19c/dbs")
        self.proc.add_process(3001, ["ora_dbw0_ORCLCDB"], uid=54321)
        self.proc.add_process(3050, ["oracleORCLCDB (LOCAL=NO)"], uid=54321, comm="oracleORCLCDB")
        self.proc.add_process(3060, ["asm_pmon_+ASM"], uid=54321)
        hw, keys = self.dbproc()
        self.assertEqual(keys["dbproc.1.engine"], "oracle")
        self.assertEqual(keys["dbproc.1.instance"], "ORCLCDB")
        self.assertEqual(keys["dbproc.1.user"], "oracle")
        self.assertEqual(keys["dbproc.1.exe"], "/u01/app/oracle/product/19c/bin/oracle")
        self.assertEqual(keys["dbproc.1.cwd"], "")
        self.assertNotIn("dbproc.1.cwd_note", keys)
        self.assertNotIn("dbproc.2.engine", keys)

    def test_sql_server_child_of_the_watchdog(self):
        self.proc.add_process(900, ["/opt/mssql/bin/sqlservr"], ppid=1, uid=998, exe="/opt/mssql/bin/sqlservr", cwd="/")
        self.proc.add_process(901, ["/opt/mssql/bin/sqlservr"], ppid=900, uid=998, exe="/opt/mssql/bin/sqlservr", cwd="/")
        _, keys = self.dbproc()
        self.assertEqual(keys["dbproc.1.engine"], "mssql")
        self.assertEqual(keys["dbproc.1.pid"], "901")
        self.assertEqual(keys["dbproc.1.user"], "mssql")
        self.assertNotIn("dbproc.2.engine", keys)

    def test_lone_sql_server_process_is_used_when_there_is_no_watchdog_pair(self):
        self.proc.add_process(910, ["/opt/mssql/bin/sqlservr"], ppid=1, uid=998)
        _, keys = self.dbproc()
        self.assertEqual(keys["dbproc.1.pid"], "910")

    def test_unreadable_cwd_leaves_it_empty_with_a_note(self):
        self.proc.add_process(1234, ["/usr/bin/postgres", "-D", "/var/lib/pgsql/16/data"], uid=26, exe="/usr/bin/postgres")
        _, keys = self.dbproc()
        self.assertEqual(keys["dbproc.1.cwd"], "")
        self.assertEqual(keys["dbproc.1.cwd_note"], "unreadable (run as root)")
        self.assertEqual(keys["dbproc.1.args_datadir"], "/var/lib/pgsql/16/data")

    def test_unknown_uid_falls_back_to_the_number(self):
        self.proc.add_process(1234, ["/usr/bin/postgres"], uid=4242, cwd="/x")
        _, keys = self.dbproc()
        self.assertEqual(keys["dbproc.1.user"], "4242")

    def test_host_without_a_database_is_unchanged(self):
        self.proc.add_process(1, ["/sbin/init"], ppid=0)
        self.proc.add_process(2200, ["/usr/bin/bash", "-l"], uid=1000, cwd="/home/user")
        self.proc.add_process(2201, ["/usr/bin/psql", "-h", "db", "postgres"], uid=1000)
        self.proc.add_process(2202, ["/usr/bin/vim", "/etc/postgres.conf"], uid=1000)
        self.proc.add_kernel_thread(3, "kworker/0:1")
        hw, keys = self.dbproc()
        self.assertEqual(keys, {})
        with open(os.path.join(self.run_dir(), "hardware.txt")) as handle:
            with_processes = handle.read()
        # The same host with no process tree at all gives the same file.
        shutil.rmtree(self.outdir)
        shutil.rmtree(self.proc.root)
        self.assertEqual(self.run_script().returncode, 0)
        with open(os.path.join(self.run_dir(), "hardware.txt")) as handle:
            self.assertEqual(handle.read(), with_processes)


class RunBehaviourTests(InventoryCase):
    def test_non_root_note_and_exit_zero(self):  # T10 (simulated by an unprivileged uid when possible)
        self.sysfs.add_disk("vda", vendor="0x1af4")
        result = self.run_script()
        self.assertEqual(result.returncode, 0)
        if os.geteuid() != 0:
            self.assertIn("not root", result.stderr)
            manifest = read_text(os.path.join(self.run_dir(), "manifest.txt"))
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
