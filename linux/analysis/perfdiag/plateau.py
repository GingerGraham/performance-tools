"""Plateau detection: "a limit we were not told about" (section 6.5.1).

A window of `sustain_s` is a plateau when all three hold:

1. throughput (or IOPS) is flat: coefficient of variation below plateau_cv;
2. the window mean is at least 90 % of the run's p99 for that metric;
3. latency in the window is at least 2x the device's median latency over the
   whole run, and at least the class warn value.

Condition 3 matters: a steady workload is flat too, and is not a cap.
"""

import math
import statistics

from .episodes import exceedance, percentile
from .model import Episode


def _prefix_sums(values):
    sums, squares = [0.0], [0.0]
    for value in values:
        sums.append(sums[-1] + value)
        squares.append(squares[-1] + value * value)
    return sums, squares


def detect_plateaus(times, values, latency, warn_latency, interval_s, sustain_s, cv_max, device, metric_label):
    """Episodes for flat, latency-inflated stretches.

    times/values: the throughput or IOPS series. latency: {time: ms}.
    """
    count = len(values)
    window = int(math.ceil(sustain_s / float(interval_s)))
    if window < 2 or count < window:
        return []
    if any(math.isnan(value) for value in values):
        values = [0.0 if math.isnan(value) else value for value in values]

    p99 = percentile(values, 99)
    if p99 <= 0:
        return []
    latency_values = [latency[t] for t in times if t in latency]
    if not latency_values:
        return []
    median_latency = statistics.median(latency_values)
    latency_floor = max(2.0 * median_latency, warn_latency)

    sums, squares = _prefix_sums(values)
    latency_series = [latency.get(t, 0.0) for t in times]
    latency_sums, _ = _prefix_sums(latency_series)
    # A window that straddles a hole in the data is not a window.
    hole = [0]
    for index in range(1, count):
        hole.append(hole[-1] + (1 if times[index] - times[index - 1] > 2 * interval_s else 0))

    qualifying = []
    for first in range(0, count - window + 1):
        last = first + window
        if hole[last - 1] - hole[first] > 0:
            continue
        mean = (sums[last] - sums[first]) / window
        if mean <= 0 or mean < 0.9 * p99:
            continue
        variance = max(0.0, (squares[last] - squares[first]) / window - mean * mean)
        if math.sqrt(variance) / mean >= cv_max:
            continue
        mean_latency = (latency_sums[last] - latency_sums[first]) / window
        if mean_latency < latency_floor:
            continue
        qualifying.append((first, last - 1))

    episodes = []
    merged = []
    for first, last in qualifying:
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    for first, last in merged:
        span_values = values[first:last + 1]
        span_latency = latency_series[first:last + 1]
        score = sum(exceedance(value, warn_latency, "above") for value in span_latency) * interval_s
        episodes.append(Episode(
            rule="disk_plateau", device=device, kind="sustained", severity="warn",
            start=times[first] - interval_s, end=times[last],
            duration_s=int(times[last] - times[first] + interval_s), samples=len(span_values),
            exceeding_samples=len(span_values), mean=statistics.fmean(span_values),
            p95=percentile(span_values, 95), peak=max(span_values), score=score,
            notes=["%s flat at %.4g (p99 of run %.4g); latency averaged %.1f ms against a run median of %.1f ms" % (
                metric_label, statistics.fmean(span_values), p99, statistics.fmean(span_latency), median_latency)],
            threshold={"metric": metric_label, "plateau_cv": cv_max, "run_p99": p99,
                       "latency_floor_ms": latency_floor, "source": "plateau detection"}))
    return episodes
