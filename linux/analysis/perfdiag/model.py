"""Data classes shared by readers, analysis and reports."""

import collections
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

NAN = float("nan")


@dataclass
class Series:
    """One metric for one device (or the host, with an empty device).

    `times` are epoch seconds at the END of each sample interval. Rate values are
    interval averages, as sar reports them.
    """

    metric: str
    device: str
    times: List[int]
    values: List[float]
    interval_s: int
    source: str = "sar"          # "sar" (sadf export) or "psar"

    def __len__(self):
        return len(self.times)

    def points(self):
        return list(zip(self.times, self.values))

    def clipped(self, start, end):
        """Samples whose interval end falls in [start, end]. None bounds are open."""
        times, values = [], []
        for time_value, value in zip(self.times, self.values):
            if start is not None and time_value < start:
                continue
            if end is not None and time_value > end:
                continue
            times.append(time_value)
            values.append(value)
        return Series(self.metric, self.device, times, values, self.interval_s, self.source)

    def finite_values(self):
        return [value for value in self.values if not math.isnan(value)]


@dataclass
class DiskInfo:
    kname: str
    display: str = ""
    size_bytes: Optional[int] = None
    rotational: str = ""
    model: str = ""
    vendor: str = ""
    media_class: str = "unknown"
    slaves: List[str] = field(default_factory=list)
    holders: List[str] = field(default_factory=list)
    md_level: str = ""
    md_raid_disks: Optional[int] = None
    dm_target: str = ""
    dm_stripes: Optional[int] = None
    transport_hint: str = ""
    hw_raid: str = ""
    parallelism: Optional[int] = None
    scheduler: str = ""
    nr_requests: str = ""
    hw_queues: str = ""

    @property
    def name_for_humans(self):
        return self.display or self.kname


@dataclass
class Filesystem:
    mountpoint: str
    source: str
    fstype: str
    kname: str


@dataclass
class Hardware:
    """Parsed hardware.txt."""

    raw: Dict[str, str] = field(default_factory=dict)
    disks: Dict[str, DiskInfo] = field(default_factory=dict)
    filesystems: List[Filesystem] = field(default_factory=list)
    swap_devices: List[str] = field(default_factory=list)

    def get(self, key, default=""):
        return self.raw.get(key, default)

    def get_int(self, key):
        value = self.raw.get(key, "")
        if value.isdigit():
            return int(value)
        return None

    @property
    def cpu_logical(self):
        return self.get_int("cpu.logical")

    @property
    def mem_total_kb(self):
        return self.get_int("mem.total_kb")

    def find_disk(self, name):
        """Disk by kernel name or by its device-mapper display name."""
        if name in self.disks:
            return self.disks[name]
        for disk in self.disks.values():
            if disk.display and disk.display == name:
                return disk
        return None

    def mounts_on(self, kname):
        """Filesystems whose block device is `kname`, or sits on it through dm/md."""
        found = []
        for filesystem in self.filesystems:
            if filesystem.kname == kname or self.sits_on(filesystem.kname, kname):
                found.append(filesystem)
        return found

    def sits_on(self, upper, lower, depth=0):
        if depth > 8 or upper not in self.disks:
            return False
        for slave in self.disks[upper].slaves:
            if slave == lower or self.sits_on(slave, lower, depth + 1):
                return True
        return False


@dataclass
class Run:
    name: str
    tag: str
    host: str
    path: str
    manifest: Dict[str, str] = field(default_factory=dict)


@dataclass
class KernelEvent:
    time: int
    category: str
    device: str
    text: str


ProcessRow = collections.namedtuple(
    "ProcessRow", "time pid cpu_pct rss_kb majflt_ps rd_kBps wr_kBps iodelay command"
)


@dataclass
class HostData:
    host: str
    series: Dict[Tuple[str, str, str], Series] = field(default_factory=dict)
    interval_s: int = 0
    zone: object = None
    cpus: Optional[int] = None
    mem_total_kb: Optional[int] = None
    hardware: Optional[Hardware] = None
    kernel_events: List[KernelEvent] = field(default_factory=list)
    process_rows: List[ProcessRow] = field(default_factory=list)
    runs: List[Run] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    sysstat_version: str = ""
    psi_expected: bool = False
    distro: str = ""
    kernel: str = ""
    start: Optional[int] = None
    end: Optional[int] = None
    gaps: List[Tuple[int, int]] = field(default_factory=list)
    activities: List[str] = field(default_factory=list)
    data_sources: List[str] = field(default_factory=list)

    def add_series(self, series):
        key = (series.source, series.metric, series.device)
        self.series[key] = series

    def get(self, metric, device="", sources=("sar", "psar")):
        for source in sources:
            found = self.series.get((source, metric, device))
            if found is not None and len(found) > 0:
                return found
        return None

    def devices(self, metric, sources=("sar", "psar")):
        names = []
        for source in sources:
            for (series_source, series_metric, device), series in self.series.items():
                if series_source == source and series_metric == metric and len(series) > 0 and device not in names:
                    names.append(device)
        return sorted(names)

    def has(self, metric, sources=("sar", "psar")):
        return bool(self.devices(metric, sources))

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)


# ---------------------------------------------------------------------------
# Analysis results
# ---------------------------------------------------------------------------
@dataclass
class Episode:
    rule: str
    device: str
    kind: str                     # "sustained" or "transient"
    severity: str                 # "warn" or "crit"
    start: int                    # epoch, start of first interval
    end: int                      # epoch, end of last interval
    duration_s: int
    samples: int
    exceeding_samples: int
    mean: float
    p95: float
    peak: float
    score: float = 0.0
    notes: list = field(default_factory=list)
    id: str = ""
    threshold: dict = field(default_factory=dict)
    interval_limited: bool = False
    impact: str = ""              # for transients: "absorbed" or "impactful"
    evidence: list = field(default_factory=list)


@dataclass
class Hypothesis:
    id: str                        # "H1"
    title: str
    subject: str
    confidence: str                # "high", "medium", "low"
    primary: Optional[Episode]
    supporting: List[Episode] = field(default_factory=list)
    contradicting: List[Episode] = field(default_factory=list)
    other_primary: List[Episode] = field(default_factory=list)
    kernel_events: List[KernelEvent] = field(default_factory=list)
    top_processes: List[dict] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    supporting_facts: List[str] = field(default_factory=list)     # evidence that is not an episode
    contradicting_facts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    recurring: bool = False
    rank_score: float = 0.0
    sequence: int = 0
