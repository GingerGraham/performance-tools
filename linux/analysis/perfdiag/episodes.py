"""Episode detection and scoring (spec section 6.3).

Findings are ranked by threshold-normalised exceedance integral: how far past
the warn threshold, in units of the warn-to-crit band (capped), for how long.
With crit at 2x warn, a 5-second burst to 2x scores 5; 45 seconds at 1.5x scores
22.5.
"""

import math
import statistics
from dataclasses import dataclass

from .model import Episode


@dataclass
class Rule:
    name: str
    column: str
    warn: float
    crit: float
    direction: str = "above"      # "above" or "below"


def exceeds(value, threshold, direction):
    if math.isnan(value):
        return False
    if direction == "above":
        return value > threshold
    return value < threshold


DEFAULT_EXCEEDANCE_CAP = 3.0


def exceedance(value, warn, crit, direction, cap=DEFAULT_EXCEEDANCE_CAP):
    """Distance past warn in units of the warn-to-crit band, capped. 0 when not exceeding.

    1.0 means as far past warn as crit is. Using the band, not warn, puts
    "below" rules (idle, available memory) on the same scale as "above" rules.
    """
    if math.isnan(value):
        return 0.0
    band = abs(crit - warn)
    if band == 0:
        band = abs(warn)
    if band == 0:
        return 0.0
    if direction == "above":
        distance = value - warn
    else:
        distance = warn - value
    if distance <= 0:
        return 0.0
    return min(cap, distance / band)


def percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def default_gap_samples(sustain_s, interval_s):
    """max(1, min(5, round(0.2 x sustain_s / interval_s)))"""
    return max(1, min(5, int(round(0.2 * sustain_s / float(interval_s)))))


def detect_episodes(points, rule, device, interval_s, sustain_s, gap_samples, min_samples=3, crit_values=None,
                    exceedance_cap=DEFAULT_EXCEEDANCE_CAP):
    """points: [(epoch_interval_end, value), ...] sorted by time.

    A missing-data gap longer than 2 x interval closes the current run: episodes
    never span a hole in the data.

    crit_values, if given, is a list parallel to `points` used instead of the
    value when judging severity (the HDD queue rule judges crit against the
    latency crit as well as the queue depth).
    """
    episodes = []
    run_indexes = []
    gap = 0
    previous_time = None

    def close_run():
        if not run_indexes:
            return
        first, last = run_indexes[0], run_indexes[-1]
        span = points[first:last + 1]
        values = [value for _, value in span]
        hits = [value for value in values if exceeds(value, rule.warn, rule.direction)]
        duration = (span[-1][0] - span[0][0]) + interval_s
        if duration >= sustain_s and len(values) >= min_samples:
            kind = "sustained"
        else:
            kind = "transient"
        if crit_values is not None:
            severity_values = crit_values[first:last + 1]
        else:
            severity_values = values
        finite = [value for value in severity_values if not math.isnan(value)]
        if finite and exceeds(statistics.median(finite), rule.crit, rule.direction):
            severity = "crit"
        else:
            severity = "warn"
        finite_values = [value for value in values if not math.isnan(value)]
        if rule.direction == "above":
            peak = max(finite_values)
            p95 = percentile(finite_values, 95)
        else:
            peak = min(finite_values)
            p95 = percentile(finite_values, 5)
        score = sum(exceedance(value, rule.warn, rule.crit, rule.direction, exceedance_cap)
                    for value in values) * interval_s
        episodes.append(Episode(
            rule=rule.name, device=device, kind=kind, severity=severity,
            start=span[0][0] - interval_s, end=span[-1][0], duration_s=int(duration),
            samples=len(values), exceeding_samples=len(hits),
            mean=statistics.fmean(finite_values), p95=p95, peak=peak, score=score,
        ))

    for index, (time_value, value) in enumerate(points):
        if run_indexes and previous_time is not None and time_value - previous_time > 2 * interval_s:
            close_run()
            run_indexes.clear()
            gap = 0
        previous_time = time_value
        if exceeds(value, rule.warn, rule.direction):
            run_indexes.append(index)
            gap = 0
        elif run_indexes:
            gap = gap + 1
            if gap > gap_samples:
                close_run()
                run_indexes.clear()
                gap = 0
    close_run()
    return episodes


def time_above(values, warn, crit, direction):
    """Share of samples past warn and crit, plus distribution figures (section 9.4)."""
    finite = [value for value in values if not math.isnan(value)]
    if not finite:
        return None
    count = float(len(finite))
    warn_hits = sum(1 for value in finite if exceeds(value, warn, direction))
    crit_hits = sum(1 for value in finite if exceeds(value, crit, direction))
    result = {
        "warn_pct": round(100.0 * warn_hits / count, 1),
        "crit_pct": round(100.0 * crit_hits / count, 1),
        "p50": round(percentile(finite, 50), 3),
        "direction": direction,
    }
    if direction == "above":
        result["p95"] = round(percentile(finite, 95), 3)
        result["max"] = round(max(finite), 3)
    else:
        result["p5"] = round(percentile(finite, 5), 3)
        result["min"] = round(min(finite), 3)
    return result
