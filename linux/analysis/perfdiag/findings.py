"""Finding text (section 9.2), findings.json (9.4) and the plain-text `analyse` output.

Text is template-generated and literal: no adjectives beyond the severity word,
and indicators rather than causes ("latency averaged 18 ms against a 10 ms
threshold", never "the disk is too slow").
"""

import json
import math

from .timeutil import clock, duration_text, range_text, stamp_minutes, zone_label, zone_short

SCHEMA = 1


# ---------------------------------------------------------------------------
# Number and time formatting
# ---------------------------------------------------------------------------
def format_number(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    magnitude = abs(value)
    if magnitude >= 1000 and float(value).is_integer():
        return "{:,}".format(int(value)).replace(",", " ")
    if magnitude >= 100:
        return "%.0f" % value
    if magnitude >= 10:
        return "%.1f" % value
    if magnitude >= 1:
        return "%.2f" % value
    if magnitude == 0:
        return "0"
    return "%.3g" % value


def format_threshold_number(value):
    """Thresholds read better without trailing zeros: 10 ms, 0.8, 2.5."""
    if value == int(value):
        return "%d" % value
    return "%g" % value


def format_value(value, unit, trim=False):
    """Value with its unit; sysstat's kB/s (1 kB = 1024 bytes) is shown as MiB/s."""
    number = format_threshold_number if trim else format_number
    if unit == "kB/s":
        mebibytes = value / 1024.0
        if mebibytes >= 1024:
            return "%s GiB/s" % number(mebibytes / 1024.0)
        return "%s MiB/s" % number(mebibytes)
    if unit in ("%", "% idle", "% of RAM", "% of RAM + swap"):
        return "%s %%" % number(value)
    return ("%s %s" % (number(value), unit)).strip()


def threshold_text(threshold):
    if not threshold or "warn" not in threshold:
        return ""
    return format_value(threshold["warn"], threshold.get("unit", ""), trim=True)


def window_text(result, start, end):
    """'10:12 and 10:41 BST (29 min)'. Dates are added when the data spans days."""
    host = result.host
    zone = host.zone
    first, last = range_text(zone, start, end, host.start, host.end)
    zone_first, zone_last = zone_short(zone, start), zone_short(zone, end)
    if zone_first == zone_last:
        return "%s and %s %s (%s)" % (first, last, zone_last, duration_text(end - start))
    return "%s %s and %s %s (%s)" % (first, zone_first, last, zone_last, duration_text(end - start))


def source_text(threshold):
    source = (threshold or {}).get("source", "")
    if source.startswith("default:"):
        return " for %s devices" % source.split(":", 1)[1]
    if source.startswith("limit:"):
        return " (80 %% of the declared limit, %s)" % source.split(":", 1)[1]
    if source == "relative to run median":
        return " (a multiple of the run median)"
    return ""


def worst_burst_text(result, episode):
    """'Worst burst: 2026-10-17 18:02 to 18:03 (70 s), lowest 1.02 %, 4 of 7 samples below the threshold.'"""
    host = result.host
    first, last = range_text(host.zone, episode.start, episode.end, host.start, host.end)
    direction = (episode.threshold or {}).get("direction", "above")
    return "Worst burst: %s to %s (%s), %s %s, %d of %d samples %s the threshold." % (
        first, last, duration_text(episode.duration_s), "lowest" if direction == "below" else "peak",
        format_value(episode.peak, episode_unit(result, episode)), episode.exceeding_samples, episode.samples,
        direction)


def episode_unit(result, episode):
    """Unit of an episode's values: its indicator's, or the plateau's own."""
    indicator = result.indicator(episode.rule, episode.device)
    if indicator is not None:
        return indicator.unit
    return (episode.threshold or {}).get("unit", "")


def describe_episode(result, episode):
    """One literal sentence about an episode."""
    indicator = result.indicator(episode.rule, episode.device)
    title = indicator.title if indicator else ("%s %s" % (episode.rule, episode.device)).strip()
    unit = episode_unit(result, episode)
    threshold = episode.threshold or {}
    direction = threshold.get("direction", "above")
    if episode.rule == "disk_plateau":
        return "%s: %s" % (episode.device, episode.notes[0] if episode.notes else "flat throughput with raised latency")
    if direction == "below":
        return "%s averaged %s (lowest %s) against a warning threshold of %s" % (
            title, format_value(episode.mean, unit), format_value(episode.peak, unit),
            format_value(threshold.get("warn", 0), unit, trim=True))
    return "%s averaged %s (p95 %s, peak %s) against a %s warning threshold%s" % (
        title, format_value(episode.mean, unit), format_value(episode.p95, unit), format_value(episode.peak, unit),
        threshold_text(threshold), source_text(threshold))


def short_evidence(result, episode):
    """'IO pressure (some) averaged 22 % (10:12-10:41)'."""
    indicator = result.indicator(episode.rule, episode.device)
    title = indicator.title if indicator else episode.rule
    unit = episode_unit(result, episode)
    host = result.host
    first, last = range_text(host.zone, episode.start, episode.end, host.start, host.end)
    return "%s averaged %s (%s to %s)" % (title, format_value(episode.mean, unit), first, last)


# ---------------------------------------------------------------------------
# Hypothesis text
# ---------------------------------------------------------------------------
NOT_SEEN_CHECKS = {
    "storage": (("cpu_busy", "runq"), "no CPU saturation"),
    "cpu": (("psi_io_some", "disk_await", "disk_r_await", "disk_w_await"), "no IO pressure or disk latency episode"),
}
STORAGE_CODES = ("H1", "H2", "H7", "D1", "D2", "D6")
CPU_CODES = ("H3", "H5", "H9", "D5")


def database_instance(result, hypothesis):
    """The DatabaseInstance a D hypothesis is about, else None."""
    if not hypothesis.id.startswith("D"):
        return None
    for instance in result.host.databases:
        if instance.short == hypothesis.subject:
            return instance
    return None


def not_seen_lines(result, hypothesis):
    primary = hypothesis.primary
    if primary is None:
        return []
    group = "storage" if hypothesis.id in STORAGE_CODES else "cpu" if hypothesis.id in CPU_CODES else None
    if group is None:
        return []
    # The OS data of a remote database's collecting machine is not evidence about that database.
    if hypothesis.id.startswith("D"):
        instance = database_instance(result, hypothesis)
        if instance is None or instance.local is not True:
            return []
    rules, label = NOT_SEEN_CHECKS[group]
    measured = [rule for rule in rules if any(key[0] == rule for key in result.indicators)]
    if not measured:
        return []
    interval = result.interval_s or 1
    for episode in result.episodes:
        if episode.rule in rules and episode.start - interval < primary.end and episode.end + interval > primary.start:
            return []
    return ["%s in this window." % label]


def process_lines(hypothesis):
    parts = []
    for entry in hypothesis.top_processes:
        many = entry.get("processes", 1) > 1
        name = entry["command"] if many else (entry.get("example") or entry["command"])
        detail = []
        if "io_kBps" in entry:
            if entry["wr_kBps"] >= entry["rd_kBps"]:
                text = "%s %s writes" % (name, format_value(entry["wr_kBps"], "kB/s"))
            else:
                text = "%s %s reads" % (name, format_value(entry["rd_kBps"], "kB/s"))
            if entry.get("share_pct") is not None:
                detail.append("%.0f %% %s" % (entry["share_pct"], entry.get("share_basis", "")))
            largest = format_value(entry.get("example_value", 0.0), "kB/s")
        elif entry.get("metric") == "%CPU":
            text = "%s %.0f %% of one CPU" % (name, entry["cpu_pct"])
            largest = "%.0f %%" % entry.get("example_value", 0.0)
        else:
            text = "%s RSS grew %s MiB, %.0f major faults/s" % (
                name, format_number(entry["rss_growth_kB"] / 1024.0), entry["majflt_ps"])
            largest = "%s MiB" % format_number(entry.get("example_value", 0.0) / 1024.0)
        if many:
            detail.append("%d processes, largest %s %s" % (entry["processes"], entry.get("example", ""), largest))
        if detail:
            text += " (" + "; ".join(detail) + ")"
        parts.append(text)
    return parts


PLURAL_ROLES = {"client backend": "client backends", "foreground": "foreground processes",
                "DB writer": "DB writers"}


def role_text(role, by):
    """'checkpointer 60 MiB/s writes' or 'client backends 35 % of one CPU' for one entry of a role breakdown."""
    name = PLURAL_ROLES.get(role["role"], role["role"]) if role["processes"] > 1 else role["role"]
    if by == "cpu":
        return "%s %.0f %% of one CPU" % (name, role["cpu_pct"])
    if role["wr_kBps"] >= role["rd_kBps"]:
        return "%s %s writes" % (name, format_value(role["wr_kBps"], "kB/s"))
    return "%s %s reads" % (name, format_value(role["rd_kBps"], "kB/s"))


def database_line(entry):
    """One line for a database_context entry: what the database was doing while the host hypothesis held."""
    parts = []
    for key, word in (("statements_ps", "statements/s"), ("transactions_ps", "transactions/s")):
        if entry.get(key) is not None:
            parts.append("%s %s" % (format_number(entry[key]), word))
    if entry.get("waits"):
        parts.append("waits " + ", ".join("%s %s" % (item["category"], format_number(item["sessions"]))
                                           for item in entry["waits"]) + " sessions")
    for key, word in (("read_latency_ms", "read latency"), ("commit_latency_ms", "commit latency")):
        if entry.get(key) is not None:
            parts.append("%s %s" % (word, format_value(entry[key], "ms")))
    if entry.get("temp_kBps") is not None and entry["temp_kBps"] > 0:
        parts.append("temporary files %s" % format_value(entry["temp_kBps"], "kB/s"))
    for key, word in (("write_share_pct", "writes"), ("read_share_pct", "reads")):
        if entry.get(key) is not None:
            parts.append("%.0f %% of the device's %s" % (entry[key], word))
    if entry.get("roles"):
        parts.append(", ".join(role_text(role, entry.get("roles_by", "io")) for role in entry["roles"]))
    return "Database %s, %s: %s." % (entry["instance"], entry["label"], "; ".join(parts) or "no activity figures")


def link_text(links):
    return "See also: " + ", ".join("%s %s" % (code, subject) for code, subject in links) + "."


def hypothesis_sections(result, hypothesis):
    """The pieces of a finding: used by the text, Markdown and HTML renderers.

    "database" has one line per database_context entry of an H hypothesis, "links" the See-also line,
    "notes" the hypothesis's own notes (a remote database, the above-the-block-layer hint).
    """
    zone = result.host.zone
    primary = hypothesis.primary
    sections = {
        "headline": "%s %s — %s — confidence %s" % (
            hypothesis.id, hypothesis.title, subject_label(result, hypothesis), hypothesis.confidence),
        "summary": [], "related": [], "supporting": [], "against": [], "not_seen": [], "kernel": [],
        "processes": [], "database": [], "links": [], "notes": [],
        "kind": "kernel-only" if primary is None else ("recurring" if hypothesis.recurring else "sustained"),
    }
    if primary is None:
        sections["summary"].append(kernel_summary(result, hypothesis))
    elif hypothesis.recurring:
        aggregate = result.repeat_summaries.get((primary.rule, primary.device))
        if aggregate:
            sections["summary"].append(aggregate + ".")
        sections["summary"].append(worst_burst_text(result, primary))
    else:
        sections["summary"].append("Between %s, %s." % (window_text(result, primary.start, primary.end),
                                                        describe_episode(result, primary)))
    sections["related"] = related_lines(result, hypothesis)
    if hypothesis.other_primary:
        sections["summary"].append("%d further %s window%s on the same indicator." % (
            len(hypothesis.other_primary), "recurring" if hypothesis.recurring else "sustained",
            "" if len(hypothesis.other_primary) == 1 else "s"))
    if primary is not None and primary.interval_limited:
        sections["summary"].append("Interval-limited: the sample interval is too coarse to separate sustained from transient behaviour.")
    for episode in hypothesis.supporting:
        sections["supporting"].append(short_evidence(result, episode))
    sections["supporting"].extend(hypothesis.supporting_facts)
    for episode in hypothesis.contradicting:
        sections["against"].append(short_evidence(result, episode))
    sections["against"].extend(hypothesis.contradicting_facts)
    sections["not_seen"] = not_seen_lines(result, hypothesis)
    for event in hypothesis.kernel_events[:5]:
        sections["kernel"].append("%s %s%s%s: %s" % (
            clock(event.time, zone), event.category, (" on " + event.device) if event.device else "",
            (" (x%d)" % event.count) if event.count > 1 else "", event.text[:140]))
    # Process lists belong to the H codes; a D hypothesis shows its instance's own figures instead.
    if hypothesis.id.startswith("D"):
        pass
    elif hypothesis.top_processes:
        label = {"H1": "Top IO", "H2": "Top IO", "H7": "Top IO", "H8": "Top IO", "H6": "Top memory growth"}.get(
            hypothesis.id, "Top CPU")
        sections["processes"] = [label + ": " + ", ".join(process_lines(hypothesis)) + "."]
    elif primary is not None and hypothesis.id not in ("H4",) and not result.host.process_rows:
        sections["processes"] = ["No process data for this window."]
    sections["database"] = [database_line(entry) for entry in hypothesis.database_context]
    if hypothesis.links:
        sections["links"] = [link_text(hypothesis.links)]
    sections["notes"] = list(hypothesis.notes)
    return sections


KERNEL_WHAT = {"H11": "storage error or timeout message", "H12": "cgroup OOM kill",
               "H6": "OOM kill", "H8": "hung-task message"}
RELATED_WORD = {"disk_await": "await", "disk_r_await": "read await", "disk_w_await": "write await"}


def kernel_summary(result, hypothesis):
    events = hypothesis.kernel_events
    zone = result.host.zone
    first, last = min(e.time for e in events), max(e.time for e in events)
    total = sum(e.count for e in events)
    what = KERNEL_WHAT.get(hypothesis.id, "event")
    if total != 1:
        what += "s"
    if hypothesis.id == "H11":
        where = "" if hypothesis.subject == "unattributed device" else " on %s" % hypothesis.subject
        what += where
    when = ("at %s %s" % (clock(first, zone), zone_short(zone, first)) if clock(first, zone) == clock(last, zone)
            else "between %s" % window_text(result, first, last))
    return "The kernel log recorded %d %s %s. No indicator episode is part of this finding." % (total, what, when)


def related_lines(result, hypothesis):
    if not hypothesis.related:
        return []
    from .hypotheses import Builder
    builder = Builder(result)
    parts = []
    for device, episode in hypothesis.related:
        label = builder.label_for(device)
        disk = result.host.hardware.find_disk(device) if result.host.hardware is not None else None
        if label == device and disk is not None and disk.kname != device:
            label = "%s (%s)" % (device, disk.kname)
        parts.append("%s, %s averaged %s" % (label, RELATED_WORD.get(episode.rule, episode.rule),
                                             format_value(episode.mean, "ms")))
    return ["Also seen on: " + "; ".join(parts) + " over the same window."]


def subject_label(result, hypothesis):
    subject = hypothesis.subject
    instance = database_instance(result, hypothesis)
    if instance is not None:
        return instance.label
    if hypothesis.id in ("H1", "H2", "H7", "H10", "H11") and result.host.hardware is not None:
        if hypothesis.subject == "unattributed device":
            return hypothesis.subject
        from .hypotheses import Builder
        return Builder(result).label_for(subject)
    return subject


def hypothesis_text(result, hypothesis, indent=""):
    sections = hypothesis_sections(result, hypothesis)
    lines = [sections["headline"], ""]
    lines.extend(sections["summary"])
    lines.extend(sections["related"])
    for note in sections["notes"]:
        lines.append("Note: " + note + ".")
    if sections["supporting"]:
        lines.append("Supporting: " + "; ".join(sections["supporting"]) + ".")
    if sections["against"]:
        lines.append("Against: " + "; ".join(sections["against"]) + ".")
    lines.extend(sections["processes"])
    lines.extend(sections["database"])
    lines.extend(sections["links"])
    for line in sections["not_seen"]:
        lines.append("Not seen: " + line)
    if sections["kernel"]:
        lines.append("Kernel log:")
        lines.extend("  " + line for line in sections["kernel"])
    lines.append("")
    lines.append("Next steps")
    lines.extend("- " + step for step in hypothesis.next_steps)
    return "\n".join(indent + line if line else line for line in lines)


# ---------------------------------------------------------------------------
# findings.json
# ---------------------------------------------------------------------------
def _round(value, digits=3):
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, digits)
    return value


