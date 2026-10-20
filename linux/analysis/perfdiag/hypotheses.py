"""Hypotheses (section 6.6): a primary indicator plus supporting and contradicting ones.

A hypothesis is raised when its primary indicator has a sustained episode, or
repeated impactful transients. Supporting and contradicting indicators count
when they overlap the primary window by at least half of the shorter of the two
windows, with one interval of tolerance at each end.

Wording is indicators, not causes. Kernel events do not become measured
hypotheses: storage errors and timeouts raise H11, cgroup OOM kills raise H12, a
global OOM kill raises H6 and a hung task raises H8. Those carry origin
"kernel" and are listed after the measured ones. When a measured hypothesis
covers the event's time, the event is added to its evidence and confidence is
high.
"""

import re

from . import attribution
from .findings import format_value
from .model import Hypothesis
from .thresholds import resolve_disk_profile
from .timeutil import stamp

REPEAT_TRANSIENTS = 3          # impactful transients that count as "repeated"
READ_DOMINANT_SHARE = 0.7
UNATTRIBUTED = "unattributed device"
H11_CATEGORIES = ("io_error", "timeout_reset", "fs_error", "raid", "multipath")
_KILLED_NAME = re.compile(r"[Kk]illed process \d+ \(([^)]+)\)")
_TASK_NAME = re.compile(r"\btask=([^,\s]+)")

TITLES = {
    "H1": "Storage device saturated",
    "H2": "Cloud volume or instance storage cap",
    "H3": "CPU saturation",
    "H4": "CPU steal or burst credit exhaustion",
    "H5": "Single-threaded bottleneck",
    "H6": "Memory pressure",
    "H7": "Writeback stall",
    "H8": "Load high from blocked tasks, not CPU",
    "H9": "Softirq or interrupt concentration",
    "H10": "Stacking-layer overhead",
    "H11": "Storage errors or timeouts",
    "H12": "Cgroup memory limit reached",
    # Database hypotheses (dbhypotheses.py), one per instance and code.
    "D1": "Database reads waiting on storage",
    "D2": "Commit latency (log or WAL writes)",
    "D3": "Lock contention",
    "D4": "Internal latch or buffer contention",
    "D5": "Database CPU demand above capacity",
    "D6": "Dirty-buffer flushing falling behind",
    "D7": "Query memory grants queued",
    "D8": "Connection or worker exhaustion",
    "D9": "Replication lag",
}
# D1, D2 and D6 take the top processes of their instance's data or log device (dbhypotheses.attribution_device).
ATTRIBUTION_KIND = {"H1": "disk", "H2": "disk", "H7": "disk", "H8": "disk",
                    "H3": "cpu", "H5": "cpu", "H9": "cpu", "H10": "cpu", "H6": "memory",
                    "D1": "disk", "D2": "disk", "D6": "disk", "D5": "cpu"}


def overlaps(primary, other, interval_s):
    """Overlap of `other` with `primary` is at least 50 % of the shorter window (+/- one interval)."""
    start = max(primary.start - interval_s, other.start)
    end = min(primary.end + interval_s, other.end)
    shared = end - start
    shorter = min(primary.end - primary.start, other.end - other.start)
    if shorter <= 0:
        return shared >= 0
    return shared >= 0.5 * shorter


def is_database_code(code):
    return code.startswith("D")


