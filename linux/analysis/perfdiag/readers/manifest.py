"""manifest.txt, gather_manifest.txt and hardware.txt: key=value, one per line."""

from ..model import DiskInfo, Filesystem, Hardware


def read_key_values(path):
    """Dict from a key=value file. Later duplicate keys win. Blank/odd lines skipped."""
    values = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key] = value
    return values


def _int_or_none(text):
    text = (text or "").strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return None


def _split_list(text):
    return [item for item in (text or "").split(",") if item]


def parse_hardware(raw):
    """Hardware from the hardware.txt dict."""
    hardware = Hardware(raw=dict(raw))

    names = []
    for key in raw:
        if key.startswith("disk."):
            rest = key[len("disk."):]
            # kernel names can contain '/' (cciss/c0d0) but never '.'
            name = rest.rsplit(".", 1)[0]
            if name not in names:
                names.append(name)
    for name in names:
        prefix = "disk.%s." % name
        hardware.disks[name] = DiskInfo(
            kname=name,
            display=raw.get(prefix + "display", ""),
            size_bytes=_int_or_none(raw.get(prefix + "size_bytes")),
            rotational=raw.get(prefix + "rotational", ""),
            model=raw.get(prefix + "model", ""),
            vendor=raw.get(prefix + "vendor", ""),
            media_class=raw.get(prefix + "class", "") or "unknown",
            slaves=_split_list(raw.get(prefix + "slaves")),
            holders=_split_list(raw.get(prefix + "holders")),
            md_level=raw.get(prefix + "md_level", ""),
            md_raid_disks=_int_or_none(raw.get(prefix + "md_raid_disks")),
            dm_target=raw.get(prefix + "dm_target", ""),
            dm_stripes=_int_or_none(raw.get(prefix + "dm_stripes")),
            transport_hint=raw.get(prefix + "transport_hint", ""),
            hw_raid=raw.get(prefix + "hw_raid", ""),
            parallelism=_int_or_none(raw.get(prefix + "parallelism")),
            scheduler=raw.get(prefix + "scheduler", ""),
            nr_requests=raw.get(prefix + "nr_requests", ""),
            hw_queues=raw.get(prefix + "hw_queues", ""),
        )

    index = 1
    while ("fs.%d.mountpoint" % index) in raw:
        prefix = "fs.%d." % index
        hardware.filesystems.append(Filesystem(
            mountpoint=raw.get(prefix + "mountpoint", ""),
            source=raw.get(prefix + "source", ""),
            fstype=raw.get(prefix + "fstype", ""),
            kname=raw.get(prefix + "kname", ""),
        ))
        index += 1

    index = 1
    while ("swap.%d.device" % index) in raw:
        hardware.swap_devices.append(raw["swap.%d.device" % index])
        index += 1
    return hardware