def episode_to_json(episode):
    data = {
        "id": episode.id, "rule": episode.rule, "device": episode.device, "kind": episode.kind,
        "severity": episode.severity, "start": episode.start, "end": episode.end,
        "duration_s": episode.duration_s, "samples": episode.samples,
        "exceeding_samples": episode.exceeding_samples, "mean": _round(episode.mean),
        "p95": _round(episode.p95), "peak": _round(episode.peak), "score": _round(episode.score, 2),
    }
    if episode.threshold:
        data["threshold"] = {key: _round(value) for key, value in episode.threshold.items()}
    if episode.kind == "transient" and episode.impact:
        data["impact"] = episode.impact
        if episode.evidence:
            data["evidence"] = list(episode.evidence)
    if episode.role != "primary":
        data["role"] = episode.role
    if episode.interval_limited:
        data["interval_limited"] = True
    if episode.notes:
        data["notes"] = list(episode.notes)
    return data


def to_json(result):
    host = result.host
    referenced = {}
    for episode in result.episodes:
        if episode.kind == "sustained" or episode.impact == "impactful":
            referenced[episode.id] = episode
    for hypothesis in result.hypotheses:
        for episode in ([hypothesis.primary] if hypothesis.primary else []) + hypothesis.supporting + \
                hypothesis.contradicting + hypothesis.other_primary + \
                [related for _, related in hypothesis.related]:
            referenced[episode.id] = episode
    ordered = sorted(referenced.values(), key=lambda e: int(e.id[1:]))
    settings = result.settings
    document = {
        "schema": SCHEMA,
        "host": host.host,
        "window": {"start": host.start, "end": host.end, "interval_s": result.interval_s,
                   "timezone": zone_label(host.zone, host.start)},
        "settings": {
            "sustain_s": result.sustain_s,
            "gap_samples": result.gap_samples,
            "min_samples": result.min_samples,
            "interval_limited": result.interval_limited,
            "non_default": {key: {"value": value, "source": source} for key, (value, source) in
                            sorted(settings.changed.items())},
        },
        "episodes": [episode_to_json(episode) for episode in ordered],
        "transients": {"absorbed": dict(sorted(result.absorbed.items())),
                       "impactful": [episode.id for episode in result.impactful],
                       "summaries": list(result.transient_summaries)},
        "hypotheses": [],
        "time_above": result.time_above,
        "notes": list(result.notes) + list(host.notes),
    }
    for hypothesis in result.hypotheses:
        document["hypotheses"].append({
            "id": hypothesis.id, "title": hypothesis.title, "subject": hypothesis.subject,
            "confidence": hypothesis.confidence, "origin": hypothesis.origin,
            "primary": hypothesis.primary.id if hypothesis.primary else None,
            "supporting": [e.id for e in hypothesis.supporting],
            "contradicting": [e.id for e in hypothesis.contradicting],
            "other_primary": [e.id for e in hypothesis.other_primary],
            "related": [{"device": device, "episode": episode.id} for device, episode in hypothesis.related],
            "recurring": hypothesis.recurring,
            "supporting_facts": list(hypothesis.supporting_facts),
            "contradicting_facts": list(hypothesis.contradicting_facts),
            "kernel_events": [{"time": e.time, "category": e.category, "device": e.device, "text": e.text,
                               "count": e.count}
                              for e in hypothesis.kernel_events],
            "top_processes": [{key: _round(value) for key, value in entry.items()} for entry in hypothesis.top_processes],
            "next_steps": list(hypothesis.next_steps),
        })
    return document


