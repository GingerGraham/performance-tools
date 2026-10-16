"""Builds a fake /sys tree so collect-hw-inventory.sh can be tested on any host.

The sandbox and CI kernels often lack md and device-mapper, so the stacked
device paths (RAID10, LVM on LUKS) cannot be created for real.
"""

import os


class FakeSysfs:
    def __init__(self, root):
        self.root = root
        self.devices = os.path.join(root, "devices", "pci0000:00")
        for sub in ("block", "class/block", "class/scsi_host", "dev/block"):
            os.makedirs(os.path.join(root, sub), exist_ok=True)

    def _write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text + "\n")

    def add_disk(self, name, size_sectors=2097152, rotational=1, model=None, vendor=None,
                 bus="virtio1", scsi_host=None, scheduler="none [mq-deadline]", mq_queues=1):
        """Adds a whole disk. `bus` becomes part of the resolved device path."""
        if scsi_host is not None:
            bus_dir = os.path.join(self.devices, bus, "host%d" % scsi_host, "target0:0:0", "0:0:0:0")
        else:
            bus_dir = os.path.join(self.devices, bus)
        os.makedirs(bus_dir, exist_ok=True)
        if model is not None:
            self._write(os.path.join(bus_dir, "model"), model)
        if vendor is not None:
            self._write(os.path.join(bus_dir, "vendor"), vendor)
        disk_dir = os.path.join(bus_dir, "block", name)
        os.makedirs(disk_dir, exist_ok=True)
        block_link = os.path.join(self.root, "block", name)
        os.symlink(os.path.relpath(disk_dir, os.path.dirname(block_link)), block_link)
        os.symlink(os.path.relpath(bus_dir, disk_dir), os.path.join(disk_dir, "device"))
        class_link = os.path.join(self.root, "class", "block", name)
        os.symlink(os.path.relpath(disk_dir, os.path.dirname(class_link)), class_link)
        self._write(os.path.join(disk_dir, "size"), str(size_sectors))
        queue = os.path.join(disk_dir, "queue")
        self._write(os.path.join(queue, "rotational"), str(rotational))
        self._write(os.path.join(queue, "scheduler"), scheduler)
        self._write(os.path.join(queue, "nr_requests"), "256")
        self._write(os.path.join(queue, "read_ahead_kb"), "128")
        self._write(os.path.join(queue, "max_sectors_kb"), "1280")
        self._write(os.path.join(queue, "logical_block_size"), "512")
        self._write(os.path.join(queue, "physical_block_size"), "512")
        for queue_number in range(mq_queues):
            os.makedirs(os.path.join(disk_dir, "mq", str(queue_number)), exist_ok=True)
        if scsi_host is not None:
            return disk_dir
        return disk_dir

    def set_scsi_proc_name(self, host_number, proc_name):
        self._write(os.path.join(self.root, "class", "scsi_host", "host%d" % host_number, "proc_name"), proc_name)

    def add_partition(self, disk_name, partition_name, devno=None):
        disk_dir = os.path.realpath(os.path.join(self.root, "block", disk_name))
        part_dir = os.path.join(disk_dir, partition_name)
        os.makedirs(part_dir, exist_ok=True)
        self._write(os.path.join(part_dir, "partition"), "1")
        class_link = os.path.join(self.root, "class", "block", partition_name)
        os.symlink(os.path.relpath(part_dir, os.path.dirname(class_link)), class_link)
        if devno:
            dev_link = os.path.join(self.root, "dev", "block", devno)
            os.symlink(os.path.relpath(part_dir, os.path.dirname(dev_link)), dev_link)
        return part_dir

    def _virtual_device(self, name, devno=None, size_sectors=2097152):
        disk_dir = os.path.join(self.root, "devices", "virtual", "block", name)
        os.makedirs(disk_dir, exist_ok=True)
        block_link = os.path.join(self.root, "block", name)
        os.symlink(os.path.relpath(disk_dir, os.path.dirname(block_link)), block_link)
        class_link = os.path.join(self.root, "class", "block", name)
        os.symlink(os.path.relpath(disk_dir, os.path.dirname(class_link)), class_link)
        if devno:
            dev_link = os.path.join(self.root, "dev", "block", devno)
            os.symlink(os.path.relpath(disk_dir, os.path.dirname(dev_link)), dev_link)
        self._write(os.path.join(disk_dir, "size"), str(size_sectors))
        queue = os.path.join(disk_dir, "queue")
        self._write(os.path.join(queue, "rotational"), "1")
        self._write(os.path.join(queue, "scheduler"), "none")
        os.makedirs(os.path.join(disk_dir, "slaves"), exist_ok=True)
        os.makedirs(os.path.join(disk_dir, "holders"), exist_ok=True)
        return disk_dir

    def _link_slave(self, stacked_dir, slave_name):
        """Creates stacked/slaves/<slave> and <slave>/holders/<stacked>."""
        slave_target = os.path.realpath(os.path.join(self.root, "class", "block", slave_name))
        stacked_name = os.path.basename(stacked_dir)
        slave_link = os.path.join(stacked_dir, "slaves", slave_name)
        os.symlink(os.path.relpath(slave_target, os.path.dirname(slave_link)), slave_link)
        holders = os.path.join(slave_target, "holders")
        os.makedirs(holders, exist_ok=True)
        holder_link = os.path.join(holders, stacked_name)
        if not os.path.lexists(holder_link):
            os.symlink(os.path.relpath(stacked_dir, holders), holder_link)

    def add_md(self, name, level, slaves, chunk_bytes=524288, devno=None):
        md_dir = self._virtual_device(name, devno)
        self._write(os.path.join(md_dir, "md", "level"), level)
        self._write(os.path.join(md_dir, "md", "raid_disks"), str(len(slaves)))
        self._write(os.path.join(md_dir, "md", "chunk_size"), str(chunk_bytes))
        for slave in slaves:
            self._link_slave(md_dir, slave)
        return md_dir

    def add_dm(self, name, mapper_name, slaves, devno=None):
        dm_dir = self._virtual_device(name, devno)
        self._write(os.path.join(dm_dir, "dm", "name"), mapper_name)
        for slave in slaves:
            self._link_slave(dm_dir, slave)
        return dm_dir

    def set_dmi(self, sys_vendor, product_name, asset_tag=""):
        dmi = os.path.join(self.root, "class", "dmi", "id")
        self._write(os.path.join(dmi, "sys_vendor"), sys_vendor)
        self._write(os.path.join(dmi, "product_name"), product_name)
        self._write(os.path.join(dmi, "board_asset_tag"), asset_tag)
