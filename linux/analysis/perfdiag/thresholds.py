"""Default thresholds and override resolution (sections 6.5 and 8).

Settings are a flat dict of dotted keys. INI sections join their option names:
[disk.latency] nvme.warn_ms -> disk.latency.nvme.warn_ms. Precedence, lowest to
highest: built-in defaults < ~/.config/perfdiag/perfdiag.ini < --config FILE <
--set and the dedicated flags.

The defaults are engineering starting points, not vendor figures, except where
the spec names a source.
"""

import configparser
import difflib
import os
import re

from .timeutil import TimeError, parse_duration

MEDIA_CLASSES = ("nvme", "ssd", "hdd", "hw-raid", "virtual", "san", "cloud-ebs", "cloud-azure", "stacked", "unknown")

DEFAULT_LATENCY_MS = {
    "nvme": (2, 10),
    "ssd": (5, 20),
    "hdd": (20, 50),
    "hw-raid": (20, 50),
    "virtual": (10, 30),
    "san": (10, 30),
    "cloud-ebs": (10, 30),
    "cloud-azure": (10, 30),
    "unknown": (10, 30),
}

DEFAULTS = {
    "analysis.sustain_s": "300",
    "analysis.gap_samples": "auto",
    "analysis.min_samples": "3",
    "analysis.disk_min_tps": "5",
    "analysis.plateau_cv": "0.10",
    "analysis.exceedance_cap": "3",
    "analysis.recurring_min_pct": "1",
    # disk
    "disk.util.warn_pct": "80",
    "disk.util.crit_pct": "95",
    "disk.limit.warn_fraction": "0.80",
    "disk.limit.crit_fraction": "0.95",
    "disk.queue.warn_factor": "2",
    "disk.queue.crit_factor": "4",
    "disk.stack.warn_ms": "5",
    "disk.stack.crit_ms": "20",
    # cpu and load
    "cpu.busy.warn_idle_pct": "20",
    "cpu.busy.crit_idle_pct": "5",
    "cpu.single.warn_idle_pct": "5",
    "cpu.single.crit_idle_pct": "1",
    "cpu.single.others_idle_min_pct": "40",
    "cpu.steal.warn_pct": "5",
    "cpu.steal.crit_pct": "15",
    "cpu.soft_single.warn_pct": "30",
    "cpu.soft_single.crit_pct": "60",
    "cpu.runq.warn_per_cpu": "1.0",
    "cpu.runq.crit_per_cpu": "2.0",
    "cpu.load.warn_per_cpu": "1.0",
    "cpu.load.crit_per_cpu": "2.0",
    "cpu.psi.warn_pct": "20",
    "cpu.psi.crit_pct": "50",
    "cpu.iowait.warn_pct": "10",
    "cpu.iowait.crit_pct": "30",
    "cpu.blocked.warn": "1",
    "cpu.blocked.crit_per_cpu": "1.0",
    "cpu.cswch.warn_ratio": "2.0",
    "cpu.cswch.crit_ratio": "4.0",
    # memory
    "mem.avail.warn_pct": "10",
    "mem.avail.crit_pct": "5",
    "mem.swap_out.warn_pps": "10",
    "mem.swap_out.crit_pps": "100",
    "mem.swap_in.warn_pps": "10",
    "mem.swap_in.crit_pps": "100",
    "mem.direct_reclaim.warn_pps": "100",
    "mem.direct_reclaim.crit_pps": "1000",
    "mem.major_faults.warn_ps": "50",
    "mem.major_faults.crit_ps": "500",
    "mem.psi_some.warn_pct": "5",
    "mem.psi_some.crit_pct": "20",
    "mem.psi_full.warn_pct": "1",
    "mem.psi_full.crit_pct": "5",
    "mem.dirty.warn_pct": "10",
    "mem.dirty.crit_pct": "20",
    "mem.commit.warn_pct": "100",
    "mem.commit.crit_pct": "150",
    # PSI IO
    "io.psi_some.warn_pct": "10",
    "io.psi_some.crit_pct": "30",
    "io.psi_full.warn_pct": "5",
    "io.psi_full.crit_pct": "15",
}

for _name, (_warn, _crit) in DEFAULT_LATENCY_MS.items():
    DEFAULTS["disk.latency.%s.warn_ms" % _name] = str(_warn)
    DEFAULTS["disk.latency.%s.crit_ms" % _name] = str(_crit)

# Optional absolute disk throughput and IOPS thresholds. No default: without a
# declared limit these rules do not run (plateau detection runs instead).
# The kBps keys are kB/s as sar reports them, where 1 kB = 1024 bytes.
OPTIONAL_KEYS = (
    "disk.read_kBps.warn", "disk.read_kBps.crit",
    "disk.write_kBps.warn", "disk.write_kBps.crit",
    "disk.throughput_kBps.warn", "disk.throughput_kBps.crit",
    "disk.iops.warn", "disk.iops.crit",
)