class Builder:
    def __init__(self, result):
        self.result = result
        self.host = result.host
        self.hardware = result.host.hardware
        self.interval = result.interval_s or 1
        self.counter = {}
        self.data_devices = set(self.host.devices("disk.tps")) | set(self.host.devices("disk.read_kBps"))
        self.instances = {instance.short: instance for instance in self.host.databases}

    def instance_of(self, hypothesis):
        """The DatabaseInstance a D hypothesis is about (its subject is the instance's short name)."""
        if not is_database_code(hypothesis.id):
            return None
        return self.instances.get(hypothesis.subject)

    # --- evidence ---------------------------------------------------------
    def overlapping(self, primary, rules, device=None, interval=None):
        """Episodes of `rules` that overlap `primary`. `interval` widens the tolerance for sparse series."""
        interval = interval or self.interval
        found = []
        for episode in self.result.episodes:
            if episode.rule not in rules or episode is primary:
                continue
            if device is not None and episode.device != device:
                continue
            if overlaps(primary, episode, interval):
                found.append(episode)
        found.sort(key=lambda e: -e.score)
        return found

    def kernel_events(self, primary, categories=None, device_names=None):
        events = []
        for event in self.host.kernel_events:
            if not (primary.start - self.interval <= event.time <= primary.end + self.interval):
                continue
            if categories and event.category not in categories:
                continue
            if device_names is not None and event.device not in device_names and not (set(event.disks) & device_names):
                continue
            events.append(event)
        return events

    def kname_of(self, name):
        disk = self.hardware.find_disk(name) if self.hardware is not None else None
        return disk.kname if disk is not None else name

    def event_subjects(self, event):
        """One name per distinct disk the event applies to; the name used in the data when there is one."""
        if not event.disks:
            return [UNATTRIBUTED]
        by_kname = {}
        for name in event.disks:
            by_kname.setdefault(self.kname_of(name), []).append(name)
        subjects = []
        for names in by_kname.values():
            in_data = [name for name in names if name in self.data_devices]
            subjects.append(in_data[0] if in_data else names[0])
        return subjects

    def device_names(self, device):
        """Names a kernel log might use for a device: its kernel name, display name, members."""
        names = {device}
        if self.hardware is not None:
            disk = self.hardware.find_disk(device)
            if disk is not None:
                names.add(disk.kname)
                names.update(disk.slaves)
                names.update(disk.holders)
        return names

    def label_for(self, device):
        """'nvme1n1 (/var/lib/pgsql)' using the filesystem map."""
        if self.hardware is None:
            return device
        disk = self.hardware.find_disk(device)
        kname = disk.kname if disk is not None else device
        mounts = [fs.mountpoint for fs in self.hardware.mounts_on(kname) if fs.mountpoint]
        if mounts:
            return "%s (%s)" % (device, ", ".join(mounts[:3]))
        return device

    def best_primary(self, rules, device=None):
        """(primary, others, recurring) for one subject, or None.

        Highest-scoring sustained episode; otherwise repeated impactful transients.
        """
        sustained = [e for e in self.result.episodes
                     if e.rule in rules and e.kind == "sustained" and (device is None or e.device == device)]
        if sustained:
            sustained.sort(key=lambda e: -e.score)
            return sustained[0], sustained[1:], False
        transients = [e for e in self.result.episodes
                      if e.rule in rules and e.kind == "transient" and e.impact == "impactful"
                      and (device is None or e.device == device)]
        if len(transients) >= REPEAT_TRANSIENTS and self.recurring_long_enough(transients):
            transients.sort(key=lambda e: -e.score)
            return transients[0], transients[1:], True
        return None

    def recurring_long_enough(self, transients):
        """Bursts that add up to a real share of the window, not three blips in a week."""
        host = self.host
        window = (host.end - host.start) if host.start is not None else 0
        minimum = max(self.result.sustain_s, self.result.settings.float("analysis.recurring_min_pct") / 100.0 * window)
        return sum(e.duration_s for e in transients) >= minimum

    def subjects(self, rules):
        names = []
        for episode in self.result.episodes:
            if episode.rule in rules and episode.device not in names:
                names.append(episode.device)
        return names

    # --- construction -------------------------------------------------------
    def make(self, code, subject, primary, other_primary, recurring, supporting, contradicting, facts_for,
             facts_against, kernel=(), steps=None, extra_notes=()):
        supporting_count = len(supporting) + len(facts_for) + len(kernel)
        contradicting_count = len(contradicting) + len(facts_against)
        confidence = confidence_for(supporting_count, contradicting_count)
        if kernel:
            confidence = "high"
        hypothesis = Hypothesis(
            id=code, title=TITLES[code], subject=subject, confidence=confidence, primary=primary,
            supporting=supporting, contradicting=contradicting, other_primary=list(other_primary),
            kernel_events=list(kernel), supporting_facts=list(facts_for), contradicting_facts=list(facts_against),
            recurring=recurring, notes=list(extra_notes), next_steps=steps if steps is not None else [])
        hypothesis.rank_score = primary.score if primary is not None else 0.0
        return hypothesis


