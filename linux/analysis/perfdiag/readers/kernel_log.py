"""Reader for kernel_events.log written by gather-perf-diag.sh.

Comment lines start with '#': '# source=…' and, for dmesg, '# boot_epoch=…'.
Line formats seen:

    2026-10-16T14:57:49+0100 host kernel: …        journalctl -o short-iso
    2026-10-16T14:57:49.123456+01:00 host kernel:  rsyslog high-resolution
    Oct 16 14:57:49 host kernel: …                  traditional syslog (no year)
    [ 1234.567890] …                                dmesg (seconds since boot)
"""

import datetime
import re

from ..model import KernelEvent
from ..timeutil import parse_iso

CATEGORIES = (
    ("oom", re.compile(r"out of memory|oom-kill|killed process", re.I)),
    ("hung_task", re.compile(r"hung_task|blocked for more than|soft lockup|hard lockup", re.I)),
    ("timeout_reset", re.compile(r"nvme.*(timeout|reset)|scsi.*(timeout|abort|reset)|task abort", re.I)),
    ("io_error", re.compile(r"i/o error|blk_update_request|buffer i/o error", re.I)),
    ("fs_error", re.compile(r"ext4-fs (error|warning)|xfs.*(error|corruption)", re.I)),
    ("raid", re.compile(r"md/raid.*(fail|degraded)", re.I)),
    ("multipath", re.compile(r"multipath.*(fail|reinstated)", re.I)),
    ("link", re.compile(r"link (down|is not ready)", re.I)),
    ("conntrack", re.compile(r"nf_conntrack: table full", re.I)),
    ("alloc_failure", re.compile(r"page allocation failure", re.I)),
    ("thermal", re.compile(r"cpu clock throttled", re.I)),
)

_DEVICE = re.compile(r"\b(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]|dm-\d+|md\d+|mmcblk\d+)\b")
_ISO_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z))\s+(.*)$")
_SYSLOG_LINE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+(.*)$")
_DMESG_LINE = re.compile(r"^\[\s*(\d+\.\d+)\]\s*(.*)$")
_MONTHS = {name: index for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


def categorise(text):
    for name, pattern in CATEGORIES:
        if pattern.search(text):
            return name
    return ""


def device_in(text):
    match = _DEVICE.search(text)
    return match.group(1) if match else ""


def _syslog_epoch(month, day, clock, zone, year_hint, now_epoch):
    hour, minute, second = (int(part) for part in clock.split(":"))
    moment = datetime.datetime(year_hint, _MONTHS[month], int(day), hour, minute, second, tzinfo=zone)
    # A December line read against a January window belongs to the previous year.
    if now_epoch is not None and moment.timestamp() > now_epoch + 86400:
        moment = moment.replace(year=year_hint - 1)
    return int(moment.timestamp())


def parse_kernel_log(text, zone, year_hint, now_epoch=None):
    """(source, [KernelEvent]). `zone` reads syslog lines, which carry no offset."""
    source = ""
    boot_epoch = None
    events = []
    for line in text.splitlines():
        if line.startswith("#"):
            if line.startswith("# source="):
                source = line[len("# source="):].split()[0]
            elif line.startswith("# boot_epoch="):
                try:
                    boot_epoch = int(line[len("# boot_epoch="):].split()[0])
                except ValueError:
                    boot_epoch = None
            continue
        if not line.strip():
            continue
        epoch = None
        message = line
        match = _ISO_LINE.match(line)
        if match:
            try:
                epoch = parse_iso(match.group(1))
            except ValueError:
                epoch = None
            message = match.group(2)
        else:
            match = _SYSLOG_LINE.match(line)
            if match and match.group(1) in _MONTHS:
                epoch = _syslog_epoch(match.group(1), match.group(2), match.group(3), zone, year_hint, now_epoch)
                message = match.group(4)
            else:
                match = _DMESG_LINE.match(line)
                if match and boot_epoch is not None:
                    epoch = boot_epoch + int(float(match.group(1)))
                    message = match.group(2)
        if epoch is None:
            continue
        category = categorise(message)
        if not category:
            continue
        events.append(KernelEvent(time=epoch, category=category, device=device_in(message), text=message.strip()))
    return source, events


def read_kernel_log(path, zone, year_hint, now_epoch=None):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_kernel_log(handle.read(), zone, year_hint, now_epoch)