_DEVICE_KEY = re.compile(r"^device\.(.+)\.(class|iops_limit|throughput_mibps|spindles)$")
_NUMERIC_DEVICE_FIELDS = ("iops_limit", "throughput_mibps", "spindles")
MBPS_MESSAGE = ("mbps is ambiguous (megabits or megabytes); use mibps for MiB/s, as AWS and Azure publish "
                "volume limits")
MIB_IN_KB = 1024.0


class SettingsError(ValueError):
    pass


class Settings:
    def __init__(self):
        self.values = dict(DEFAULTS)
        self.sources = {key: "default" for key in DEFAULTS}
        self.changed = {}                 # key -> (value, source) for the report

    # --- loading ---------------------------------------------------------
    def load_ini(self, path, label=None):
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            with open(path, encoding="utf-8") as handle:
                parser.read_file(handle)
        except OSError as error:
            raise SettingsError("cannot read config %s: %s" % (path, error))
        except configparser.Error as error:
            raise SettingsError("cannot parse config %s: %s" % (path, error))
        for section in parser.sections():
            for option, value in parser.items(section):
                self.set("%s.%s" % (section, option), value, label or path)

    def load_user_config(self):
        path = os.path.expanduser("~/.config/perfdiag/perfdiag.ini")
        if os.path.isfile(path):
            self.load_ini(path, path)

    def set(self, key, value, source):
        key = key.strip()
        value = str(value).strip()
        self._validate(key, value)
        if self.values.get(key) != value:
            self.changed[key] = (value, source)
        elif key in self.changed:
            del self.changed[key]
        self.values[key] = value
        self.sources[key] = source

    def _validate(self, key, value):
        if key in DEFAULTS or key in OPTIONAL_KEYS:
            if key == "analysis.gap_samples" and value == "auto":
                return
            try:
                float(value)
            except ValueError:
                raise SettingsError("setting %s needs a number, got '%s'" % (key, value))
            return
        if re.match(r"^device\..+\.throughput_mbps$", key):
            raise SettingsError(MBPS_MESSAGE)
        match = _DEVICE_KEY.match(key)
        if match:
            field = match.group(2)
            if field == "class" and value not in MEDIA_CLASSES:
                raise SettingsError("device class '%s' is not one of: %s" % (value, ", ".join(MEDIA_CLASSES)))
            if field in _NUMERIC_DEVICE_FIELDS:
                try:
                    float(value)
                except ValueError:
                    raise SettingsError("setting %s needs a number, got '%s'" % (key, value))
            return
        latency = re.match(r"^disk\.latency\.([^.]+)\.(warn_ms|crit_ms)$", key)
        if latency:
            if latency.group(1) not in MEDIA_CLASSES:
                raise SettingsError("unknown disk class '%s' in %s" % (latency.group(1), key))
            try:
                float(value)
            except ValueError:
                raise SettingsError("setting %s needs a number, got '%s'" % (key, value))
            return
        guesses = difflib.get_close_matches(key, list(DEFAULTS) + list(OPTIONAL_KEYS), n=3)
        hint = (" Did you mean: %s?" % ", ".join(guesses)) if guesses else ""
        raise SettingsError("unknown setting '%s'.%s" % (key, hint))

    # --- dedicated flags -------------------------------------------------
    def apply_flags(self, args):
        for item in args.set or []:
            if "=" not in item:
                raise SettingsError("--set needs KEY=VALUE, got '%s'" % item)
            key, _, value = item.partition("=")
            self.set(key, value, "--set")
        for item in args.latency or []:
            name, _, rest = item.partition("=")
            if not rest:
                raise SettingsError("--latency needs CLASS=WARN[,CRIT], got '%s'" % item)
            numbers = rest.split(",")
            self.set("disk.latency.%s.warn_ms" % name, numbers[0], "--latency")
            if len(numbers) > 1:
                self.set("disk.latency.%s.crit_ms" % name, numbers[1], "--latency")
        for item in args.device_limit or []:
            device, _, rest = item.partition(":")
            if not rest:
                raise SettingsError("--device-limit needs DEV:iops=N,mibps=N, got '%s'" % item)
            for part in rest.split(","):
                field, _, number = part.partition("=")
                if field == "mbps":
                    raise SettingsError(MBPS_MESSAGE)
                mapped = {"iops": "iops_limit", "mibps": "throughput_mibps"}.get(field)
                if not mapped:
                    raise SettingsError("--device-limit knows iops and mibps, got '%s'" % field)
                self.set("device.%s.%s" % (device, mapped), number, "--device-limit")
        for item in args.spindles or []:
            device, _, number = item.partition("=")
            self.set("device.%s.spindles" % device, number, "--spindles")
        for item in args.device_class or []:
            device, _, name = item.partition("=")
            self.set("device.%s.class" % device, name, "--class")
        if args.sustain:
            try:
                seconds = parse_duration(args.sustain)
            except TimeError as error:
                raise SettingsError(str(error))
            self.set("analysis.sustain_s", seconds, "--sustain")

    # --- reading ---------------------------------------------------------
    def float(self, key, default=None):
        value = self.values.get(key)
        if value is None:
            return default
        return float(value)

    def int(self, key):
        return int(float(self.values[key]))

    def has(self, key):
        return key in self.values

    def device_value(self, names, field):
        """Per-device override, looked up under each of the device's names."""
        for name in names:
            key = "device.%s.%s" % (name, field)
            if key in self.values:
                return self.values[key], self.sources[key]
        return None, None

    def latency(self, media_class):
        warn = self.float("disk.latency.%s.warn_ms" % media_class)
        crit = self.float("disk.latency.%s.crit_ms" % media_class)
        return warn, crit