def confidence_for(supporting, contradicting):
    if contradicting >= supporting:
        return "low"
    if contradicting == 0 and supporting >= 2:
        return "high"
    return "medium"


def build_hypotheses(result):
    builder = Builder(result)
    found = []
    for build in (h1_storage, h2_cloud_cap, h3_cpu, h4_steal, h5_single_thread, h6_memory, h7_writeback,
                  h8_blocked_load, h9_softirq, h10_stacking):
        found.extend(build(builder))
    found = merge_stacked_layers(builder, found, "H1")
    found = merge_stacked_layers(builder, found, "H7")
    found.extend(h11_storage_errors(builder))
    found.extend(h12_cgroup_oom(builder))
    found.extend(kernel_only_hypotheses(builder, found))
    _note_cgroup_oom_on_memory_pressure(builder, found)
    if builder.instances:
        from . import dbhypotheses
        found.extend(dbhypotheses.build_database_hypotheses(builder))
    for hypothesis in found:
        _attach_evidence(builder, hypothesis)
    if builder.instances:
        # After the top processes exist (they decide which databases an H hypothesis touches) and before
        # the next steps are written (they depend on which engines are involved).
        dbhypotheses.link_databases(builder, found)
    for hypothesis in found:
        hypothesis.next_steps = next_steps_for(builder, hypothesis)
    measured = sorted((h for h in found if h.origin == "measured"), key=lambda h: (-h.rank_score, h.id))
    from_kernel = sorted((h for h in found if h.origin == "kernel"),
                         key=lambda h: (min(e.time for e in h.kernel_events), h.id))
    found = measured + from_kernel
    for number, hypothesis in enumerate(found, start=1):
        hypothesis.sequence = number
    result.hypotheses = found
    return found


def merge_stacked_layers(builder, found, code):
    """One hypothesis per physical problem, not per stacking layer (LVM, LUKS, multipath, md).

    A stacked device U whose members also have a hypothesis of the same code over the same window:
    one member overlaps -> U is dropped and listed as "also seen on" that member;
    several members overlap (RAID, striping) -> the members are folded into U;
    none overlaps -> U stays, the latency was added above the members.
    """
    hardware = builder.hardware
    if hardware is None:
        return found

    def disk_of(hypothesis):
        return hardware.find_disk(hypothesis.subject)

    def is_stacked(hypothesis):
        profile = builder.result.profile_for(hypothesis.subject)
        return profile is not None and profile.media_class == "stacked" and disk_of(hypothesis) is not None

    group = [h for h in found if h.id == code and h.primary is not None]
    stacked = [h for h in group if is_stacked(h)]

    def layers(hypothesis):
        kname = disk_of(hypothesis).kname
        return sum(1 for name in hardware.disks if hardware.sits_on(kname, name))

    removed = set()
    for upper in sorted(stacked, key=layers):
        upper_kname = disk_of(upper).kname
        members = []
        for other in group:
            if other is upper or id(other) in removed or disk_of(other) is None:
                continue
            if hardware.sits_on(upper_kname, disk_of(other).kname) and overlaps(upper.primary, other.primary, builder.interval):
                members.append(other)
        if len(members) == 1:
            members[0].related.append((upper.subject, upper.primary))
            members[0].related.extend(upper.related)
            removed.add(id(upper))
        elif len(members) >= 2:
            for member in members:
                upper.related.append((member.subject, member.primary))
                upper.related.extend(member.related)
                removed.add(id(member))
    return [h for h in found if id(h) not in removed]


# ---------------------------------------------------------------------------
# Individual hypotheses
# ---------------------------------------------------------------------------
H1_PRIMARY = ("disk_await", "disk_r_await", "disk_w_await")


