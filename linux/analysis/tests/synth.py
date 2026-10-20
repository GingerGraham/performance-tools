"""Builds synthetic gather-style run directories in sadf -d format for the tests.

A calm system by default; tests override the series they care about.
"""

import os

BASE_EPOCH = 1792163600          # 2026-10-16 15:13:20 UTC


def _fmt(value):
    return ("%.2f" % value) if isinstance(value, float) else str(value)


class Synth:
    def __init__(self, root, host="db01", samples=120, interval=1, cpus=2, mem_total_kb=8 * 1024 * 1024,
                 start=BASE_EPOCH, timezone="UTC +0000", old_sysstat=False, psi=True, tag_stamp="20261016_151320",
                 tz_name=""):
        self.root = root
        self.host = host
        self.samples = samples
        self.interval = interval
        self.cpus = cpus
        self.mem_total_kb = mem_total_kb
        self.start = start
        self.timezone = timezone
        self.tz_name = tz_name
        self.old_sysstat = old_sysstat
        self.psi = psi
        self.tag_stamp = tag_stamp
        self.hardware = None
        self.kernel_lines = []
        self.disks = {}
        self.cpu_idle = {"all": [95.0] * samples}
        for index in range(cpus):
            self.cpu_idle[str(index)] = [95.0] * samples
        self.steal = [0.0] * samples
        self.iowait = [0.0] * samples
        self.soft = {str(index): [0.0] * samples for index in range(cpus)}
        self.runq = [0] * samples
        self.avg1 = [0.1] * samples
        self.blocked = [0] * samples
        self.cswch = [1000.0] * samples
        self.mem_avail_pct = [80.0] * samples
        self.dirty_kb = [1000] * samples
        self.swap_out = [0.0] * samples
        self.swap_in = [0.0] * samples
        self.pgscand = [0.0] * samples
        self.majflt = [0.0] * samples
        self.psi_cpu = [0.0] * samples
        self.psi_io_some = [0.0] * samples
        self.psi_io_full = [0.0] * samples
        self.psi_mem_some = [0.0] * samples
        self.psi_mem_full = [0.0] * samples
        self.skip_ranges = []           # (first_index, last_index) samples to leave out entirely
        self.add_disk("vda")

    # --- construction helpers -------------------------------------------
    def add_disk(self, name):
        n = self.samples
        self.disks[name] = {
            "tps": [1.0] * n, "rkB": [0.0] * n, "wkB": [10.0] * n, "areq": [8.0] * n,
            "queue": [0.01] * n, "await": [0.5] * n, "util": [1.0] * n,
        }
        return self.disks[name]

    def fill(self, series, first, last, value):
        for index in range(first, last):
            series[index] = value

    def times(self):
        return [self.start + (index + 1) * self.interval for index in range(self.samples)
                if not any(a <= index <= b for a, b in self.skip_ranges)]

    def _indexes(self):
        return [index for index in range(self.samples) if not any(a <= index <= b for a, b in self.skip_ranges)]

    # --- writing ----------------------------------------------------------
    def _write_csv(self, directory, stem, header, key_name, rows):
        columns = ["hostname", "interval", "timestamp"] + ([key_name] if key_name else []) + header
        lines = ["# " + ";".join(columns)]
        for index in self._indexes():
            epoch = self.start + (index + 1) * self.interval
            for key, values in rows(index):
                fields = [self.host, str(self.interval), str(epoch)] + ([key] if key_name else [])
                fields += [_fmt(value) for value in values]
                lines.append(";".join(fields))
        with open(os.path.join(directory, stem + ".csv"), "w") as handle:
            handle.write("\n".join(lines) + "\n")

    def write(self):
        run_name = "%s_os_%s" % (self.host, self.tag_stamp)
        run_dir = os.path.join(self.root, run_name)
        sadf_dir = os.path.join(run_dir, "sadf")
        os.makedirs(sadf_dir, exist_ok=True)
        psi_line = "yes" if self.psi else "no"
        with open(os.path.join(run_dir, "manifest.txt"), "w") as handle:
            handle.write("\n".join([
                "script=collect-os-metrics.sh", "script_version=2.0.0", "hostname=" + self.host,
                "distro=Test Linux 1.0", "kernel=6.1.0-test", "cpus=%d" % self.cpus,
                "mem_total_kb=%d" % self.mem_total_kb, "timezone=" + self.timezone, "tz_name=" + self.tz_name,
                "start=2026-10-16T15:13:20+00:00", "interval_s=%d" % self.interval,
                "samples=%d" % self.samples, "duration_s=%d" % (self.samples * self.interval),
                "psi=" + psi_line, "sysstat_version=sysstat version 12.7.4", "notes=",
            ]) + "\n")

        def cpu_rows(index):
            rows = []
            for key, idle_values in self.cpu_idle.items():
                idle = idle_values[index]
                busy = 100.0 - idle
                steal = self.steal[index] if key == "all" else 0.0
                iowait = self.iowait[index] if key == "all" else 0.0
                soft = self.soft.get(key, [0.0] * self.samples)[index] if key != "all" else 0.0
                usr = max(0.0, busy - steal - iowait - soft)
                rows.append(("-1" if key == "all" else key, [usr, 0.0, 0.0, iowait, steal, 0.0, soft, 0.0, 0.0, idle]))
            return rows
        self._write_csv(sadf_dir, "cpu", ["%usr", "%nice", "%sys", "%iowait", "%steal", "%irq", "%soft", "%guest", "%gnice", "%idle"],
                        "CPU", cpu_rows)
        self._write_csv(sadf_dir, "load", ["runq-sz", "plist-sz", "ldavg-1", "ldavg-5", "ldavg-15", "blocked"], None,
                        lambda i: [("", [self.runq[i], 200, self.avg1[i], self.avg1[i], self.avg1[i], self.blocked[i]])])
        self._write_csv(sadf_dir, "cswch", ["proc/s", "cswch/s"], None, lambda i: [("", [1.0, self.cswch[i]])])

        total_kb = self.mem_total_kb

        def memory_row(index):
            avail = int(total_kb * self.mem_avail_pct[index] / 100.0)
            if self.old_sysstat:
                return [("", [avail // 2, 100, 100, 0.5, 100, avail // 2, 1000, 5.0, 100, 100, self.dirty_kb[index], 100])]
            return [("", [avail // 2, avail, total_kb - avail, 50.0, 100, avail // 2, 1000, 5.0, 100, 100, self.dirty_kb[index], 100])]
        if self.old_sysstat:
            self._write_csv(sadf_dir, "memory", ["kbmemfree", "kbmemused", "%memused", "kbbuffers", "kbcached", "kbcommit",
                                                 "%commit", "kbactive", "kbinact", "kbdirty", "kbslab"], None, memory_row)
        else:
            self._write_csv(sadf_dir, "memory", ["kbmemfree", "kbavail", "kbmemused", "%memused", "kbbuffers", "kbcached",
                                                 "kbcommit", "%commit", "kbactive", "kbinact", "kbdirty", "kbslab"],
                            None, memory_row)
        self._write_csv(sadf_dir, "paging", ["pgpgin/s", "pgpgout/s", "fault/s", "majflt/s", "pgfree/s", "pgscank/s",
                                             "pgscand/s", "pgsteal/s", "%vmeff"], None,
                        lambda i: [("", [0.0, 0.0, 100.0, self.majflt[i], 10.0, 0.0, self.pgscand[i], 0.0, 0.0])])
        self._write_csv(sadf_dir, "swap_rate", ["pswpin/s", "pswpout/s"], None,
                        lambda i: [("", [self.swap_in[i], self.swap_out[i]])])

        if self.old_sysstat:
            disk_header = ["tps", "rd_sec/s", "wr_sec/s", "avgrq-sz", "avgqu-sz", "await", "svctm", "%util"]
        else:
            disk_header = ["tps", "rkB/s", "wkB/s", "dkB/s", "areq-sz", "aqu-sz", "await", "%util"]

        def disk_rows(index):
            rows = []
            for name, d in self.disks.items():
                if self.old_sysstat:
                    rows.append((name, [d["tps"][index], d["rkB"][index] * 2, d["wkB"][index] * 2, d["areq"][index] * 2,
                                        d["queue"][index], d["await"][index], 0.1, d["util"][index]]))
                else:
                    rows.append((name, [d["tps"][index], d["rkB"][index], d["wkB"][index], 0.0, d["areq"][index],
                                        d["queue"][index], d["await"][index], d["util"][index]]))
            return rows
        self._write_csv(sadf_dir, "disk", disk_header, "DEV", disk_rows)

        if self.psi:
            self._write_csv(sadf_dir, "psi_cpu", ["%scpu-10", "%scpu-60", "%scpu-300", "%scpu"], None,
                            lambda i: [("", [self.psi_cpu[i]] * 4)])
            self._write_csv(sadf_dir, "psi_io", ["%sio-10", "%sio-60", "%sio-300", "%sio", "%fio-10", "%fio-60", "%fio-300", "%fio"],
                            None, lambda i: [("", [self.psi_io_some[i]] * 4 + [self.psi_io_full[i]] * 4)])
            self._write_csv(sadf_dir, "psi_mem", ["%smem-10", "%smem-60", "%smem-300", "%smem", "%fmem-10", "%fmem-60", "%fmem-300", "%fmem"],
                            None, lambda i: [("", [self.psi_mem_some[i]] * 4 + [self.psi_mem_full[i]] * 4)])

        if self.hardware is not None:
            hw_dir = os.path.join(self.root, "%s_hw_%s" % (self.host, self.tag_stamp))
            os.makedirs(hw_dir, exist_ok=True)
            with open(os.path.join(hw_dir, "hardware.txt"), "w") as handle:
                for key, value in self.hardware.items():
                    handle.write("%s=%s\n" % (key, value))
            with open(os.path.join(hw_dir, "manifest.txt"), "w") as handle:
                handle.write("script=collect-hw-inventory.sh\nhostname=%s\nnotes=\n" % self.host)
        if self.kernel_lines:
            with open(os.path.join(self.root, "kernel_events.log"), "w") as handle:
                handle.write("# source=journalctl\n" + "\n".join(self.kernel_lines) + "\n")
        return run_dir


def hardware_for(disks, logical=2, mem_total_kb=8 * 1024 * 1024, extra=None):
    """hardware.txt dict. disks: {name: {class, parallelism, slaves, display, dm_target}}"""
    values = {"cpu.logical": str(logical), "mem.total_kb": str(mem_total_kb)}
    for name, info in disks.items():
        prefix = "disk.%s." % name
        values[prefix + "class"] = info.get("class", "virtual")
        values[prefix + "display"] = info.get("display", "")
        values[prefix + "parallelism"] = str(info.get("parallelism", "")) if info.get("parallelism") else ""
        values[prefix + "slaves"] = ",".join(info.get("slaves", []))
        values[prefix + "dm_target"] = info.get("dm_target", "")
        values[prefix + "size_bytes"] = str(info.get("size_bytes", 100 * 1024 ** 3))
    if extra:
        values.update(extra)
    return values
