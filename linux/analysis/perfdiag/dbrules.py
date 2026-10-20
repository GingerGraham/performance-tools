"""Database indicators (spec section 10.1).

Each indicator is a `rules.Indicator` with `device = instance.short` and `metric` set to the canonical
metric, so `analysis._detect_all` finds episodes, time above threshold and transients with no special
case: the series travel inside the Indicator, which is why database series can stay out of
`host.series`.

Principle 3 of the method holds here as it does for the OS rules: load indicators (`db_io_wait`,
`db_temp`, `db_concurrency`, `db_cache_hit`) are supporting only. A finding needs a latency, wait or
exhaustion indicator, and those are the primary ones.

Remote instances get their indicators like local ones. What a remote instance may link to is decided
in dbhypotheses.
"""

import math
from dataclasses import dataclass
from typing import Tuple

from .dbengines import SAMPLED
from .rules import PRIMARY, SUPPORTING, Indicator

NAN = float("nan")

# Wait categories whose sessions add up to "waiting on IO".
IO_WAIT_METRICS = ("db.wait.io_read", "db.wait.io_write")
READ_DEVICE_ROLES = ("data", "asm_disk")
COMMIT_DEVICE_ROLES = ("wal", "redo", "log")


@dataclass(frozen=True)
class Spec:
    """How one indicator is made from the canonical metrics of an instance."""

    name: str
    metrics: Tuple[str, ...]      # canonical metrics; more than one are combined (see `combine`)
    combine: str                  # "single", "sum" (finite parts added) or "max" (worst part)
    noun: str                     # what the title says after the instance name: "pg:5432 lock waits"
    unit: str
    direction: str
    role: str
    warn_key: str                 # settings keys; the latency indicators resolve theirs in latency_thresholds
    crit_key: str
    needs_missing: str = ""       # built only when this metric is absent (commit waits stand in for latency)


SPECS = (
    Spec("db_read_latency", ("db.read_latency_ms",), "single", "read latency", "ms", "above", PRIMARY,
         "db.read_latency.warn_ms", "db.read_latency.crit_ms"),
    Spec("db_commit_latency", ("db.commit_latency_ms",), "single", "commit latency", "ms", "above", PRIMARY,
         "db.commit_latency.warn_ms", "db.commit_latency.crit_ms"),
    # PostgreSQL has no commit latency source; its WAL waits are the closest primary signal.
    Spec("db_commit_wait", ("db.wait.commit",), "single", "commit waits", "sessions", "above", PRIMARY,
         "db.commit_wait.warn_sessions", "db.commit_wait.crit_sessions", needs_missing="db.commit_latency_ms"),
    Spec("db_lock_wait", ("db.wait.lock",), "single", "lock waits", "sessions", "above", PRIMARY,
         "db.lock_wait.warn_sessions", "db.lock_wait.crit_sessions"),
    Spec("db_latch_wait", ("db.wait.latch",), "single", "latch waits", "sessions", "above", PRIMARY,
         "db.latch_wait.warn_sessions", "db.latch_wait.crit_sessions"),
    Spec("db_io_wait", IO_WAIT_METRICS, "sum", "IO waits", "sessions", "above", SUPPORTING,
         "db.io_wait.warn_sessions", "db.io_wait.crit_sessions"),
    Spec("db_cpu_demand", ("db.cpu_demand_per_cpu",), "single", "CPU demand per CPU", "sessions / CPU", "above",
         PRIMARY, "db.cpu_demand.warn_per_cpu", "db.cpu_demand.crit_per_cpu"),
    Spec("db_concurrency", ("db.concurrency_per_cpu",), "single", "running sessions per CPU", "sessions / CPU",
         "above", SUPPORTING, "db.cpu_demand.warn_per_cpu", "db.cpu_demand.crit_per_cpu"),
    Spec("db_flush_stall", ("db.flush_stalls_ps",), "single", "buffer flush stalls", "/s", "above", PRIMARY,
         "db.flush_stall.warn_ps", "db.flush_stall.crit_ps"),
    # The gauge is sampled and the wait time is averaged; the worse of the two shows a short queue.
    Spec("db_memory_grants", ("db.memory_grants_pending", "db.wait.memory"), "max", "memory grants pending",
         "queries", "above", PRIMARY, "db.memory_grants.warn", "db.memory_grants.crit"),
    Spec("db_connections", ("db.connections_pct",), "single", "connections in use", "%", "above", PRIMARY,
         "db.connections.warn_pct", "db.connections.crit_pct"),
    Spec("db_worker_queue", ("db.worker_queue", "db.wait.workers"), "max", "worker queue", "tasks", "above",
         PRIMARY, "db.worker_queue.warn", "db.worker_queue.crit"),
    Spec("db_repl_lag", ("db.replication_lag_s",), "single", "replication lag", "s", "above", PRIMARY,
         "db.replication_lag.warn_s", "db.replication_lag.crit_s"),
    Spec("db_cache_hit", ("db.cache_hit_pct",), "single", "buffer cache hit ratio", "%", "below", SUPPORTING,
         "db.cache_hit.warn_pct", "db.cache_hit.crit_pct"),
    Spec("db_temp", ("db.temp_kBps",), "single", "temporary file writes", "kB/s", "above", SUPPORTING,
         "db.temp.warn_kBps", "db.temp.crit_kBps"),
    Spec("db_xact_age", ("db.xact_age_s",), "single", "oldest transaction age", "s", "above", SUPPORTING,
         "db.xact_age.warn_s", "db.xact_age.crit_s"),
    Spec("db_history_list", ("db.history_list",), "single", "history list length", "", "above", SUPPORTING,
         "db.history_list.warn", "db.history_list.crit"),
    Spec("db_compile", ("db.compile_pct",), "single", "compilations", "%", "above", SUPPORTING,
         "db.compile.warn_pct", "db.compile.crit_pct"),
    # MySQL reports dirty pages, MariaDB also checkpoint age; either one filling up is the same story.
    Spec("db_dirty", ("db.dirty_pct", "db.checkpoint_age_pct"), "max", "dirty pages or checkpoint age", "%",
         "above", SUPPORTING, "db.dirty.warn_pct", "db.dirty.crit_pct"),
)


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------
def _finite(value):
    return value is not None and not math.isnan(value)