def h1_storage(builder):
    found = []
    for device in builder.subjects(H1_PRIMARY):
        picked = builder.best_primary(H1_PRIMARY, device)
        if picked is None:
            continue
        primary, others, recurring = picked
        supporting = []
        for rule in ("disk_queue", "disk_util"):
            supporting.extend(builder.overlapping(primary, (rule,), device)[:1])
        for rule in ("psi_io_some", "psi_io_full", "blocked", "iowait"):
            supporting.extend(builder.overlapping(primary, (rule,))[:1])
        facts_for = []
        if builder.overlapping(primary, ("load",)) and not builder.overlapping(primary, ("cpu_busy",)):
            supporting.extend(builder.overlapping(primary, ("load",))[:1])
        kernel = builder.kernel_events(primary, H11_CATEGORIES, builder.device_names(device))
        plateaus = builder.overlapping(primary, ("disk_plateau",), device)
        if plateaus:
            supporting.append(plateaus[0])
            facts_for.append("throughput was flat at %s while latency rose: the device was working at its limit" %
                             format_value(plateaus[0].mean, plateaus[0].threshold.get("unit", ""), trim=True))
        facts_against = []
        found.append(builder.make(
            "H1", device, primary, others, recurring, supporting, [], facts_for, facts_against, kernel))
    return found


def h2_cloud_cap(builder):
    result = builder.result
    found = []
    hardware = builder.hardware
    for device in builder.subjects(("disk_plateau",)):
        if device != "cloud-sum":
            profile = result.profile_for(device)
            if profile is None or profile.media_class not in ("cloud-ebs", "cloud-azure"):
                continue
        picked = builder.best_primary(("disk_plateau",), device)
        if picked is None:
            continue
        primary, others, recurring = picked
        latency_rules = ("disk_await", "disk_r_await", "disk_w_await")
        supporting = builder.overlapping(primary, latency_rules, None if device == "cloud-sum" else device)[:1]
        for rule in ("psi_io_some", "psi_io_full"):
            supporting.extend(builder.overlapping(primary, (rule,))[:1])
        facts_for, facts_against = [], []
        instance = ""
        if hardware is not None:
            instance = hardware.get("cloud.instance_type") or ""
        if instance:
            facts_for.append("instance type %s is known, so its storage limits can be looked up" % instance)
        if not supporting:
            facts_against.append("no sustained latency rise or IO pressure overlapped the plateau")
        found.append(builder.make("H2", device, primary, others, recurring, supporting, [], facts_for, facts_against))
    return found


def h3_cpu(builder):
    picked = builder.best_primary(("cpu_busy", "runq"))
    if picked is None:
        return []
    primary, others, recurring = picked
    supporting = list(builder.overlapping(primary, ("psi_cpu",))[:1])
    supporting.extend(builder.overlapping(primary, ("cswch",))[:1])
    contradicting = builder.overlapping(primary, ("cpu_steal",))[:1]
    return [builder.make("H3", "host", primary, others, recurring, supporting, contradicting, [], [])]


def h4_steal(builder):
    picked = builder.best_primary(("cpu_steal",))
    if picked is None:
        return []
    primary, others, recurring = picked
    supporting = list(builder.overlapping(primary, ("cpu_busy",))[:1])
    facts_for = []
    instance = builder.hardware.get("cloud.instance_type") if builder.hardware is not None else ""
    if not instance and builder.hardware is not None:
        instance = infer_instance_type(builder.hardware)
    if is_burstable(instance, builder.hardware):
        facts_for.append("instance type %s is a burstable family" % instance)
    return [builder.make("H4", "host", primary, others, recurring, supporting, [], facts_for, [])]


def h5_single_thread(builder):
    found = []
    cpu_devices = builder.subjects(("cpu_single",))
    for device in cpu_devices:
        picked = builder.best_primary(("cpu_single",), device)
        if picked is None:
            continue
        primary, others, recurring = picked
        contradicting = builder.overlapping(primary, ("cpu_busy",))[:1]
        facts_for = []
        concurrent = [e for e in builder.overlapping(primary, ("cpu_single",)) if e.device != device]
        if not concurrent:
            facts_for.append("the same CPU (%s) was saturated in every exceeding sample" % device)
        hot = attribution.single_hot_process(builder.host, primary.start, primary.end)
        if hot is not None:
            facts_for.append("%s averaged %.0f %% of one CPU" % (hot["command"], hot["cpu_pct"]))
        found.append(builder.make(
            "H5", "CPU %s" % device, primary, others, recurring, [], contradicting, facts_for, []))
    return found


H6_PRIMARY = ("swap_out", "direct_reclaim", "psi_mem_some", "psi_mem_full")


