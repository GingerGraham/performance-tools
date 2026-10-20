"""Finding text (section 9.2), findings.json (9.4) and the plain-text `analyse` output.

Text is template-generated and literal: no adjectives beyond the severity word,
and indicators rather than causes ("latency averaged 18 ms against a 10 ms
threshold", never "the disk is too slow").
"""

import json
import math

from .timeutil import clock, duration_text, stamp_minutes, zone_label, zone_short

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
    """Value with its unit; kB/s is shown as MB/s."""
    number = format_threshold_number if trim else format_number
    if unit == "kB/s":
        megabytes = value / 1000.0
        if megabytes >= 1000:
            return "%s GB/s" % number(megabytes / 1000.0)
        return "%s MB/s" % number(megabytes)
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
    multi_day = host.start is not None and (host.end - host.start) > 20 * 3600
    if multi_day or clock(start, zone) > clock(end, zone):
        first, last = stamp_minutes(start, zone), stamp_minutes(end, zone)
    else:
        first, last = clock(start, zone), clock(end, zone)
    return "%s and %s %s (%s)" % (first, last, zone_short(zone), duration_text(end - start))


def source_text(threshold):
    source = (threshold or {}).get("source", "")
    if source.startswith("default:"):
        return " for %s devices" % source.split(":", 1)[1]
    if source.startswith("limit:"):
        return " (80 %% of the declared limit, %s)" % source.split(":", 1)[1]
    if source == "relative to run median":
        return " (a multiple of the run median)"
    return ""


def describe_episode(result, episode):
    """One literal sentence about an episode."""
    indicator = result.indicator(episode.rule, episode.device)
    title = indicator.title if indicator else ("%s %s" % (episode.rule, episode.device)).strip()
    unit = indicator.unit if indicator else ""
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
    unit = indicator.unit if indicator else ""
    zone = result.host.zone
    return "%s averaged %s (%s to %s)" % (
        title, format_value(episode.mean, unit), clock(episode.start, zone), clock(episode.end, zone))


# ---------------------------------------------------------------------------
# Hypothesis text
# ---------------------------------------------------------------------------
NOT_SEEN_CHECKS = {
    "storage": (("cpu_busy", "runq"), "no CPU saturation"),
    "cpu": (("psi_io_some", "disk_await", "disk_r_await", "disk_w_await"), "no IO pressure or disk latency episode"),
}
STORAGE_CODES = ("H1", "H2", "H7")
CPU_CODES = ("H3", "H5", "H9")


def not_seen_lines(result, hypothesis):
    primary = hypothesis.primary
    if primary is None:
        return []
    group = "storage" if hypothesis.id in STORAGE_CODES else "cpu" if hypothesis.id in CPU_CODES else None
    if group is None:
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
        if "io_kBps" in entry:
            if entry["wr_kBps"] >= entry["rd_kBps"]:
                text = "%s %s writes" % (entry["command"], format_value(entry["wr_kBps"], "kB/s"))
            else:
                text = "%s %s reads" % (entry["command"], format_value(entry["rd_kBps"], "kB/s"))
            if entry.get("share_pct") is not None:
                text += " (%.0f %% %s)" % (entry["share_pct"], entry.get("share_basis", ""))
        elif entry.get("metric") == "%CPU":
            text = "%s %.0f %% of one CPU" % (entry["command"], entry["cpu_pct"])
        else:
            text = "%s RSS grew %s MB, %.0f major faults/s" % (
                entry["command"], format_number(entry["rss_growth_kB"] / 1000.0), entry["majflt_ps"])
        parts.append(text)
    return parts


def hypothesis_sections(result, hypothesis):
    """The pieces of a finding: used by the text, Markdown and HTML renderers."""
    zone = result.host.zone
    primary = hypothesis.primary
    sections = {
        "headline": "%s %s — %s — confidence %s" % (
            hypothesis.id, hypothesis.title, subject_label(result, hypothesis), hypothesis.confidence),
        "summary": [], "related": [], "supporting": [], "against": [], "not_seen": [], "kernel": [],
        "processes": [],
        "kind": "kernel-only" if primary is None else ("recurring" if hypothesis.recurring else "sustained"),
    }
    if primary is None:
        sections["summary"].append(kernel_summary(result, hypothesis))
    elif hypothesis.recurring:
        sections["summary"].append("Between %s, %s" % (window_text(result, primary.start, primary.end),
                                                       describe_episode(result, primary)))
        for line in result.transient_summaries:
            indicator = result.indicator(primary.rule, primary.device)
            if indicator is not None and line.startswith(indicator.title):
                sections["summary"].append(line + ".")
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
    if hypothesis.top_processes:
        label = {"H1": "Top IO", "H2": "Top IO", "H7": "Top IO", "H8": "Top IO", "H6": "Top memory growth"}.get(
            hypothesis.id, "Top CPU")
        sections["processes"] = [label + ": " + ", ".join(process_lines(hypothesis)) + "."]
    elif primary is not None and hypothesis.id not in ("H4",) and not result.host.process_rows:
        sections["processes"] = ["No process data for this window."]
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
    when = ("at %s %s" % (clock(first, zone), zone_short(zone)) if clock(first, zone) == clock(last, zone)
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
    if sections["supporting"]:
        lines.append("Supporting: " + "; ".join(sections["supporting"]) + ".")
    if sections["against"]:
        lines.append("Against: " + "; ".join(sections["against"]) + ".")
    lines.extend(sections["processes"])
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
                   "timezone": zone_label(host.zone)},
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


def render_text(result, max_hypotheses=5, json_path=None):
    host = result.host
    zone = host.zone
    lines = []
    lines.append("Host %s, %s" % (host.host, host.distro or "distro unknown"))
    if host.start is not None:
        lines.append("Window: %s to %s %s, sample interval %d s, sustain window %d s%s" % (
            stamp_minutes(host.start, zone), stamp_minutes(host.end, zone), zone_label(zone), result.interval_s,
            result.sustain_s, " (interval-limited)" if result.interval_limited else ""))
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
        lines.append("%-4s %-16s %-10s %-5s %-17s %8s %10s %10s %9s" % (
            "id", "indicator", "device", "sev", "from", "duration", "mean", "peak", "score"))
        for episode in sustained:
            indicator = result.indicator(episode.rule, episode.device)
            unit = indicator.unit if indicator else ""
            lines.append("%-4s %-16s %-10s %-5s %-17s %8s %10s %10s %9.1f%s" % (
                episode.id, episode.rule, episode.device or "-", episode.severity,
                stamp_minutes(episode.start, zone), duration_text(episode.duration_s),
                format_value(episode.mean, unit), format_value(episode.peak, unit), episode.score,
                "  [supporting]" if indicator is not None and indicator.role != "primary" else ""))
    else:
        lines.append("none")
    lines.append("")

    lines.append("Transient events")
    lines.append("=" * 16)
    absorbed_total = sum(result.absorbed.values())
    if result.impactful:
        for episode in result.impactful:
            lines.append("%-4s %-16s %-10s %s for %s: %s" % (
                episode.id, episode.rule, episode.device or "-", stamp_minutes(episode.start, zone),
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
