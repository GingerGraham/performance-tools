# collect-hw-inventory.sh

Records what the host is made of: CPU topology, memory settings, every block device with its media class, how devices are stacked (LVM, LUKS, md RAID), which filesystem sits on which device, and the cloud provider. It runs once, takes about a second and records no time series.

`perfdiag` reads the result to choose disk thresholds. An `aqu-sz` of 8 is saturation on one SATA disk and idle on NVMe, so the analysis needs to know which one it is looking at.

Location: `linux/os/collect-hw-inventory.sh`

## Requirements

- Linux with `/proc` and `/sys`. No packages are needed.
- Root is optional. Without it, `dmsetup`, `lvs`, `multipath` and `nvme` output is skipped with a `NOTE:`, and the device-mapper target of each `dm-*` device stays empty. Everything else is read from `/sys` and `/proc`.

## Quick start

```bash
sudo ./collect-hw-inventory.sh
```

With the instance type from the cloud metadata service:

```bash
sudo ./collect-hw-inventory.sh --imds
```

## Options

| Option | Meaning | Default |
|--------|---------|---------|
| `-o`, `--outdir` | Base directory. The script writes `<host>_hw_<timestamp>/` inside it | `/var/log/perf-diag` (`~/perf-diag` when that is not writable) |
| `--imds` | Ask the instance metadata service for the instance type (AWS IMDSv2, Azure). 2-second timeout. A failure is a `NOTE:`, never an error | Off. The script makes no network call unless asked |
| `-h`, `--help` | Print usage and exit | |
| `-V`, `--version` | Print name and version and exit | |

`-d`, `-c` and `-i` do not apply: the script does not sample.

## Output

```
<outdir>/<host>_hw_<YYYYmmdd_HHMMSS>/
    manifest.txt        run metadata, same keys as the collectors
    hardware.txt        key=value, the file perfdiag reads
    raw/                command output for people. perfdiag never parses it
        lscpu.txt  lsblk.txt  mdstat.txt  dmsetup_table.txt  lvs.txt  multipath.txt
        nvme_list.txt  lspci_storage.txt  mountinfo.txt  swaps.txt  sysctl_vm.txt
        scsi_hosts.txt  raid_tools.txt
```

A file in `raw/` is written only when its command exists, succeeded and printed something.

`hardware.txt` is one `key=value` per line with dotted keys. Values are single-line. It is `grep`-able and has no escaping rules.

### Keys

| Prefix | Keys |
|--------|------|
| `cpu.` | `model`, `logical`, `sockets`, `physical_cores`, `cores_per_socket`, `threads_per_core`, `online`, `governor`, `hypervisor`, `flags_virt` |
| `numa.` | `nodes`, `node<N>.cpus`, `node<N>.mem_kb` |
| `mem.` | `total_kb`, `hugepages_total`, `hugepage_kb`, `thp_enabled`, `thp_defrag` |
| `swap.<n>.` | `device`, `type`, `size_kb`, `priority`. `<n>` starts at 1 |
| `vm.` | `swappiness`, `dirty_ratio`, `dirty_background_ratio`, `dirty_bytes`, `dirty_background_bytes`, `overcommit_memory`, `min_free_kbytes` |
| `disk.<kname>.` | see below |
| `fs.<n>.` | `mountpoint`, `source`, `fstype`, `kname`. `<n>` starts at 1 |
| `part.<kname>.` | `parent`: the whole disk a partition belongs to |
| `cloud.` | `provider`, `sys_vendor`, `product_name`, `instance_type` (only with `--imds`), `asset_tag` |

CPU topology is read from `/sys/devices/system/cpu/cpu*/topology`, not from `lscpu -J`, which older util-linux lacks. On a virtual machine, `threads_per_core` describes the vCPU topology the hypervisor presents, which may not match the host.

### Disks

One set of keys per whole disk in `/sys/block`, excluding `loop*`, `ram*`, `zram*`, `sr*` and `fd*`. A `!` in a kernel name is written as `/` (`cciss/c0d0`).

| Key | Meaning |
|-----|---------|
| `display` | Device-mapper name (`dm/name`), else empty |
| `size_bytes`, `rotational`, `model`, `vendor` | From sysfs. NVMe namespaces take the model from the controller |
| `class` | Media class, below |
| `scheduler`, `nr_requests`, `read_ahead_kb`, `max_sectors_kb`, `hw_queues` | Queue settings. `hw_queues` is the blk-mq hardware queue count |
| `logical_block`, `physical_block` | Block sizes |
| `slaves`, `holders` | Comma lists. Partitions in `slaves` are replaced by their whole disk |
| `md_level`, `md_raid_disks`, `md_chunk_kb` | md RAID |
| `dm_target`, `dm_stripes` | First device-mapper target and its stripe count (root only) |
| `transport_hint` | `nvme`, `virtio`, `usb`, `ata`, `iscsi`, `fc` or `host` (other SCSI), from the device path |
| `hw_raid` | `suspected` or `no` |
| `parallelism` | How many independent devices can serve IO at once. Used only by the HDD queue rules |

### Partitions

