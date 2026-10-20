"""Database hypotheses D1-D9, their links to the host hypotheses H1-H12, and the engine next steps (spec 11).

A D hypothesis is about one instance (`subject` is the instance's short name, `findings.subject_label`
shows its label). It is built the way an H hypothesis is: `Builder.best_primary` over its primary
indicators, then supporting episodes, facts for and facts against. Ranking is shared with the H codes
through the primary episode's score, so a lock problem on an otherwise idle host ranks first.

Evidence from the OS side of the bundle is used only for a *local* instance. For a remote instance (or one
whose locality is unknown) the OS data belongs to another machine: D hypotheses are built from database
data alone and carry a note saying so.

`link_databases` runs once all hypotheses exist. It adds database context to the H hypotheses an instance
took part in, and the matching facts and "See also" links in both directions.
"""

import math
import re

from . import attribution
from .dbdetect import ENGINE_NAMES, engine_family
from .dbrules import COMMIT_DEVICE_ROLES, READ_DEVICE_ROLES
from .findings import RELATED_WORD, format_number, format_value
from .hypotheses import confidence_for, is_database_code, overlaps

# H hypotheses a database can take part in. H4, H9 and H10 do not involve a database's own work.
LINKED_H_CODES = ("H1", "H2", "H3", "H5", "H6", "H7", "H8", "H12")
# Their subject is a device; the others are the host or a CPU.
DISK_H_CODES = ("H1", "H2", "H7")

DISK_LATENCY_RULES = ("disk_await", "disk_r_await", "disk_w_await")
READ_LATENCY_RULES = ("disk_r_await", "disk_await")
WRITE_LATENCY_RULES = ("disk_w_await", "disk_await")

# Wait detail (a lock-manager or log-buffer wait name) must average this many sessions to be worth saying.
MIN_DETAIL_SESSIONS = 0.1
# D5: more idle CPU than this while the database wants CPU points at a limit set below the host.
HOST_IDLE_AGAINST_PCT = 40.0
# D6: backends writing more than this share of the flushed buffers themselves.
BACKEND_WRITES_FACT_PCT = 50.0
# D4: CPU demand within this share of its warn threshold counts as "near warn".
NEAR_WARN_SHARE = 0.7
# D1: the read rate in the last third of the window is under this share of the first third.
READ_RATE_FELL_SHARE = 0.7
MIN_SAMPLES_PER_THIRD = 2
TOP_WAIT_CATEGORIES = 3
TOP_ROLES = 4

ABOVE_BLOCK_LAYER_NOTE = (
    "the device's own latency stayed under its warn threshold while the database waited; look above the "
    "block layer: filesystem locks, cgroup io.max throttling, a network filesystem, or memory pressure "
    "evicting the page cache")
REMOTE_NOTE = ("the database server is remote (%s); the OS data in this bundle is from %s, the collecting "
               "machine, and is not used as evidence")
UNKNOWN_LOCALITY_NOTE = ("it is not known whether the database server (%s) runs on %s; the OS data in this "
                         "bundle is not used as evidence")
UNCONFIRMED_DEVICE = "not confirmed as this database's device"
RESOURCE_MANAGER_FACT = "Resource Manager limited CPU; the plan, not the host, set the limit"
COLLECTOR_HINT = ("Run linux/db/collect-%s-metrics.sh alongside collect-os-metrics.sh during the next occurrence "
                  "for database-level evidence.")

# How a session-count indicator reads in a sentence: "sessions waiting on IO averaged 6.1".
SESSION_PHRASES = {
    "db_io_wait": "sessions waiting on IO", "db_lock_wait": "sessions waiting on locks",
    "db_latch_wait": "sessions waiting on latches", "db_commit_wait": "sessions waiting on commit",
}


# ---------------------------------------------------------------------------
# Numbers over a window
# ---------------------------------------------------------------------------
def window_values(series, start, end):
    """Finite values of a series whose sample time is in (start, end]."""
    if series is None:
        return []
    return [value for time, value in zip(series.times, series.values) if start < time <= end and not math.isnan(value)]


def window_mean(series, start, end):
    values = window_values(series, start, end)
    return sum(values) / len(values) if values else None


def instance_mean(instance, metric, start, end, sub=""):
    return window_mean(instance.get(metric, sub), start, end)


def top_details(instance, category, start, end, minimum=MIN_DETAIL_SESSIONS):
    """[(name, mean sessions)] of the detail names under db.wait.<category>, largest first, above `minimum`."""
    found = []
    for sub in instance.subs("db.wait." + category):
        mean = instance_mean(instance, "db.wait." + category, start, end, sub)
        if mean is not None and mean >= minimum:
            found.append((sub, mean))
    found.sort(key=lambda item: (-item[1], item[0]))
    return found


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------
def tolerance(builder, primary):
    """Overlap tolerance: the host interval, or the database's when it samples less often."""
    indicator = builder.result.indicator(primary.rule, primary.device)
    return max(builder.interval, indicator.interval_s if indicator else 0)


