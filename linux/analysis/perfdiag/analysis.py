"""Runs the rules over a host: episodes, transient classification, time above threshold."""

from dataclasses import dataclass, field
from typing import Dict, List

from . import episodes as episode_logic
from . import plateau, rules
from .episodes import Rule
from .model import Episode

# Section 6.4: which indicators confirm that a transient mattered.
IMPACT_PAIRS = {
    "disk_throughput": ("disk_await", "disk_r_await", "disk_w_await", "disk_queue"),
    "disk_read_kBps": ("disk_await", "disk_r_await", "disk_queue"),
    "disk_write_kBps": ("disk_await", "disk_w_await", "disk_queue"),
    "disk_iops": ("disk_await", "disk_r_await", "disk_w_await", "disk_queue"),
    "cpu_busy": ("psi_cpu", "load"),
    "runq": ("psi_cpu", "load"),
    "cpu_single": ("psi_cpu", "runq"),
    "cpu_soft_single": ("psi_cpu", "runq"),
    "mem_avail": ("swap_out", "direct_reclaim", "major_faults", "psi_mem_some"),
}
# These are the impact indicators themselves: a transient on one is always listed.
SELF_IMPACT = frozenset((
    "disk_await", "disk_r_await", "disk_w_await", "disk_queue", "stack_overhead",
    "psi_cpu", "psi_io_some", "psi_io_full", "psi_mem_some", "psi_mem_full",
    "swap_out", "direct_reclaim", "major_faults", "cpu_steal",
))
# Disk rules pair with the same device; the others are host-wide.
SAME_DEVICE_PAIRING = frozenset(("disk_throughput", "disk_read_kBps", "disk_write_kBps", "disk_iops"))
# Plateaus are H2 candidates on these classes; elsewhere they are only evidence for H1.
PLATEAU_PRIMARY_CLASSES = ("cloud-ebs", "cloud-azure", "virtual", "san", "unknown")
REPEAT_SUMMARY_MIN = 3
MAX_LISTED_TRANSIENTS = 50


def key_text(rule, device):
    return "%s:%s" % (rule, device) if device else rule


@dataclass
class AnalysisResult:
    host: object
    settings: object
    interval_s: int = 0
    sustain_s: int = 0
    min_samples: int = 3
    interval_limited: bool = False
    indicators: Dict[tuple, object] = field(default_factory=dict)
    episodes: List[Episode] = field(default_factory=list)
    absorbed: Dict[str, int] = field(default_factory=dict)
    impactful: List[Episode] = field(default_factory=list)
    omitted_transients: int = 0
    transient_summaries: List[str] = field(default_factory=list)
    repeat_summaries: Dict[tuple, str] = field(default_factory=dict)      # (rule, device) -> aggregate line
    time_above: Dict[str, dict] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    breaches: Dict[tuple, List[int]] = field(default_factory=dict)
    hypotheses: list = field(default_factory=list)
    gap_samples: int = 0
    profiles: Dict[str, object] = field(default_factory=dict)

    def profile_for(self, device):
        return self.profiles.get(device)

    def indicator(self, rule, device=""):
        return self.indicators.get((rule, device))

    def episodes_for(self, rule, device=None, kind=None):
        found = []
        for episode in self.episodes:
            if episode.rule != rule:
                continue
            if device is not None and episode.device != device:
                continue
            if kind is not None and episode.kind != kind:
                continue
            found.append(episode)
        return found

    def sustained(self):
        return [episode for episode in self.episodes if episode.kind == "sustained"]


def _gap_samples(settings, sustain_s, interval_s):
    text = settings.values["analysis.gap_samples"]
    if text == "auto":
        return episode_logic.default_gap_samples(sustain_s, interval_s)
    return max(0, int(float(text)))


def _host_interval(host):
    if host.interval_s:
        return host.interval_s
    counts = {}
    for series in host.series.values():
        counts[series.interval_s] = counts.get(series.interval_s, 0) + 1
    return max(counts, key=counts.get) if counts else 0


def run_analysis(host, settings):
    result = AnalysisResult(host=host, settings=settings)
    result.interval_s = _host_interval(host)
    result.min_samples = settings.int("analysis.min_samples")
    configured_sustain = settings.int("analysis.sustain_s")
    result.sustain_s = configured_sustain

    if result.interval_s and configured_sustain / float(result.interval_s) < 3:
        result.sustain_s = 3 * result.interval_s
        result.interval_limited = True
        result.notes.append(
            "interval-limited: a %d s sustain window is under 3 samples at the %d s sample interval, so it was "
            "raised to %d s. Faster sampling would separate sustained from transient behaviour." % (
                configured_sustain, result.interval_s, result.sustain_s))
    result.gap_samples = _gap_samples(settings, result.sustain_s, result.interval_s or 1)

    context = rules.Context(host, settings)
    for indicator in rules.build_indicators(context):
        result.indicators[indicator.key] = indicator
    result.notes.extend(context.notes)
    result.profiles = context.profiles

    _detect_all(result)
    _detect_plateaus(result, context)
    _rank_and_number(result)
    _classify_transients(result)
    from . import hypotheses
    hypotheses.build_hypotheses(result)
    return result