def write_json(result, path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(to_json(result), handle, indent=2, sort_keys=False)
        handle.write("\n")


# ---------------------------------------------------------------------------
# Plain text for `perfdiag analyse`
# ---------------------------------------------------------------------------
def measured_hypotheses(result):
    return [h for h in result.hypotheses if h.origin == "measured"]


def kernel_hypotheses(result):
    """Hypotheses raised by the kernel log alone; listed apart from the ranked ones."""
    return [h for h in result.hypotheses if h.origin == "kernel"]


def further_hypotheses_text(count, json_path=None):
    if json_path:
        return "%d further hypotheses are in %s." % (count, json_path)
    return "%d further hypotheses not shown: use --json FILE or the report." % count


DEVICE_ROLE_WORDS = (("data", "data"), ("wal", "WAL"), ("redo", "redo"), ("log", "log"), ("asm_disk", "ASM disks"))


def device_phrase(names):
    """'vg-data (sdb)' for a device and what it sits on, 'nvme1n1' for a plain device."""
    return names[0] if len(names) == 1 else "%s (%s)" % (names[0], ", ".join(names[1:]))


def database_header(result, instance):
    """The 'Database: ...' line of one instance (spec 12.1).

    'PostgreSQL 16.4 (pg:5432), collector, local, data on nvme1n1, WAL on nvme1n1' for a collector run,
    'mysqld (process data only), seen in 96 % of pidstat samples, data on sdb' for processes alone.
    """
    from .dbdetect import process_seen_share
    from .dbload import label_for

    if instance.source == "process":
        text = "%s (process data only)" % process_program_name(instance)
        share = process_seen_share(result.host, instance.process_keys)
        if share is not None:
            text += ", seen in %.0f %% of pidstat samples" % (100.0 * share)
    else:
        locality = {True: "local", False: "remote", None: "locality unknown"}[instance.local]
        text = "%s (%s), %s, %s" % (label_for(instance.engine, instance.version, ""), instance.short,
                                    instance.source, locality)
    for role, word in DEVICE_ROLE_WORDS:
        if instance.devices.get(role):
            text += ", %s on %s" % (word, device_phrase(instance.devices[role]))
    return "Database: " + text


def process_program_name(instance):
    """'mysqld' for a MySQL server seen in pidstat; the label's own name when the engine has no single program."""
    if instance.engine != "oracle" and instance.process_keys:
        for preferred in ("postgres", "mysqld", "mariadbd", "sqlservr"):
            if preferred in instance.process_keys:
                return preferred
        return instance.process_keys[0]
    return instance.label.replace(" (process data only)", "")


def column_widths(result, episodes):
    """(indicator width, device width) of the episode tables: 16 and 10, wider for database names."""
    if not result.host.databases:
        return 16, 10
    return (max([16] + [len(episode.rule) for episode in episodes]),
            max([10] + [len(episode.device or "-") for episode in episodes]))


def render_text(result, max_hypotheses=5, json_path=None):
    host = result.host
    zone = host.zone
    lines = []
    lines.append("Host %s, %s" % (host.host, host.distro or "distro unknown"))
    if host.start is not None:
        lines.append("Window: %s to %s %s, sample interval %d s, sustain window %d s%s" % (
            stamp_minutes(host.start, zone), stamp_minutes(host.end, zone), zone_label(zone, host.start), result.interval_s,
            result.sustain_s, " (interval-limited)" if result.interval_limited else ""))
    for instance in host.databases:
        lines.append(database_header(result, instance))
    for note in result.notes + host.notes:
        lines.append("NOTE: " + note)
    lines.append("")

    measured = measured_hypotheses(result)
    shown = measured[:max_hypotheses]
    if shown:
        lines.append("Hypotheses, ranked by primary-episode score")
        lines.append("=" * 44)
        for hypothesis in shown:
            lines.append("")
            lines.append(hypothesis_text(result, hypothesis))
        if len(measured) > max_hypotheses:
            lines.append("")
            lines.append(further_hypotheses_text(len(measured) - max_hypotheses, json_path))
    else:
        lines.append("No hypotheses raised: no sustained episode on a primary indicator.")
    from_kernel = kernel_hypotheses(result)
    if from_kernel:
        lines.append("")
        lines.append("Kernel log findings")
        lines.append("=" * 19)
        for hypothesis in from_kernel:
            lines.append("")
            lines.append(hypothesis_text(result, hypothesis))
    lines.append("")

    sustained = [e for e in result.episodes if e.kind == "sustained"]
    lines.append("Sustained episodes, ranked by score (how far over, for how long)")
    lines.append("=" * 64)
    if sustained:
        # Database names are longer than the OS ones; the columns widen only when there is a database.
        rule_width, device_width = column_widths(result, sustained)
        lines.append("%-4s %-*s %-*s %-5s %-17s %8s %10s %10s %9s" % (
            "id", rule_width, "indicator", device_width, "device", "sev", "from", "duration", "mean", "peak", "score"))
        for episode in sustained:
            indicator = result.indicator(episode.rule, episode.device)
            unit = episode_unit(result, episode)
            supporting = (indicator is not None and indicator.role != "primary") or episode.role != "primary"
            lines.append("%-4s %-*s %-*s %-5s %-17s %8s %10s %10s %9.1f%s" % (
                episode.id, rule_width, episode.rule, device_width, episode.device or "-", episode.severity,
                stamp_minutes(episode.start, zone), duration_text(episode.duration_s),
                format_value(episode.mean, unit), format_value(episode.peak, unit), episode.score,
                "  [supporting]" if supporting else ""))
    else:
        lines.append("none")
    lines.append("")

    lines.append("Transient events")
    lines.append("=" * 16)
    absorbed_total = sum(result.absorbed.values())
    if result.impactful:
        rule_width, device_width = column_widths(result, result.impactful)
        for episode in result.impactful:
            lines.append("%-4s %-*s %-*s %s for %s: %s" % (
                episode.id, rule_width, episode.rule, device_width, episode.device or "-", stamp_minutes(episode.start, zone),
                duration_text(episode.duration_s), "; ".join(episode.evidence) or "impact indicator"))
        if result.omitted_transients:
            lines.append("... and %d more impactful transients (see findings.json)." % result.omitted_transients)
    else:
        lines.append("No impactful transients.")
    for summary in result.transient_summaries:
        lines.append("Repeated: " + summary + ".")
    if absorbed_total:
        detail = ", ".join("%s %d" % (name, count) for name, count in sorted(result.absorbed.items()))
        lines.append("Absorbed (no paired latency, queue or pressure indicator breached): %d (%s)." % (absorbed_total, detail))
    return "\n".join(lines) + "\n"