def h6_memory(builder):
    picked = builder.best_primary(H6_PRIMARY)
    if picked is None:
        return []
    primary, others, recurring = picked
    supporting = []
    for rule in ("mem_avail", "major_faults", "swap_in"):
        supporting.extend(builder.overlapping(primary, (rule,))[:1])
    kernel = builder.kernel_events(primary, ("oom",))
    return [builder.make("H6", "host", primary, others, recurring, supporting, [], [], [], kernel)]


def window_split(builder, primary, device):
    """(read share, write share) of the device's throughput in the primary window."""
    stats = attribution.device_throughput(builder.host, device, primary.start, primary.end)
    if stats is None or (stats[0] + stats[1]) <= 0:
        return None
    total = stats[0] + stats[1]
    return stats[0] / total, stats[1] / total


def h7_writeback(builder):
    found = []
    primary_rules = ("disk_w_await", "disk_await")
    for device in builder.subjects(primary_rules):
        picked = builder.best_primary(primary_rules, device)
        if picked is None:
            continue
        primary, others, recurring = picked
        shares = window_split(builder, primary, device)
        supporting = []
        for rule in ("dirty", "psi_io_full"):
            supporting.extend(builder.overlapping(primary, (rule,))[:1])
        facts_for, facts_against = [], []
        if shares is not None:
            if shares[1] >= READ_DOMINANT_SHARE:
                facts_for.append("%.0f %% of the device's throughput in the window was writes" % (100 * shares[1]))
            elif shares[0] >= READ_DOMINANT_SHARE:
                facts_against.append("%.0f %% of the device's throughput in the window was reads" % (100 * shares[0]))
        # Without dirty-page or full-stall evidence this is just H1, not a writeback story.
        if not supporting or facts_against:
            continue
        found.append(builder.make("H7", device, primary, others, recurring, supporting, [], facts_for, facts_against))
    return found


def h8_blocked_load(builder):
    picked = builder.best_primary(("load",))
    if picked is None:
        return []
    primary, others, recurring = picked
    supporting = []
    for rule in ("blocked", "iowait"):
        supporting.extend(builder.overlapping(primary, (rule,))[:1])
    contradicting = builder.overlapping(primary, ("cpu_busy",))[:1]
    facts_for = []
    if not contradicting:
        facts_for.append("CPUs were not saturated during the high load")
    kernel = builder.kernel_events(primary, ("hung_task",))
    return [builder.make("H8", "host", primary, others, recurring, supporting, contradicting, facts_for, [], kernel)]


def h9_softirq(builder):
    found = []
    for device in builder.subjects(("cpu_soft_single",)):
        picked = builder.best_primary(("cpu_soft_single",), device)
        if picked is None:
            continue
        primary, others, recurring = picked
        supporting = builder.overlapping(primary, ("softnet_drop",))[:1]
        found.append(builder.make("H9", "CPU %s" % device, primary, others, recurring, supporting, [], [], []))
    return found


def h10_stacking(builder):
    found = []
    for device in builder.subjects(("stack_overhead",)):
        picked = builder.best_primary(("stack_overhead",), device)
        if picked is None:
            continue
        primary, others, recurring = picked
        facts_for = []
        disk = builder.hardware.find_disk(device) if builder.hardware is not None else None
        if disk is not None and disk.dm_target == "crypt":
            facts_for.append("the device-mapper target is crypt, which costs CPU")
        kernel = builder.kernel_events(primary, ("raid",))
        found.append(builder.make("H10", device, primary, others, recurring, [], [], facts_for, [], kernel))
    return found


# ---------------------------------------------------------------------------
# Kernel-log hypotheses and context
# ---------------------------------------------------------------------------
def _kernel_hypothesis(builder, code, subject, events):
    hypothesis = builder.make(code, subject, None, [], False, [], [], [], [], events)
    hypothesis.origin = "kernel"
    return hypothesis


