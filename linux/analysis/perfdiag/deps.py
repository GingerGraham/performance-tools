"""Dependency check and install hints (`perfdiag check`)."""

import platform
import shutil
import subprocess
import sys

MIN_PYTHON = (3, 9)
VENV_COMMAND = "python3 -m venv ~/.venvs/perfdiag && ~/.venvs/perfdiag/bin/pip install matplotlib"


def read_os_release(path="/etc/os-release"):
    values = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if "=" in line:
                    key, _, value = line.strip().partition("=")
                    values[key] = value.strip('"')
    except OSError:
        pass
    return values


def detect_platform(os_release=None, uname_release=None, sys_platform=None):
    """'fedora', 'debian', 'macos', 'wsl-debian' or 'other'."""
    sys_platform = sys_platform or sys.platform
    if sys_platform == "darwin":
        return "macos"
    if os_release is None:
        os_release = read_os_release()
    release = (uname_release if uname_release is not None else platform.uname().release).lower()
    identifiers = [os_release.get("ID", "")] + os_release.get("ID_LIKE", "").split()
    family = "other"
    if any(item in ("fedora", "rhel", "centos", "rocky", "almalinux", "amzn") for item in identifiers):
        family = "fedora"
    elif any(item in ("debian", "ubuntu") for item in identifiers):
        family = "debian"
    if "microsoft" in release and family != "other":
        return "wsl-" + family
    return family


def matplotlib_hint(platform_name):
    if platform_name == "fedora":
        return "sudo dnf install python3-matplotlib"
    if platform_name in ("debian", "wsl-debian"):
        return "sudo apt install python3-matplotlib"
    if platform_name == "wsl-fedora":
        return "sudo dnf install python3-matplotlib"
    return VENV_COMMAND


def sadf_hint(platform_name):
    if platform_name in ("fedora", "wsl-fedora"):
        return "sudo dnf install sysstat"
    if platform_name in ("debian", "wsl-debian"):
        return "sudo apt install sysstat"
    if platform_name == "macos":
        return "not available on macOS. Re-run gather-perf-diag.sh on the server to produce sadf/ exports"
    return "install the sysstat package"


def matplotlib_version():
    try:
        import matplotlib
        return matplotlib.__version__
    except ImportError:
        return None


def sadf_version():
    path = shutil.which("sadf")
    if not path:
        return None
    try:
        result = subprocess.run([path, "-V"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or result.stderr).strip().splitlines()
    for line in text:
        if "version" in line.lower():
            return line.split()[-1]
    return "unknown"


def require_matplotlib():
    """Returns the version, or raises DependencyError with install steps."""
    version = matplotlib_version()
    if version is None:
        hint = matplotlib_hint(detect_platform())
        raise DependencyError(
            "matplotlib is required for plot and report but is not installed.\n"
            "  install:  %s\n"
            "  'perfdiag analyse' needs no extra packages." % hint)
    return version


class DependencyError(Exception):
    pass


def run_check(out=None):
    """Prints the check table. Returns the process exit code (1 if a required item is missing)."""
    out = out or sys.stdout
    platform_name = detect_platform()
    exit_code = 0

    version = platform.python_version()
    python_ok = sys.version_info[:2] >= MIN_PYTHON
    out.write("%-11s %-9s %s\n" % ("python", version,
                                   "OK" if python_ok else "too old: Python %d.%d or newer is required" % MIN_PYTHON))
    if not python_ok:
        exit_code = 1

    mpl = matplotlib_version()
    if mpl:
        out.write("%-11s %-9s OK\n" % ("matplotlib", mpl))
    else:
        out.write("%-11s %-9s MISSING: needed for plot and report (analyse and inspect still work)\n" % ("matplotlib", "—"))
        out.write("%-11s %-9s install:  %s\n" % ("", "", matplotlib_hint(platform_name)))
        if platform_name in ("fedora", "debian", "wsl-fedora", "wsl-debian"):
            out.write("%-11s %-9s or, if the distro package is old:  %s\n" % ("", "", VENV_COMMAND))
        exit_code = 1

    sadf = sadf_version()
    if sadf:
        out.write("%-11s %-9s OK (used only for bundles without sadf/ exports)\n" % ("sadf", sadf))
    else:
        out.write("%-11s %-9s optional, not found: only needed for bundles without sadf/ exports\n" % ("sadf", "—"))
        out.write("%-11s %-9s install:  %s\n" % ("", "", sadf_hint(platform_name)))
    return exit_code