def _detect_all(result):
    for key, indicator in result.indicators.items():
        interval = indicator.interval_s or result.interval_s
        if interval <= 0 or len(indicator.times) == 0:
            continue
        sustain = max(result.sustain_s, 3 * interval)
        forced = sustain > result.settings.int("analysis.sustain_s")
        gap = _gap_samples(result.settings, sustain, interval)
        rule = Rule(indicator.name, indicator.metric or indicator.name, indicator.warn, indicator.crit,
                    indicator.direction)
        found = episode_logic.detect_episodes(
            indicator.points(), rule, indicator.device, interval, sustain, gap,
            result.min_samples, indicator.crit_values, result.settings.float("analysis.exceedance_cap"))
        for episode in found:
            episode.threshold = {"warn": indicator.warn, "crit": indicator.crit,
                                 "source": indicator.threshold_source, "direction": indicator.direction,
                                 "unit": indicator.unit}
            episode.interval_limited = forced
            if indicator.labels:
                episode.notes.extend(indicator.labels)
        result.episodes.extend(found)

        result.breaches[key] = [t for t, v in indicator.points()
                                if episode_logic.exceeds(v, indicator.warn, indicator.direction)]
        stats = episode_logic.time_above(indicator.values, indicator.warn, indicator.crit, indicator.direction)
        if stats is not None:
            stats["role"] = indicator.role
            stats["unit"] = indicator.unit
            result.time_above[key_text(indicator.name, indicator.device)] = stats


def _detect_plateaus(result, context):
    host, settings = result.host, result.settings
    cv_max = settings.float("analysis.plateau_cv")
    cap = settings.float("analysis.exceedance_cap")
    cloud_devices = []
    for device in host.devices("disk.read_kBps"):
        profile = context.profile(device)
        if profile.media_class == "stacked":
            continue
        latency = _latency_lookup(host, device)
        if latency is None:
            continue
        role = "primary" if profile.media_class in PLATEAU_PRIMARY_CLASSES else "supporting"
        interval = latency[1]
        sustain = max(result.sustain_s, 3 * interval)
        read = host.get("disk.read_kBps", device)
        write = host.get("disk.write_kBps", device)
        tps = host.get("disk.tps", device)
        if read is not None and write is not None:
            times, values = rules.derive(read, lambda r, w: r + w, write)
            _add_plateau(result, plateau.detect_plateaus(
                times, values, latency[0], profile.warn_ms, profile.crit_ms, interval, sustain, cv_max, device,
                "throughput (kB/s)", cap), role)
        if tps is not None:
            _add_plateau(result, plateau.detect_plateaus(
                list(tps.times), list(tps.values), latency[0], profile.warn_ms, profile.crit_ms, interval, sustain,
                cv_max, device, "IOPS", cap), role)
        if profile.media_class in ("cloud-ebs", "cloud-azure") and read is not None and write is not None:
            cloud_devices.append((device, profile, latency))

    if len(cloud_devices) >= 2:
        _cloud_sum_plateau(result, host, cloud_devices, cv_max, cap)


def _latency_lookup(host, device):
    """({time: ms}, interval) from sar await, or the worse of psar's read and write await."""
    sar = host.get("disk.await_ms", device, ("sar",))
    if sar is not None:
        return dict(zip(sar.times, sar.values)), sar.interval_s
    reads = host.get("disk.r_await_ms", device, ("psar",))
    writes = host.get("disk.w_await_ms", device, ("psar",))
    if reads is not None and writes is not None:
        read_lookup, write_lookup = dict(zip(reads.times, reads.values)), dict(zip(writes.times, writes.values))
        return ({t: max(read_lookup[t], write_lookup.get(t, 0.0)) for t in read_lookup}, reads.interval_s)
    return None


def _cloud_sum_plateau(result, host, cloud_devices, cv_max, cap):
    first_device, _, first_latency = cloud_devices[0]
    interval = first_latency[1]
    sums = {}
    latency = {}
    for device, _, (lookup, _) in cloud_devices:
        read = host.get("disk.read_kBps", device)
        write = host.get("disk.write_kBps", device)
        for time_value, r, w in zip(read.times, read.values, write.values):
            sums[time_value] = sums.get(time_value, 0.0) + r + w
        for time_value, wait in lookup.items():
            latency[time_value] = max(latency.get(time_value, 0.0), wait)
    times = sorted(sums)
    warn = min(profile.warn_ms for _, profile, _ in cloud_devices)
    crit = min(profile.crit_ms for _, profile, _ in cloud_devices)
    sustain = max(result.sustain_s, 3 * interval)
    found = plateau.detect_plateaus(times, [sums[t] for t in times], latency, warn, crit, interval, sustain, cv_max,
                                    "cloud-sum", "throughput (kB/s)", cap)
    for episode in found:
        episode.notes.append("sum across %s" % ", ".join(device for device, _, _ in cloud_devices))
    _add_plateau(result, found)