def combine_series(parts, how):
    """(times, values, interval_s) from one or more Series.

    "sum" and "max" use the finite values at each time and give NaN where no part has one, so a part
    that is missing for a sample does not hide the others.
    """
    if how == "single" or len(parts) == 1:
        return list(parts[0].times), list(parts[0].values), parts[0].interval_s
    lookups = [dict(zip(part.times, part.values)) for part in parts]
    times = sorted(set(time for lookup in lookups for time in lookup))
    pick = sum if how == "sum" else max
    values = []
    for time in times:
        finite = [lookup[time] for lookup in lookups if time in lookup and _finite(lookup[time])]
        values.append(pick(finite) if finite else NAN)
    interval = min((part.interval_s for part in parts if part.interval_s), default=0)
    return times, values, interval


def sampled_labels(instance, metrics, interval_s):
    """The label analysis._detect_all copies into episode notes when any part is a snapshot, not an average."""
    if any(instance.basis.get(metric) == SAMPLED and instance.get(metric) is not None for metric in metrics):
        return ["sampled every %d s, not averaged" % (instance.interval_s or interval_s)]
    return []


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
def first_device(instance, roles):
    for role in roles:
        names = instance.devices.get(role)
        if names:
            return names[0]
    return ""


def latency_thresholds(context, instance, spec):
    """(warn, crit, source text) for a latency indicator.

    A local instance whose data (or log) device is mapped is judged by that device's media class: NVMe
    latency of 8 ms is bad, HDD latency of 8 ms is not. Otherwise, and whenever the user set one of the
    db.*_latency keys, the db.* settings decide.
    """
    settings = context.settings
    user_set = any(settings.sources.get(key, "default") != "default" for key in (spec.warn_key, spec.crit_key))
    if not user_set and instance.local is True:
        roles = READ_DEVICE_ROLES if spec.name == "db_read_latency" else COMMIT_DEVICE_ROLES
        device = first_device(instance, roles)
        if device:
            profile = context.profile(device)
            return profile.warn_ms, profile.crit_ms, profile.threshold_source
    return settings.float(spec.warn_key), settings.float(spec.crit_key), "config" if user_set else "default"


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def build_indicator(context, instance, spec):
    """One Indicator for an instance, or None when its data does not have the metric."""
    if spec.needs_missing and instance.get(spec.needs_missing) is not None:
        return None
    parts = [series for series in (instance.get(metric) for metric in spec.metrics) if series is not None]
    if not parts:
        return None
    times, values, interval_s = combine_series(parts, spec.combine)
    if spec.name in ("db_read_latency", "db_commit_latency"):
        warn, crit, source = latency_thresholds(context, instance, spec)
    else:
        warn, crit = context.settings.float(spec.warn_key), context.settings.float(spec.crit_key)
        source = "config" if any(context.settings.sources.get(key, "default") != "default"
                                 for key in (spec.warn_key, spec.crit_key)) else "default"
    return Indicator(
        name=spec.name, device=instance.short, title="%s %s" % (instance.short, spec.noun), unit=spec.unit,
        warn=warn, crit=crit, direction=spec.direction, times=times, values=values, interval_s=interval_s,
        role=spec.role, threshold_source=source, labels=sampled_labels(instance, spec.metrics, interval_s),
        metric=spec.metrics[0],
        # A restart or statistics reset is a hole in the data even where a sampled gauge kept a value.
        breaks=list(instance.resets))


def instance_indicators(context, instance):
    found = []
    for spec in SPECS:
        indicator = build_indicator(context, instance, spec)
        if indicator is not None:
            found.append(indicator)
    return found


def db_indicators(context):
    """Indicators of every database instance of the host, called from rules.build_indicators."""
    indicators = []
    for instance in context.host.databases:
        indicators.extend(instance_indicators(context, instance))
    return indicators