def h11_storage_errors(builder):
    """IO errors, timeouts and similar: a fault in the device or path, listed per disk."""
    by_subject = {}
    for event in builder.host.kernel_events:
        if event.category not in H11_CATEGORIES or event.ignored:
            continue
        for subject in builder.event_subjects(event):
            by_subject.setdefault(subject, []).append(event)
    found = []
    for subject, events in by_subject.items():
        hypothesis = _kernel_hypothesis(builder, "H11", subject, events)
        hypothesis.confidence = "medium" if subject == UNATTRIBUTED else "high"
        found.append(hypothesis)
    return found


def killed_process_name(event):
    for pattern in (_KILLED_NAME, _TASK_NAME):
        match = pattern.search(event.text)
        if match:
            return match.group(1)
    return "cgroup"


def h12_cgroup_oom(builder):
    by_subject = {}
    for event in builder.host.kernel_events:
        if event.category == "oom_cgroup":
            by_subject.setdefault(killed_process_name(event), []).append(event)
    return [_kernel_hypothesis(builder, "H12", subject, events) for subject, events in by_subject.items()]


def kernel_only_hypotheses(builder, found):
    """A global OOM kill or hung task that no measured H6 / H8 window already covers."""
    extra = []
    for code, category in (("H6", "oom"), ("H8", "hung_task")):
        covered = set()
        for hypothesis in found:
            if hypothesis.id == code:
                covered.update(id(event) for event in hypothesis.kernel_events)
        leftover = [e for e in builder.host.kernel_events if e.category == category and id(e) not in covered]
        if leftover:
            extra.append(_kernel_hypothesis(builder, code, "host", leftover))
    return extra


def _note_cgroup_oom_on_memory_pressure(builder, found):
    fact = "a cgroup OOM kill occurred in this window"
    cgroup_events = [e for e in builder.host.kernel_events if e.category == "oom_cgroup"]
    for hypothesis in found:
        if hypothesis.id != "H6" or hypothesis.primary is None or fact in hypothesis.supporting_facts:
            continue
        primary = hypothesis.primary
        if any(primary.start - builder.interval <= e.time <= primary.end + builder.interval for e in cgroup_events):
            hypothesis.supporting_facts.append(fact)
            if not hypothesis.kernel_events:
                hypothesis.confidence = confidence_for(
                    len(hypothesis.supporting) + len(hypothesis.supporting_facts),
                    len(hypothesis.contradicting) + len(hypothesis.contradicting_facts))


def _attach_evidence(builder, hypothesis):
    """Kernel events and top processes of the primary window."""
    primary = hypothesis.primary
    host = builder.host
    start, end = (primary.start, primary.end) if primary is not None else (None, None)
    # The OS of a remote database's collecting machine says nothing about that database.
    database = builder.instance_of(hypothesis)
    os_applies = not is_database_code(hypothesis.id) or (database is not None and database.local is True)
    if primary is not None and os_applies:
        for event in host.kernel_events:
            if start - builder.interval <= event.time <= end + builder.interval and event not in hypothesis.kernel_events:
                hypothesis.kernel_events.append(event)
    kind = ATTRIBUTION_KIND.get(hypothesis.id)
    if kind and primary is not None and os_applies:
        if database is not None:
            from . import dbhypotheses
            device = dbhypotheses.attribution_device(builder, hypothesis, database)
        else:
            device = hypothesis.subject if hypothesis.id in ("H1", "H2", "H7") and hypothesis.subject != "cloud-sum" else None
        hypothesis.top_processes = attribution.top_processes(host, start, end, kind, device)


# ---------------------------------------------------------------------------
# Instance type helpers
# ---------------------------------------------------------------------------
def infer_instance_type(hardware):
    """AWS Nitro puts the instance type in DMI product_name; accept it only when it looks like one."""
    import re

    if hardware.get("cloud.provider") != "aws":
        return ""
    product = hardware.get("cloud.product_name")
    if re.fullmatch(r"[a-z][a-z0-9-]*\d[a-z0-9-]*\.[a-z0-9]+", product or ""):
        return product
    return ""


def is_burstable(instance_type, hardware):
    if not instance_type:
        return False
    provider = hardware.get("cloud.provider") if hardware is not None else ""
    lowered = instance_type.lower()
    if provider == "aws" or lowered.startswith(("t2", "t3", "t4g")):
        return lowered.startswith("t") and lowered[1:2].isdigit()
    if provider == "azure" or lowered.startswith("standard_"):
        return lowered.startswith("standard_b")
    return False


