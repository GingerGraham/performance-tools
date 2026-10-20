"""`perfdiag inspect`: what is in a bundle, before any analysis."""

from . import loader
from .timeutil import duration_text, stamp, zone_label


def human_bytes(value):
    if value is None:
        return "?"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return ("%d %s" % (size, unit)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024.0
    return "%d B" % value


def inspect_bundle(bundle, out, tz_override=None):
    gather = bundle.gather_manifest
    out.write("Bundle: %s\n" % bundle.source)
    if gather:
        out.write("  gathered %s on %s with %s %s\n" % (
            gather.get("start", "?"), gather.get("hostname", "?"),
            gather.get("script", "gather-perf-diag.sh"), gather.get("script_version", "")))
        out.write("  redacted: %s, kernel log: %s\n" % (gather.get("redacted", "?"), gather.get("kernel_log_source", "?")))
    hosts = bundle.hosts()
    if not hosts:
        out.write("  no run directories found\n")
        return 1
    out.write("  hosts: %s\n" % ", ".join(hosts))

    status = 0
    for name in hosts:
        out.write("\nHost %s\n" % name)
        runs = bundle.runs_for(name)
        try:
            host = loader.load_host(bundle, name, tz_override)
        except loader.LoadError as error:
            out.write("  cannot load: %s\n" % error)
            status = 1
            continue
        zone = host.zone
        out.write("  %s, kernel %s\n" % (host.distro or "distro unknown", host.kernel or "unknown"))
        memory = human_bytes(host.mem_total_kb * 1024) if host.mem_total_kb else "memory unknown"
        out.write("  %s CPUs, %s\n" % (host.cpus if host.cpus else "?", memory))
        out.write("  times shown in %s\n" % zone_label(zone))
        out.write("  runs:\n")
        for run in runs:
            detail = ""
            if run.tag in ("os", "psar"):
                detail = " interval %ss, %s samples" % (run.manifest.get("interval_s", "?"), run.manifest.get("samples", "?"))
            out.write("    %-34s %-6s%s\n" % (run.name, run.tag or "?", detail))
        if host.start is not None:
            out.write("  data: %s to %s (%s), interval %s s\n" % (
                stamp(host.start, zone), stamp(host.end, zone), duration_text(host.end - host.start), host.interval_s))
        partitions = sorted({key[2] for key in host.partition_series})
        if partitions:
            out.write("  partitions: %s (folded into their parent disk)\n" % ", ".join(partitions))
        if host.gaps:
            out.write("  gaps: %d (larger than 2 x interval)\n" % len(host.gaps))
        out.write("  sources: %s\n" % "; ".join(host.data_sources))
        if host.activities:
            out.write("  sadf activities: %s\n" % ", ".join(host.activities))
        missing_psi = [m for m in ("psi.cpu_some", "psi.io_some", "psi.mem_some") if not host.has(m)]
        if host.psi_expected and missing_psi:
            out.write("  PSI: kernel supports it but the sysstat export has none (sysstat older than 12.3.3)\n")
        elif not host.psi_expected:
            out.write("  PSI: not available on this host\n")
        else:
            out.write("  PSI: present\n")
        out.write("  process data (pidstat): %s\n" % (("%d rows" % len(host.process_rows)) if host.process_rows else "none"))
        if bundle.kernel_log_path:
            out.write("  kernel events in window: %d\n" % len(host.kernel_events))
        if host.hardware is not None:
            hardware = host.hardware
            out.write("  inventory: %s logical CPUs, %s socket(s), hypervisor %s\n" % (
                hardware.get("cpu.logical", "?"), hardware.get("cpu.sockets", "?"), hardware.get("cpu.hypervisor", "?")))
            for disk in hardware.disks.values():
                parallelism = disk.parallelism if disk.parallelism is not None else "-"
                out.write("    disk %-12s %-12s %10s  parallelism %s%s\n" % (
                    disk.name_for_humans, disk.media_class, human_bytes(disk.size_bytes), parallelism,
                    ("  on " + ",".join(disk.slaves)) if disk.slaves else ""))
            cloud = hardware.get("cloud.provider")
            if cloud:
                out.write("    cloud: %s %s\n" % (cloud, hardware.get("cloud.instance_type") or hardware.get("cloud.product_name")))
        if host.notes:
            out.write("  notes:\n")
            for note in host.notes:
                out.write("    NOTE: %s\n" % note)
    return status