`sar -d` with `XDISK` reports partitions (`sda1`, `nvme0n1p3`) as well as disks. Every partition of a listed disk is written as `part.<kname>.parent=<disk>`, with no `disk.<kname>.` keys, so `perfdiag` can fold it into the disk. Device-mapper partitions made by `kpartx` (`mpatha1`) are stacked devices with their own `slaves`, not partitions.

### Media class

The first matching rule wins.

| # | Condition | `class` |
|---|-----------|---------|
| 1 | Model contains `Amazon Elastic Block Store` | `cloud-ebs` |
| 2 | Model contains `Amazon EC2 NVMe Instance Storage` | `nvme` |
| 3 | Vendor `Msft`, or model contains `Microsoft` or `MSFT` | `cloud-azure` |
| 4 | "vendor model" matches the virtual disk table below | `virtual` |
| 5 | Transport `iscsi` or `fc` | `san` |
| 6 | Vendor is a storage array vendor (below) | `san` |
| 7 | Name `dm-*` or `md*` | `stacked` (resolved from the member disks during analysis) |
| 8 | Name `nvme*` | `nvme` |
| 9 | Name `vd*` or `xvd*` | `virtual` |
| 10 | `hw_raid=suspected` | `hw-raid` |
| 11 | The CPU reports a hypervisor and the name is `sd*` or `hd*` | `virtual` |
| 12 | `rotational=1` | `hdd` |
| 13 | `rotational=0` | `ssd` |
| 14 | Anything else | `unknown` |

`rotational` alone is not trusted: virtio disks report `rotational=1`, and so do VMware, KVM and VirtualBox SCSI disks, GCP Persistent Disk and nearly every iSCSI or FC LUN. Rule 11 covers emulated disks that are not in the table. A physical disk passed through to a VM is rare; override it with `perfdiag --class DEV=hdd`.

Virtual disk table (case-insensitive match on vendor and model):

| Pattern | Platform |
|---------|----------|
| `vmware` at the start, or `virtual disk` | VMware vSphere |
| `qemu harddisk`, or `qemu` at the start | KVM / QEMU emulation |
| `vbox harddisk` | VirtualBox |
| `google` … `persistentdisk` | GCP Persistent Disk on SCSI |
| `nvme_card-pd` | GCP Persistent Disk on NVMe |
| `oracle` … `blockvolume` | OCI block volume |

GCP Local SSD reports `nvme_card`, which is real NVMe and stays `nvme`. The GCP and OCI patterns are from memory and are not yet confirmed on real instances, nor are rules 1 to 3.

Array vendors, from the SCSI vendor field: `lio-org netapp pure 3pardata dgc emc nimble compelnt huawei ibm hitachi`. Direct-attached SATA drives report vendor `ATA` through libata, so these names mean an array LUN. The list is the `SAN_VENDORS` variable in the script. `perfdiag --class DEV=CLASS` overrides any class.

Hardware RAID is suspected when the disk's SCSI host driver is `megaraid_sas`, `hpsa`, `smartpqi`, `aacraid` or `mpi3mr`, or the model matches `PERC`, `LOGICAL VOLUME`, `MR9` or `RAID`. `mpt3sas` is normally a plain HBA and is not listed. Vendor tools (`storcli`, `perccli`, `ssacli`) are recorded as present or absent in `raw/raid_tools.txt` and are never run.

### Parallelism

| Device | `parallelism` |
|--------|---------------|
| `hdd` | 1 |
| md `raid0`, `raid1` | `md_raid_disks` (RAID 1 writes are analysed as 1) |
| md `raid10` | `md_raid_disks / 2` |
| md `raid4`, `raid5`, `raid6` | `md_raid_disks - 1` (`- 2` for RAID 6) |
| dm `striped` | `dm_stripes` |
| dm `linear`, `crypt`, multipath, and dm without root | the first member disk with a value |
| `hw-raid` | empty, with a `NOTE:`. Pass `--spindles DEV=N` to `perfdiag` |
| any other class | empty |

### Cloud detection

Read from `/sys/class/dmi/id` without root.

| `sys_vendor` | `cloud.provider` |
|--------------|------------------|
| `Amazon EC2` | `aws` |
| `Microsoft Corporation` with `product_name` `Virtual Machine` | `azure` |
| `Google` | `gcp` |
| anything else | empty |

`asset_tag` is recorded as found. On AWS it holds the instance ID. DMI serial numbers are not recorded.

With `--imds` the script calls `169.254.169.254` using `curl` or `wget`: AWS IMDSv2 (a token, then `instance-type`) or the Azure `vmSize` endpoint. Only the characters `A-Z a-z 0-9 . _ -` are accepted in the answer.

### Filesystems

`fs.<n>` is built from `/proc/self/mountinfo`: the `major:minor` of each mount is resolved through `/sys/dev/block` to a kernel name. A partition is reported as its whole disk. Pseudo filesystems (`proc`, `sysfs`, `tmpfs`, `cgroup*`, `overlay` and similar) are skipped. A mount with no block device (btrfs subvolumes, NFS) has an empty `kname`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success. Missing optional data is a `NOTE:` |
| 1 | Runtime error, such as an output directory that cannot be created |
| 2 | Usage error |