def stack_includes_class(builder, device, media_class, depth=0):
    """True when the device is of `media_class`, or is stacked on members that are."""
    if depth > 6:
        return False
    profile = resolve_disk_profile(device, builder.hardware, builder.result.settings)
    if profile.media_class == media_class:
        return True
    return any(stack_includes_class(builder, slave, media_class, depth + 1) for slave in profile.slaves)


SAN_NEXT_STEP = ("Check the array's own latency for this LUN and the path state: multipath -ll. "
                 "iSCSI: iscsiadm -m session -P 3 and errors on the storage interface. "
                 "FC: /sys/class/fc_host/host*/port_state.")


def _h11_steps(builder, hypothesis):
    zone = builder.host.zone
    first = min(event.time for event in hypothesis.kernel_events) - 60
    last = max(event.time for event in hypothesis.kernel_events) + 60
    steps = ["Read the full kernel log around these times: journalctl -k --since '%s' --until '%s' (or dmesg -T)." % (
        stamp(first, zone), stamp(last, zone))]
    subject = hypothesis.subject
    attributed = subject != UNATTRIBUTED
    disk_path = "/dev/%s" % builder.kname_of(subject) if attributed else "/dev/<disk>"
    steps.append("Device health: smartctl -x %s (SATA, SAS), nvme smart-log %s and nvme error-log %s (NVMe)." % (
        disk_path, disk_path, disk_path))
    categories = {event.category for event in hypothesis.kernel_events}
    if (attributed and stack_includes_class(builder, subject, "san")) or "multipath" in categories:
        steps.append(SAN_NEXT_STEP)
    if attributed and (stack_includes_class(builder, subject, "cloud-ebs") or
                       stack_includes_class(builder, subject, "cloud-azure")):
        steps.append("Check the volume's status in the provider: EBS volume status checks, or Azure disk health and "
                     "the VM's Resource Health.")
    if attributed and stack_includes_class(builder, subject, "virtual"):
        steps.append("Ask the platform team to check datastore or host storage health for this VM.")
    return steps