def own_episodes(builder, instance, primary, rules, limit=1):
    """Episodes of this instance's own indicators that overlap the primary."""
    return builder.overlapping(primary, rules, instance.short, tolerance(builder, primary))[:limit]


def os_episodes(builder, instance, primary, rules, devices=None, limit=1):
    """OS episodes that overlap the primary: none unless the instance is local.

    With `devices`, the best `limit` per device; without, the best `limit` of the host-wide rules.
    """
    if instance.local is not True:
        return []
    interval = tolerance(builder, primary)
    if devices is None:
        return builder.overlapping(primary, rules, None, interval)[:limit]
    found = []
    for device in devices:
        found.extend(builder.overlapping(primary, rules, device, interval)[:limit])
    return found


def episode_intersects(builder, primary, episode):
    interval = tolerance(builder, primary)
    return episode.start - interval < primary.end and episode.end + interval > primary.start


def unconfirmed_disk_facts(builder, instance, primary, rules, devices):
    """Disk episodes worded as facts when the instance's files could not be placed on any device."""
    if instance.local is not True or devices:
        return []
    facts = []
    for episode in builder.overlapping(primary, rules, None, tolerance(builder, primary))[:2]:
        word = RELATED_WORD.get(episode.rule, episode.rule)
        facts.append("%s on %s averaged %s (%s)" % (word, episode.device, format_value(episode.mean, "ms"),
                                                    UNCONFIRMED_DEVICE))
    return facts


def instance_text(instance):
    """The server's own name for itself (the manifest's db_instance), for the remote note."""
    from .dbload import instance_text_of
    return instance_text_of(instance.runs[0]) if instance.runs else instance.id


def locality_notes(builder, instance):
    if instance.local is True:
        return []
    if instance.local is False:
        return [REMOTE_NOTE % (instance_text(instance), builder.host.host)]
    return [UNKNOWN_LOCALITY_NOTE % (instance_text(instance), builder.host.host)]


# Which disks belong to a hypothesis: (metric with per-device values or None, device roles).
DEVICE_SCOPE = {
    "D1": ("db.read_latency_ms", READ_DEVICE_ROLES),
    "D2": ("db.commit_latency_ms", COMMIT_DEVICE_ROLES),
    "D6": (None, READ_DEVICE_ROLES),
    "D9": (None, READ_DEVICE_ROLES),
}


def scoped_devices(builder, instance, code, primary):
    """Disk names a D hypothesis is about, in the order they were found.

    With per-device latency (SQL Server file statistics) only the devices whose latency passed the
    indicator's warn threshold in the window; otherwise the devices of the instance's data or log files.
    """
    metric, roles = DEVICE_SCOPE.get(code, (None, ()))
    if instance.local is not True:
        return []
    subs = instance.subs(metric) if metric else []
    if subs:
        indicator = builder.result.indicator(primary.rule, primary.device) if primary is not None else None
        if indicator is None or primary is None:
            return list(subs)
        hot = []
        for device in subs:
            mean = instance_mean(instance, metric, primary.start, primary.end, device)
            if mean is not None and mean > indicator.warn:
                hot.append(device)
        return hot or list(subs)
    names = []
    for role in roles:
        for device in instance.devices.get(role, []):
            if device not in names:
                names.append(device)
    return names


def attribution_device(builder, hypothesis, instance):
    """Device whose processes a D hypothesis lists: its first mapped data or log device."""
    names = scoped_devices(builder, instance, hypothesis.id, hypothesis.primary)
    return names[0] if names else None


# ---------------------------------------------------------------------------
# Building D hypotheses
# ---------------------------------------------------------------------------
def make_database_hypothesis(builder, instance, code, picked, supporting=(), facts_for=(), facts_against=(),
                             notes=()):
    primary, others, recurring = picked
    return builder.make(
        code, instance.short, primary, others, recurring, list(supporting), [], list(facts_for),
        list(facts_against), extra_notes=locality_notes(builder, instance) + list(notes))