def build_settings(args):
    settings = Settings()
    settings.load_user_config()
    if getattr(args, "config", None):
        settings.load_ini(args.config, args.config)
    settings.apply_flags(args)
    return settings


# ---------------------------------------------------------------------------
# Disk class resolution
# ---------------------------------------------------------------------------
class DiskProfile:
    """Everything the disk rules need to know about one device."""

    def __init__(self, name, media_class, source, warn_ms, crit_ms, parallelism, parallelism_known,
                 iops_limit=None, throughput_kBps_limit=None, kname=None, slaves=()):
        self.name = name
        self.kname = kname or name
        self.media_class = media_class
        self.source = source                     # "inventory", "override", "name" or "default"
        self.warn_ms = warn_ms
        self.crit_ms = crit_ms
        self.parallelism = parallelism
        self.parallelism_known = parallelism_known
        self.iops_limit = iops_limit
        self.throughput_kBps_limit = throughput_kBps_limit
        self.slaves = list(slaves)

    @property
    def threshold_source(self):
        return "%s:%s" % ("default" if self.source in ("default", "inventory", "name") else self.source, self.media_class)


def _class_from_name(name):
    if name.startswith("nvme"):
        return "nvme"
    if name.startswith(("vd", "xvd")):
        return "virtual"
    return "unknown"


def resolve_disk_profile(name, hardware, settings, depth=0):
    """DiskProfile for a device name as it appears in the data (kernel or display name)."""
    disk = hardware.find_disk(name) if hardware is not None else None
    names = [name]
    if disk is not None:
        names = [disk.kname, disk.display, name]
    names = [n for n in names if n]

    override, override_source = settings.device_value(names, "class")
    if override:
        media_class, source = override, "override"
    elif disk is not None:
        media_class, source = disk.media_class, "inventory"
    else:
        media_class, source = _class_from_name(name), "name"

    spindles_text, _ = settings.device_value(names, "spindles")
    parallelism, known = None, True
    if spindles_text:
        parallelism = int(float(spindles_text))
    elif disk is not None and disk.parallelism:
        parallelism = disk.parallelism
    elif media_class == "hdd":
        parallelism = 1
    elif media_class == "hw-raid":
        parallelism, known = 1, False

    slaves = disk.slaves if disk is not None else []
    if media_class == "stacked" and slaves and depth < 6:
        profiles = [resolve_disk_profile(slave, hardware, settings, depth + 1) for slave in slaves]
        warn_ms = min(profile.warn_ms for profile in profiles)
        crit_ms = min(profile.crit_ms for profile in profiles)
        if parallelism is None:
            known_values = [p.parallelism for p in profiles if p.parallelism]
            parallelism = known_values[0] if known_values else None
    else:
        warn_ms, crit_ms = settings.latency("unknown" if media_class == "stacked" else media_class)

    iops_text, _ = settings.device_value(names, "iops_limit")
    mibps_text, _ = settings.device_value(names, "throughput_mibps")
    return DiskProfile(
        name=name, media_class=media_class, source=source, warn_ms=warn_ms, crit_ms=crit_ms,
        parallelism=parallelism, parallelism_known=known,
        iops_limit=float(iops_text) if iops_text else None,
        throughput_kBps_limit=float(mibps_text) * MIB_IN_KB if mibps_text else None,
        kname=disk.kname if disk is not None else name, slaves=slaves)