def _add_plateau(result, found, role="primary"):
    for episode in found:
        episode.role = role
    result.episodes.extend(found)


def _rank_and_number(result):
    result.episodes.sort(key=lambda episode: (-episode.score, episode.start, episode.rule, episode.device))
    for number, episode in enumerate(result.episodes, start=1):
        episode.id = "e%d" % number


# ---------------------------------------------------------------------------
# Transients (section 6.4)
# ---------------------------------------------------------------------------
def _breach_within(result, rule_name, device, start, end, interval):
    """Peak evidence text if any indicator of `rule_name` breached warn in [start-interval, end+interval]."""
    best = None
    for (name, indicator_device), indicator in result.indicators.items():
        if name != rule_name:
            continue
        if device and indicator_device and indicator_device != device and name in SAME_DEVICE_PAIRING_TARGETS:
            continue
        for time_value, value in indicator.points():
            if time_value < start - interval or time_value - interval > end:
                continue
            if not episode_logic.exceeds(value, indicator.warn, indicator.direction):
                continue
            if best is None or abs(value - indicator.warn) > abs(best[0] - indicator.warn):
                best = (value, indicator)
    if best is None:
        return None
    value, indicator = best
    return "%s peaked at %.3g %s against a warn threshold of %.3g" % (indicator.title, value, indicator.unit, indicator.warn)


SAME_DEVICE_PAIRING_TARGETS = frozenset(("disk_await", "disk_r_await", "disk_w_await", "disk_queue"))


def _classify_transients(result):
    absorbed = {}
    impactful = []
    for episode in result.episodes:
        if episode.kind != "transient":
            continue
        indicator = result.indicators.get((episode.rule, episode.device))
        interval = indicator.interval_s if indicator else result.interval_s
        if episode.rule in SELF_IMPACT:
            episode.impact = "impactful"
            episode.evidence = ["%s is an impact indicator" % (indicator.title if indicator else episode.rule)]
            impactful.append(episode)
            continue
        evidence = []
        device = episode.device if episode.rule in SAME_DEVICE_PAIRING else ""
        for paired in IMPACT_PAIRS.get(episode.rule, ()):
            text = _breach_within(result, paired, device, episode.start, episode.end, interval)
            if text:
                evidence.append(text)
        if evidence:
            episode.impact = "impactful"
            episode.evidence = evidence
            impactful.append(episode)
        else:
            episode.impact = "absorbed"
            name = key_text(episode.rule, episode.device)
            absorbed[name] = absorbed.get(name, 0) + 1
    result.absorbed = absorbed
    impactful.sort(key=lambda episode: -episode.score)
    result.omitted_transients = max(0, len(impactful) - MAX_LISTED_TRANSIENTS)
    result.impactful = impactful[:MAX_LISTED_TRANSIENTS]
    result.transient_summaries = _summarise_repeats(result, impactful)


def _summarise_repeats(result, impactful):
    """'CPU idle fell below 20 % in 1514 bursts (total 5 h 2 min, longest 2 min) between A and B UTC'."""
    from .findings import format_value
    from .timeutil import duration_text, range_text, zone_short

    groups = {}
    for episode in impactful:
        groups.setdefault((episode.rule, episode.device), []).append(episode)
    lines = []
    host = result.host
    zone = host.zone
    for (rule, device), members in sorted(groups.items()):
        if len(members) < REPEAT_SUMMARY_MIN:
            continue
        indicator = result.indicators.get((rule, device))
        title = indicator.title if indicator else rule
        threshold = members[0].threshold or {}
        if "warn" in threshold:
            limit = format_value(threshold["warn"], threshold.get("unit", ""), trim=True)
        else:
            limit = "its threshold"
        verb = "fell below" if threshold.get("direction") == "below" else "exceeded"
        first, last = range_text(zone, min(m.start for m in members), max(m.end for m in members),
                                 host.start, host.end)
        line = "%s %s %s in %d bursts (total %s, longest %s) between %s and %s %s" % (
            title, verb, limit, len(members), duration_text(sum(m.duration_s for m in members)),
            duration_text(max(m.duration_s for m in members)), first, last, zone_short(zone))
        lines.append(line)
        result.repeat_summaries[(rule, device)] = line
    return lines