def d1_reads(builder, instance):
    picked = builder.best_primary(("db_read_latency",), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    devices = scoped_devices(builder, instance, "D1", primary)
    supporting = own_episodes(builder, instance, primary, ("db_io_wait",))
    supporting += own_episodes(builder, instance, primary, ("db_cache_hit",))
    supporting += os_episodes(builder, instance, primary, READ_LATENCY_RULES, devices)
    supporting += os_episodes(builder, instance, primary, ("mem_avail",))
    facts_for = unconfirmed_disk_facts(builder, instance, primary, READ_LATENCY_RULES, devices)
    facts_against = []
    if read_rate_fell(instance, primary):
        facts_against.append("the read rate fell during the window (fewer reads, slower: this points to "
                             "contention, not volume)")
    notes = []
    if device_latency_stayed_low(builder, primary, devices):
        notes.append(ABOVE_BLOCK_LAYER_NOTE)
    return make_database_hypothesis(builder, instance, "D1", picked, supporting, facts_for, facts_against, notes)


def read_rate_fell(instance, primary):
    """True when the last third of the window read under READ_RATE_FELL_SHARE of the first third."""
    series = instance.get("db.read_kBps")
    if series is None:
        return False
    values = [value for time, value in zip(series.times, series.values) if primary.start < time <= primary.end]
    third = len(values) // 3
    if third < MIN_SAMPLES_PER_THIRD:
        return False
    first = [value for value in values[:third] if not math.isnan(value)]
    last = [value for value in values[-third:] if not math.isnan(value)]
    if not first or not last:
        return False
    first_mean = sum(first) / len(first)
    return first_mean > 0 and (sum(last) / len(last)) < READ_RATE_FELL_SHARE * first_mean


def device_latency_stayed_low(builder, primary, devices):
    """True when latency of these devices is measured and has no episode (of any kind) near the window."""
    measured = any(builder.result.indicator(rule, device) is not None for device in devices for rule in DISK_LATENCY_RULES)
    if not measured:
        return False
    return not any(episode.rule in DISK_LATENCY_RULES and episode.device in devices
                   and episode_intersects(builder, primary, episode) for episode in builder.result.episodes)


def d2_commit(builder, instance):
    picked = builder.best_primary(("db_commit_latency", "db_commit_wait"), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    start, end = primary.start, primary.end
    devices = scoped_devices(builder, instance, "D2", primary)
    supporting = os_episodes(builder, instance, primary, WRITE_LATENCY_RULES, devices)
    facts_for = unconfirmed_disk_facts(builder, instance, primary, WRITE_LATENCY_RULES, devices)
    replica = instance_mean(instance, "db.wait.replica", start, end)
    if replica is not None and replica >= builder.result.settings.float("db.commit_wait.warn_sessions"):
        facts_for.append("synchronous replica waits averaged %s sessions in this window" % format_number(replica))
    for name, mean in top_details(instance, "commit", start, end):
        if name == "log buffer space":
            facts_for.append("log buffer space waits averaged %s sessions in this window" % format_number(mean))
    return make_database_hypothesis(builder, instance, "D2", picked, supporting, facts_for)


def d3_locks(builder, instance):
    picked = builder.best_primary(("db_lock_wait",), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    supporting = own_episodes(builder, instance, primary, ("db_xact_age",))
    supporting += own_episodes(builder, instance, primary, ("db_history_list",))
    facts_for = []
    deadlocks = instance_mean(instance, "db.deadlocks_ps", primary.start, primary.end)
    if deadlocks is not None and round(deadlocks * (primary.end - primary.start)) >= 1:
        count = int(round(deadlocks * (primary.end - primary.start)))
        facts_for.append("%d deadlock%s in this window" % (count, "" if count == 1 else "s"))
    idle = idle_host_fact(builder, instance, primary)
    if idle:
        facts_for.append(idle)
    return make_database_hypothesis(builder, instance, "D3", picked, supporting, facts_for)


def idle_host_fact(builder, instance, primary):
    """'host CPU idle averaged 94 % and no disk latency episode': the waits are not explained by the host."""
    if instance.local is not True:
        return ""
    idle = window_mean(builder.host.get("cpu.idle_pct", "all"), primary.start, primary.end)
    measured = any(key[0] in DISK_LATENCY_RULES for key in builder.result.indicators)
    if idle is None or not measured:
        return ""
    if any(episode_intersects(builder, primary, episode) for episode in builder.result.episodes
           if episode.rule in DISK_LATENCY_RULES or episode.rule == "cpu_busy"):
        return ""
    return "host CPU idle averaged %.0f %% and no disk latency episode" % idle


def d4_latches(builder, instance):
    picked = builder.best_primary(("db_latch_wait",), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    start, end = primary.start, primary.end
    supporting = own_episodes(builder, instance, primary, ("db_cpu_demand",))
    facts_for = []
    if not supporting:
        demand = instance_mean(instance, "db.cpu_demand_per_cpu", start, end)
        warn = builder.result.settings.float("db.cpu_demand.warn_per_cpu")
        if demand is not None and demand >= NEAR_WARN_SHARE * warn:
            facts_for.append("database CPU demand averaged %s per CPU, near its warning threshold of %s" % (
                format_number(demand), format_number(warn)))
    # "class:<name>" entries are the time no listed event explained: not a name worth showing.
    names = [item for item in top_details(instance, "latch", start, end) if not item[0].startswith("class:")]
    names = names[:TOP_WAIT_CATEGORIES]
    if names:
        facts_for.append("latch waits by name: " + ", ".join(
            "%s %s" % (name.split(":", 1)[1] if name.startswith("LWLock:") else name, format_number(mean))
            for name, mean in names) + " sessions")
    facts_for.extend(tempdb_latch_fact(instance, start, end, builder.result.settings))
    return make_database_hypothesis(builder, instance, "D4", picked, supporting, facts_for)


def tempdb_latch_fact(instance, start, end, settings):
    """SQL Server PAGELATCH waits while tempdb is being written: allocation contention is possible."""
    if instance.engine != "mssql":
        return []
    pagelatch = sum(mean for name, mean in top_details(instance, "latch", start, end) if name.startswith("PAGELATCH_"))
    temp = instance_mean(instance, "db.temp_kBps", start, end)
    if pagelatch > 0 and temp is not None and temp >= settings.float("db.temp.warn_kBps"):
        return ["PAGELATCH waits averaged %s sessions while tempdb files were written at %s: tempdb allocation "
                "contention is possible" % (format_number(pagelatch), format_value(temp, "kB/s"))]
    return []


def d5_cpu(builder, instance):
    picked = builder.best_primary(("db_cpu_demand",), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    start, end = primary.start, primary.end
    supporting = own_episodes(builder, instance, primary, ("db_compile",))
    for rule in ("cpu_busy", "runq", "psi_cpu"):
        supporting += os_episodes(builder, instance, primary, (rule,))
    facts_for, facts_against = [], []
    if any(name == "resmgr:cpu quantum" for name, _ in top_details(instance, "cpu", start, end)):
        facts_for.append(RESOURCE_MANAGER_FACT)
    if instance.local is True:
        idle = window_mean(builder.host.get("cpu.idle_pct", "all"), start, end)
        if idle is not None and idle > HOST_IDLE_AGAINST_PCT:
            facts_against.append("host CPU idle averaged %.0f %% while database CPU demand was high; this points "
                                 "at Resource Manager, a cgroup or CPU affinity" % idle)
    return make_database_hypothesis(builder, instance, "D5", picked, supporting, facts_for, facts_against)


def d6_flush(builder, instance):
    picked = builder.best_primary(("db_flush_stall",), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    start, end = primary.start, primary.end
    devices = scoped_devices(builder, instance, "D6", primary)
    supporting = own_episodes(builder, instance, primary, ("db_dirty",))
    supporting += os_episodes(builder, instance, primary, WRITE_LATENCY_RULES, devices)
    facts_for = unconfirmed_disk_facts(builder, instance, primary, WRITE_LATENCY_RULES, devices)
    backend = instance_mean(instance, "db.backend_writes_pct", start, end)
    if backend is not None and backend > BACKEND_WRITES_FACT_PCT:
        facts_for.append("backends wrote %.0f %% of the flushed buffers themselves (the background writer and "
                         "checkpointer did not keep up)" % backend)
    requested = checkpoint_fact(instance, start, end, builder.result.settings)
    if requested:
        facts_for.append(requested)
    return make_database_hypothesis(builder, instance, "D6", picked, supporting, facts_for)


def checkpoint_fact(instance, start, end, settings):
    """'N of M checkpoints were requested ...' when at least two happened and enough were requested.

    Checkpoints come every few minutes at best: too sparse for episode detection, so this is a window fact.
    """
    requested, timed = instance.get("db.checkpoints", "requested"), instance.get("db.checkpoints", "timed")
    if requested is None or timed is None:
        return ""
    asked = sum(window_values(requested, start, end))
    total = asked + sum(window_values(timed, start, end))
    if total < 2 or 100.0 * asked / total < settings.float("db.checkpoint_req.warn_pct"):
        return ""
    return "%d of %d checkpoints were requested (WAL volume reached max_wal_size), not timed" % (
        round(asked), round(total))


def d7_memory_grants(builder, instance):
    picked = builder.best_primary(("db_memory_grants",), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    supporting = own_episodes(builder, instance, primary, ("db_temp",))
    for rule in ("mem_avail", "psi_mem_some", "psi_mem_full"):
        supporting += os_episodes(builder, instance, primary, (rule,))
    return make_database_hypothesis(builder, instance, "D7", picked, supporting)


def d8_exhaustion(builder, instance):
    picked = builder.best_primary(("db_connections", "db_worker_queue"), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    supporting = own_episodes(builder, instance, primary, ("db_lock_wait",))
    facts_for = []
    if supporting:
        facts_for.append("sessions waiting on locks overlapped: blocked sessions pile up and hold their connections")
    return make_database_hypothesis(builder, instance, "D8", picked, supporting, facts_for)


def d9_replication(builder, instance):
    picked = builder.best_primary(("db_repl_lag",), instance.short)
    if picked is None:
        return None
    primary = picked[0]
    devices = scoped_devices(builder, instance, "D9", primary)
    supporting = own_episodes(builder, instance, primary, ("db_io_wait",))
    supporting += os_episodes(builder, instance, primary, DISK_LATENCY_RULES, devices)
    facts_for = unconfirmed_disk_facts(builder, instance, primary, DISK_LATENCY_RULES, devices)
    return make_database_hypothesis(builder, instance, "D9", picked, supporting, facts_for)


D_BUILDERS = (d1_reads, d2_commit, d3_locks, d4_latches, d5_cpu, d6_flush, d7_memory_grants, d8_exhaustion,
              d9_replication)


def build_database_hypotheses(builder):
    """D1-D9 for every instance, in instance order."""
    found = []
    for instance in builder.host.databases:
        for build in D_BUILDERS:
            hypothesis = build(builder, instance)
            if hypothesis is not None:
                found.append(hypothesis)
    return found


# ---------------------------------------------------------------------------
# Links between H and D hypotheses (spec 11.3)
# ---------------------------------------------------------------------------
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def instance_device_names(instance):
    """Every disk name the instance's files or per-device statistics are on."""
    names = []
    for found in instance.devices.values():
        names.extend(found)
    for metric in ("db.read_latency_ms", "db.commit_latency_ms", "db.read_kBps", "db.write_kBps"):
        names.extend(instance.subs(metric))
    return set(names)


def touches_device(builder, instance, device, allowed=None):
    """True when `device` (or what it sits on or under) is one of the instance's disks, within `allowed`."""
    names = builder.device_names(device)
    pool = instance_device_names(instance) if allowed is None else set(allowed)
    return bool(names & pool)


def linked_instances(builder, hypothesis):
    """Local instances that took part in an H hypothesis: by process in its top list, or by device."""
    top_commands = {entry["command"] for entry in hypothesis.top_processes}
    found = []
    for instance in builder.host.databases:
        if instance.local is not True:
            continue
        by_process = bool(top_commands & set(instance.process_keys))
        by_device = (hypothesis.id in DISK_H_CODES and hypothesis.subject != "cloud-sum"
                     and touches_device(builder, instance, hypothesis.subject))
        if by_process or by_device:
            found.append(instance)
    return found


def link_databases(builder, found):
    """Database context, facts and links between the H hypotheses and the D hypotheses of an instance."""
    database_hypotheses = [h for h in found if is_database_code(h.id) and h.primary is not None]
    for hypothesis in found:
        if hypothesis.id not in LINKED_H_CODES or hypothesis.primary is None or hypothesis.origin != "measured":
            continue
        for instance in linked_instances(builder, hypothesis):
            hypothesis.database_context.append(database_context(builder, instance, hypothesis))
            for database in database_hypotheses:
                if database.subject == instance.short:
                    link_pair(builder, instance, hypothesis, database)


def link_pair(builder, instance, host_hypothesis, database):
    """Facts and links both ways when the two overlap in time (and, for device codes, on a disk)."""
    interval = max(tolerance(builder, database.primary), builder.interval)
    if not overlaps(database.primary, host_hypothesis.primary, interval):
        return
    if host_hypothesis.id in DISK_H_CODES and host_hypothesis.subject != "cloud-sum":
        scope = scoped_devices(builder, instance, database.id, database.primary)
        if database.id in DEVICE_SCOPE and scope and not touches_device(builder, instance, host_hypothesis.subject, scope):
            return
    add_fact(host_hypothesis, database_fact(builder, instance, database, host_hypothesis.primary))
    add_fact(database, host_fact(builder, host_hypothesis))
    add_link(host_hypothesis, (database.id, database.subject))
    add_link(database, (host_hypothesis.id, host_hypothesis.subject))


def add_fact(hypothesis, text):
    """Adds a supporting fact. A fact only raises confidence, so a forced 'high' (kernel evidence) stays."""
    if not text or text in hypothesis.supporting_facts:
        return
    hypothesis.supporting_facts.append(text)
    supporting = len(hypothesis.supporting) + len(hypothesis.supporting_facts)
    contradicting = len(hypothesis.contradicting) + len(hypothesis.contradicting_facts)
    recomputed = confidence_for(supporting, contradicting)
    if CONFIDENCE_ORDER[recomputed] > CONFIDENCE_ORDER[hypothesis.confidence]:
        hypothesis.confidence = recomputed


def add_link(hypothesis, link):
    if link not in hypothesis.links:
        hypothesis.links.append(link)


def database_fact(builder, instance, database, window):
    """'PostgreSQL (pg:5432) sessions waiting on IO averaged 6.1 in this window (D1)' for an H hypothesis."""
    indicator = None
    if database.id == "D1":
        indicator = builder.result.indicator("db_io_wait", instance.short)
    if indicator is None or window_mean(indicator, window.start, window.end) is None:
        indicator = builder.result.indicator(database.primary.rule, database.primary.device)
    if indicator is None:
        return ""
    mean = window_mean(indicator, window.start, window.end)
    if mean is None:
        mean = database.primary.mean
    if indicator.unit == "sessions":
        phrase, value = SESSION_PHRASES.get(indicator.name, indicator.title), format_number(mean)
    else:
        phrase, value = indicator.title[len(instance.short) + 1:], format_value(mean, indicator.unit)
    return "%s (%s) %s averaged %s in this window (%s)" % (
        ENGINE_NAMES.get(instance.engine, instance.engine), instance.short, phrase, value, database.id)


def host_fact(builder, hypothesis):
    """'nvme1n1 (/var/lib/pgsql) write await averaged 48 ms (H7)' for a D hypothesis."""
    primary = hypothesis.primary
    indicator = builder.result.indicator(primary.rule, primary.device)
    if primary.rule in RELATED_WORD:
        subject = "%s %s" % (builder.label_for(primary.device), RELATED_WORD[primary.rule])
        return "%s averaged %s (%s)" % (subject, format_value(primary.mean, "ms"), hypothesis.id)
    title = indicator.title if indicator is not None else "%s %s" % (primary.rule, primary.device)
    unit = indicator.unit if indicator is not None else (primary.threshold or {}).get("unit", "")
    return "%s averaged %s (%s)" % (title.strip(), format_value(primary.mean, unit), hypothesis.id)


# ---------------------------------------------------------------------------
# Database context of an H hypothesis
# ---------------------------------------------------------------------------
def database_context(builder, instance, hypothesis):
    """What the database was doing over the hypothesis's primary window (keys: see model.Hypothesis)."""
    start, end = hypothesis.primary.start, hypothesis.primary.end
    waits = []
    for category in sorted({name[len("db.wait."):] for (name, sub) in instance.series
                            if name.startswith("db.wait.") and sub == ""}):
        mean = instance_mean(instance, "db.wait." + category, start, end)
        if mean is not None and mean > 0:
            waits.append({"category": category, "sessions": mean})
    waits.sort(key=lambda item: (-item["sessions"], item["category"]))
    by = "io" if attribution_kind(hypothesis) == "disk" else "cpu"
    entry = {
        "instance": instance.short, "label": instance.label, "engine": instance.engine, "source": instance.source,
        "statements_ps": instance_mean(instance, "db.statements_ps", start, end),
        "transactions_ps": instance_mean(instance, "db.transactions_ps", start, end),
        "waits": waits[:TOP_WAIT_CATEGORIES],
        "read_latency_ms": instance_mean(instance, "db.read_latency_ms", start, end),
        "commit_latency_ms": instance_mean(instance, "db.commit_latency_ms", start, end),
        "temp_kBps": instance_mean(instance, "db.temp_kBps", start, end),
        "write_share_pct": None, "read_share_pct": None,
        "roles": role_breakdown(builder.host, instance, start, end, by),
        "roles_by": by,
    }
    if hypothesis.id in DISK_H_CODES and hypothesis.subject != "cloud-sum":
        entry["write_share_pct"] = device_share(builder, instance, hypothesis.subject, "write", start, end)
        entry["read_share_pct"] = device_share(builder, instance, hypothesis.subject, "read", start, end)
    return entry


def attribution_kind(hypothesis):
    from .hypotheses import ATTRIBUTION_KIND
    return ATTRIBUTION_KIND.get(hypothesis.id, "cpu")


def device_share(builder, instance, device, direction, start, end):
    """The database's share (0-100) of the device's reads or writes, or None when either side is unknown.

    The per-device series (SQL Server file statistics) is exact. The instance-wide series stands for the
    device only when every disk the instance uses is the same one.
    """
    metric = "db.%s_kBps" % direction
    series = instance.get(metric, device)
    if series is None:
        firsts = {names[0] for names in instance.devices.values() if names}
        if len(firsts) == 1 and touches_device(builder, instance, device):
            series = instance.get(metric)
    own = window_mean(series, start, end)
    total = window_mean(builder.host.get("disk.%s_kBps" % direction, device), start, end)
    if own is None or not total or total <= 0:
        return None
    return min(100.0, 100.0 * own / total)


def role_breakdown(host, instance, start, end, by):
    """[{role, processes, cpu_pct, rd_kBps, wr_kBps}] for the instance's processes, busiest first.

    Means are per pidstat sample in the window (a process absent from a sample counts 0), the way
    attribution.top_processes counts them.
    """
    if not instance.process_roles or not host.process_rows:
        return []
    rows = [row for row in host.process_rows if not row.is_thread and start < row.time <= end]
    samples = len({row.time for row in rows})
    if not samples:
        return []
    totals = {}
    for row in rows:
        role = instance.process_roles.get(row.command)
        if role is None:
            continue
        entry = totals.setdefault(role, {"pids": set(), "cpu_pct": 0.0, "rd_kBps": 0.0, "wr_kBps": 0.0})
        entry["pids"].add(row.pid)
        for key in ("cpu_pct", "rd_kBps", "wr_kBps"):
            value = getattr(row, key)
            if value == value:
                entry[key] += value
    roles = [{"role": role, "processes": len(entry["pids"]), "cpu_pct": entry["cpu_pct"] / samples,
              "rd_kBps": entry["rd_kBps"] / samples, "wr_kBps": entry["wr_kBps"] / samples}
             for role, entry in totals.items()]
    measure = (lambda item: item["rd_kBps"] + item["wr_kBps"]) if by == "io" else (lambda item: item["cpu_pct"])
    roles = [item for item in roles if measure(item) > 0]
    roles.sort(key=lambda item: (-measure(item), item["role"]))
    return roles[:TOP_ROLES]


# ---------------------------------------------------------------------------
# Next steps (spec 11.2)
# ---------------------------------------------------------------------------
# {now:NAME} becomes " (now VALUE)" and {is:NAME} " is VALUE", each only when the run recorded the
# setting; otherwise it leaves no trace.
DB_NEXT_STEPS = {
    # The checkpoint line the OS hypotheses always carried when a PostgreSQL run was in the bundle.
    ("H1", "postgresql"): ("PostgreSQL: review checkpoint settings (checkpoint_timeout, max_wal_size) to spread "
                           "write bursts.",),
    ("H7", "postgresql"): ("PostgreSQL: review checkpoint settings (checkpoint_timeout, max_wal_size) to spread "
                           "write bursts.",),

    ("D1", "postgresql"): (
        "Find the queries with the most read time: pg_stat_statements ordered by shared_blk_read_time "
        "(blk_read_time before 17).",
        "Check shared_buffers{now:shared_buffers} against the working set."),
    ("D1", "mysql"): (
        "Find the statements that read the most: sys.statements_with_full_table_scans, or "
        "performance_schema.events_statements_summary_by_digest ordered by SUM_ROWS_EXAMINED.",
        "Check innodb_buffer_pool_size{now:innodb_buffer_pool_size} against the working set."),
    ("D1", "oracle"): (
        "Find the SQL with the most physical reads: v$sqlstats ordered by disk_reads.",
        "Check sga_target{now:sga_target} and db_cache_size{now:db_cache_size} against the working set."),
    ("D1", "mssql"): (
        "Compare the per-file read stall in sys.dm_io_virtual_file_stats with the device latency shown here.",
        "Find the top readers in Query Store or sys.dm_exec_query_stats ordered by total_physical_reads."),

    ("D2", "postgresql"): (
        "Put pg_wal on a low-latency device; check wal_sync_method{now:wal_sync_method} and whether "
        "synchronous_commit{now:synchronous_commit} or synchronous_standby_names adds replica round-trips.",),
    ("D2", "mysql"): (
        "innodb_flush_log_at_trx_commit{is:innodb_flush_log_at_trx_commit} and sync_binlog{is:sync_binlog}; "
        "values other than 1 trade durability for latency and need sign-off.",),
    ("D2", "oracle"): (
        "Compare log file sync with log file parallel write: close values mean the redo device is slow; a large "
        "gap means CPU or commit frequency.",),
    ("D2", "mssql"): (
        "Check WRITELOG against the LOG file stalls; consider delayed durability only with sign-off.",),

    ("D3", "postgresql"): (
        "Find blockers: SELECT pid, pg_blocking_pids(pid), state, xact_start, query FROM pg_stat_activity WHERE "
        "cardinality(pg_blocking_pids(pid)) > 0;",),
    ("D3", "mysql"): (
        "Find blockers: sys.innodb_lock_waits (MySQL 8), or information_schema.INNODB_TRX for long transactions.",),
    ("D3", "oracle"): ("Find blockers: v$session where blocking_session is not null.",),
    ("D3", "mssql"): (
        "Find blockers: sys.dm_exec_requests where blocking_session_id <> 0; enable the blocked process report "
        "for history.",),

    ("D4", "postgresql"): (
        "Group the waits by name during the next occurrence: SELECT wait_event, count(*) FROM pg_stat_activity "
        "WHERE wait_event_type = 'LWLock' GROUP BY 1 ORDER BY 2 DESC;",),
    ("D4", "mysql"): (
        "Find the contended mutex or latch: performance_schema.events_waits_summary_global_by_event_name ordered "
        "by SUM_TIMER_WAIT, and the SEMAPHORES section of SHOW ENGINE INNODB STATUS.",),
    ("D4", "oracle"): (
        "Name the dominant latch or buffer wait in v$system_event, then read p1 and p2 of the waiting sessions in "
        "v$session to find the object.",),
    ("D4", "mssql"): (
        "Find the hot pages: sys.dm_os_waiting_tasks where wait_type like 'PAGELATCH%' (resource_description is "
        "database:file:page); tempdb allocation contention eases with more equally sized tempdb files.",),

    ("D5", "postgresql"): (
        "Find the queries using the most CPU: pg_stat_statements ordered by total_exec_time (total_time before 13).",),
    ("D5", "mysql"): ("Find the statements using the most time: sys.statement_analysis ordered by total_latency.",),
    ("D5", "oracle"): (
        "Top SQL by CPU in v$sqlstats; check resource_manager_plan{now:resource_manager_plan}.",),
    ("D5", "mssql"): (
        "Find the top CPU consumers: sys.dm_exec_query_stats ordered by total_worker_time; check max degree of "
        "parallelism and cost threshold for parallelism.",),

    ("D6", "postgresql"): (
        "Raise max_wal_size{now:max_wal_size} if checkpoints are requested; tune bgwriter_lru_maxpages if backends "
        "write their own buffers.",),
    ("D6", "mysql"): (
        "Check innodb_io_capacity{now:innodb_io_capacity} and innodb_io_capacity_max{now:innodb_io_capacity_max} "
        "against what the device sustains.",),
    ("D6", "oracle"): (
        "Check db_writer_processes{now:db_writer_processes} and filesystemio_options{now:filesystemio_options}: "
        "free buffer waits mean the database writers cannot keep up.",),
    ("D6", "mssql"): (
        "Compare checkpoint pages and lazy writes in sys.dm_os_performance_counters with the data file write "
        "stall in sys.dm_io_virtual_file_stats.",),

    ("D7", "mssql"): (
        "Find the queries waiting for a grant: sys.dm_exec_query_memory_grants where grant_time is null; check "
        "max server memory and Resource Governor memory limits.",),

    ("D8", "any"): (
        "Compare client pool sizes with max_connections{now:max_connections}; check for sessions left open.",),

    ("D9", "postgresql"): (
        "Compare the standby's replay position with the primary (pg_stat_replication, replay_lag) and look for "
        "long queries on the standby that delay replay.",),
    ("D9", "mysql"): (
        "SHOW REPLICA STATUS (SHOW SLAVE STATUS before 8.0.22): see whether the IO or the SQL thread is behind; "
        "replica_parallel_workers can help a single-threaded SQL thread.",),
}

# The collector script that fills a gap, by engine family.
COLLECTOR_SCRIPT = {"postgresql": "postgresql", "mysql": "mysql", "oracle": "oracle", "mssql": "mssql"}
_SETTING_PLACEHOLDER = re.compile(r"\{(now|is):([A-Za-z0-9_]+)\}")


def fill_settings(template, settings):
    def replace(match):
        value = settings.get(match.group(2), "")
        if not value:
            return ""
        return " (now %s)" % value if match.group(1) == "now" else " is %s" % value
    return _SETTING_PLACEHOLDER.sub(replace, template)


def steps_for(code, engine, settings):
    """Filled steps for (code, engine); MariaDB uses the MySQL steps, 'any' covers every engine."""
    templates = (DB_NEXT_STEPS.get((code, engine)) or DB_NEXT_STEPS.get((code, engine_family(engine)))
                 or DB_NEXT_STEPS.get((code, "any")) or ())
    return [fill_settings(template, settings) for template in templates]


def database_next_steps(builder, hypothesis):
    """Next steps of a D hypothesis: the engine's steps, with settings the run recorded."""
    instance = builder.instance_of(hypothesis)
    if instance is None:
        return []
    return steps_for(hypothesis.id, instance.engine, instance.settings)


def engine_next_steps(builder, hypothesis):
    """Engine steps for an H hypothesis, and the collector hint for a database seen only as processes.

    The engines are those of the linked instances, plus PostgreSQL whenever the bundle holds a PostgreSQL
    run (the checkpoint line the H1 and H7 steps have always carried in that case).
    """
    engines = []
    if any(run.tag == "postgresql" for run in builder.host.runs):
        engines.append("postgresql")
    process_only = []
    for entry in hypothesis.database_context:
        if entry["engine"] not in engines:
            engines.append(entry["engine"])
        if entry["source"] == "process" and engine_family(entry["engine"]) not in process_only:
            process_only.append(engine_family(entry["engine"]))
    steps = []
    for engine in engines:
        settings = {}
        for instance in builder.host.databases:
            if engine_family(instance.engine) == engine_family(engine):
                settings = instance.settings
                break
        for text in steps_for(hypothesis.id, engine, settings):
            if text not in steps:
                steps.append(text)
    for family in process_only:
        steps.append(COLLECTOR_HINT % COLLECTOR_SCRIPT.get(family, family))
    return steps
