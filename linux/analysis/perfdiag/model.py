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
    partitions: Dict[str, str] = field(default_factory=dict)       # partition kname -> disk kname

    def parent_of(self, name):
        """Kernel name of the disk a partition belongs to, or None."""
        return self.partitions.get(name)

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
class DatabaseInstance:
    """One database server seen in the bundle, from a collector run, process data, or both.

    Every database series lives here, never in HostData.series, so the OS rules cannot pick one
    up by accident. `series` is keyed (metric, sub): `sub` is "" for the instance as a whole, a
    device name for per-device series (SQL Server file statistics), or a detail name for wait
    breakdowns (see dbengines for the full list).
    """

    id: str                       # "postgresql:localhost:5432" - stable across runs of one target
    short: str                    # "pg:5432" - used as the indicator device (fits the text tables)
    engine: str                   # postgresql | mysql | mariadb | oracle | mssql
    label: str                    # "PostgreSQL 16.4 (localhost:5432)"
    source: str                   # "collector" or "process"
    local: Optional[bool] = None  # None = unknown
    version: str = ""
    version_num: Optional[int] = None
    collector_schema: int = 0     # 0 for process-only instances
    runs: List[Run] = field(default_factory=list)
    series: Dict[Tuple[str, str], Series] = field(default_factory=dict)   # (metric, sub) -> derived series
    basis: Dict[str, str] = field(default_factory=dict)                   # metric -> "time-averaged" | "sampled"
    settings: Dict[str, str] = field(default_factory=dict)
    paths: Dict[str, List[str]] = field(default_factory=dict)             # role -> paths
    devices: Dict[str, List[str]] = field(default_factory=dict)           # role -> device names in the disk data
    process_keys: List[str] = field(default_factory=list)                 # attribution.process_key values
    process_roles: Dict[str, str] = field(default_factory=dict)           # command -> role ("checkpointer", "DB writer", ...)
    resets: List[int] = field(default_factory=list)                       # epochs of restarts or stats resets
    notes: List[str] = field(default_factory=list)
    instance_name: str = ""       # the server's own name for itself: Oracle SID, SQL Server instance (merging aid)
    locality_basis: str = ""      # why `local` is what it is: socket, loopback, server hostname, local address, none
    tables_present: List[str] = field(default_factory=list)   # collector data files read (pg_waits.csv ...)
    sample_count: int = 0         # summary rows read
    interval_s: int = 0           # the collector's nominal sample interval

    def get(self, metric, sub=""):
        found = self.series.get((metric, sub))
        if found is not None and len(found) > 0:
            return found
        return None

    def subs(self, metric):
        """Sub keys that have data for `metric`, without the instance-wide "" entry."""
        return sorted(sub for (name, sub), series in self.series.items()
                      if name == metric and sub and len(series) > 0)

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)


@dataclass
class KernelEvent:
    time: int
    category: str
    device: str
    text: str
    disks: List[str] = field(default_factory=list)    # whole disks the event applies to, set by the loader
    ignored: bool = False                              # sr*, loop*, fd*: removable or virtual media
    count: int = 1                                     # log lines collapsed into this event


# With pidstat -t each process row is followed by its thread rows ("|__name"). `pid` is the TGID
# of a process row and the TID of a thread row; `tgid` is the owning process for both.
ProcessRow = collections.namedtuple(
    "ProcessRow", "time pid cpu_pct rss_kb majflt_ps rd_kBps wr_kBps iodelay command tgid is_thread",
    defaults=(0, False),
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
    partition_series: Dict[Tuple[str, str, str], Series] = field(default_factory=dict)
    dst_note: str = ""
    databases: List[DatabaseInstance] = field(default_factory=list)

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
    role: str = "primary"         # "supporting": shown only as evidence of another finding


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
    origin: str = "measured"       # "measured", or "kernel" when only the kernel log raised it
    related: List[Tuple[str, Episode]] = field(default_factory=list)   # same problem seen on stacked devices
    # Databases on this host that took part in the window (spec 11.3): one dict per instance, built by
    # dbhypotheses.link_databases. Keys: instance, label, engine, source, statements_ps, transactions_ps,
    # waits [{category, sessions}], read_latency_ms, commit_latency_ms, temp_kBps, write_share_pct,
    # read_share_pct, roles [{role, processes, cpu_pct, rd_kBps, wr_kBps}], roles_by ("io" or "cpu").
    database_context: List[dict] = field(default_factory=list)
    # Hypotheses of the other family that overlap this one: (code, subject), e.g. ("D1", "pg:5432") on an H
    # hypothesis and ("H7", "nvme1n1") on a D hypothesis. Reports render them as "See also".
    links: List[Tuple[str, str]] = field(default_factory=list)