# ---------------------------------------------------------------------------
# Next steps (fixed text)
# ---------------------------------------------------------------------------
def next_steps_for(builder, hypothesis):
    code = hypothesis.id
    hardware = builder.hardware
    provider = hardware.get("cloud.provider") if hardware is not None else ""
    steps = []
    subject = hypothesis.subject

    if code == "H1":
        steps.append("Review the top IO processes for this window." if hypothesis.top_processes else
                     "No process data for this window. During the next occurrence run pidstat -d 1 to find the top IO processes.")
        steps.append("Live: iostat -x 1 %s and pidstat -d 1." % subject)
        req = builder.host.get("disk.req_kB", subject)
        if req is not None and hypothesis.primary is not None:
            values = [v for t, v in zip(req.times, req.values) if hypothesis.primary.start < t <= hypothesis.primary.end]
            if values:
                mean = sum(values) / len(values)
                shape = ("small-block random" if mean <= 16 else "sequential or streaming" if mean >= 128 else "mixed-size")
                steps.append("Compare the average request size (%.0f kB, %s IO) with what the workload should issue." % (mean, shape))
        disk = hardware.find_disk(subject) if hardware is not None else None
        if disk is not None and disk.media_class in ("hdd", "hw-raid"):
            steps.append("Check for a RAID rebuild or resync: cat /proc/mdstat and the controller tools.")
        if stack_includes_class(builder, subject, "san"):
            steps.append(SAN_NEXT_STEP)
        steps.append("Consider a faster storage tier, spreading IO across devices, or reducing synchronous writes.")
    elif code == "H2":
        if provider in ("aws", ""):
            steps.append("AWS: check CloudWatch VolumeIOPSExceededCheck, VolumeThroughputExceededCheck and BurstBalance "
                         "(gp2/st1/sc1) for the volume, and InstanceEBSIOPSExceededCheck, "
                         "InstanceEBSThroughputExceededCheck, EBSIOBalance%% and EBSByteBalance%% for the instance.")
        if provider in ("azure", ""):
            steps.append("Azure: check Data Disk / OS Disk IOPS and Bandwidth Consumed Percentage, and VM Cached / "
                         "Uncached IOPS and Bandwidth Consumed Percentage.")
        if subject == "cloud-sum":
            steps.append("The sum across volumes is flat while individual volumes may have headroom: this points at the "
                         "instance or VM limit, not at one volume.")
        if hypothesis.primary is not None:
            steps.append("Compare the provisioned IOPS and throughput with the observed plateau (%s)." %
                         "; ".join(hypothesis.primary.notes[:1]))
    elif code == "H3":
        steps.append("Review the top CPU processes for this window." if hypothesis.top_processes else
                     "No process data for this window. Run pidstat -u 1 during the next occurrence.")
        steps.append("Live: perf top and pidstat -u -t 1.")
        steps.append("Check whether the work scales with cores before resizing.")
    elif code == "H4":
        if provider == "aws":
            steps.append("AWS: check CloudWatch CPUCreditBalance and CPUSurplusCreditsCharged.")
        elif provider == "azure":
            steps.append("Azure: check the B-series CPU credit metrics (CPU Credits Remaining and Consumed).")
        else:
            steps.append("AWS: CloudWatch CPUCreditBalance and CPUSurplusCreditsCharged. "
                         "Azure: B-series CPU credit metrics.")
        steps.append("On a non-burstable size, steal means noisy-neighbour or host contention: raise it with the "
                     "provider or move to another host or size.")
    elif code == "H5":
        steps.append("Identify the busy thread with pidstat -t 1. Look for a single worker, a lock or a GC thread.")
        steps.append("Adding cores will not help a single-threaded bottleneck.")
    elif code == "H6":
        steps.append("Review the largest RSS growth for this window." if hypothesis.top_processes else
                     "No process data for this window. Track RSS with pidstat -r 1.")
        swappiness = hardware.get("vm.swappiness") if hardware is not None else ""
        thp = hardware.get("mem.thp_enabled") if hardware is not None else ""
        steps.append("Check vm.swappiness%s, transparent hugepages%s and any cgroup memory limits." % (
            " (now %s)" % swappiness if swappiness else "", " (now %s)" % thp if thp else ""))
        steps.append("Determine whether the working set exceeds RAM.")
    elif code == "H7":
        ratio = hardware.get("vm.dirty_ratio") if hardware is not None else ""
        background = hardware.get("vm.dirty_background_ratio") if hardware is not None else ""
        steps.append("Inspect vm.dirty_*%s." % (" (dirty_ratio=%s, dirty_background_ratio=%s)" % (ratio, background)
                                                 if ratio else ""))
        steps.append("Look for fsync-heavy processes and check the filesystem journal mode.")
        steps.append("Lower vm.dirty_background_* to start writeback earlier.")
    elif code == "H8":
        steps.append("Load includes tasks in uninterruptible sleep. Find them: ps -eo state,pid,cmd | awk '$1==\"D\"'.")
        steps.append("Check storage (H1) and network filesystems (NFS, CIFS).")
    elif code == "H11":
        steps.extend(_h11_steps(builder, hypothesis))
    elif code == "H12":
        steps.append("A cgroup (a container or a systemd service) reached its own memory limit; host memory was not "
                     "necessarily short.")
        steps.append("systemd: systemctl show <unit> -p MemoryMax -p MemoryHigh. cgroup v2: memory.max and memory.events "
                     "in the cgroup directory. Containers: the runtime's memory limit (podman inspect, docker inspect, "
                     "Kubernetes resources.limits).")
        steps.append("Compare the process's RSS in pidstat with the limit, and decide whether the limit or the workload "
                     "is wrong.")
    elif code == "H9":
        steps.append("Check RSS/RPS and IRQ affinity (/proc/interrupts, irqbalance) and the NIC queue count.")
    elif code == "H10":
        steps.append("Inspect dmsetup table and /proc/mdstat.")
        steps.append("Check the CPU use of kcryptd and md*_raid* threads in the pidstat log.")
    if builder.instances:
        from . import dbhypotheses
        if is_database_code(code):
            steps.extend(dbhypotheses.database_next_steps(builder, hypothesis))
        else:
            steps.extend(dbhypotheses.engine_next_steps(builder, hypothesis))
    return steps
