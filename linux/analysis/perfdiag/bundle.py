"""Open a gather bundle (.tar.gz, .zip), a gather directory or a single run directory.

Bundles come from customer servers and are untrusted input. Every archive member
is checked BEFORE anything is extracted:

* tar: absolute names, '..' components, links, devices and FIFOs are refused.
  Python 3.12+ also applies tarfile's own "data" filter.
* zip: names that resolve outside the target directory, and symbolic links, are refused.
* The declared uncompressed size is capped.
* A truncated or corrupt archive is a BundleError, not a traceback.
* The sha256 lines of gather_manifest.txt are checked; a mismatch is a note, not an error.

Extraction goes to tempfile.mkdtemp() and is removed on exit unless keep=True.
"""

import contextlib
import hashlib
import os
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
import zlib

from .model import Run
from .readers.manifest import read_key_values

MAX_UNPACKED_BYTES = 16 * 1024 ** 3
MAX_LISTED_CHECKSUMS = 10

_RUN_NAME = re.compile(r"^(.+)_([A-Za-z0-9]+)_(\d{8})_(\d{6})$")


class BundleError(Exception):
    """The bundle cannot be opened safely."""


def _check_name(name):
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or re.match(r"^[A-Za-z]:", normalised):
        raise BundleError("refusing bundle: member '%s' has an absolute path" % name)
    if ".." in normalised.split("/"):
        raise BundleError("refusing bundle: member '%s' climbs out of the target directory" % name)


def validate_tar(archive):
    total = 0
    for member in archive.getmembers():
        _check_name(member.name)
        if member.issym() or member.islnk():
            raise BundleError("refusing bundle: member '%s' is a link" % member.name)
        if member.isdev() or member.isfifo():
            raise BundleError("refusing bundle: member '%s' is a device or FIFO" % member.name)
        total += max(member.size, 0)
        if total > MAX_UNPACKED_BYTES:
            raise BundleError("refusing bundle: unpacked size exceeds %d GiB" % (MAX_UNPACKED_BYTES // 1024 ** 3))


def validate_zip(archive, target):
    total = 0
    real_target = os.path.realpath(target)
    for info in archive.infolist():
        _check_name(info.filename)
        resolved = os.path.realpath(os.path.join(target, info.filename))
        if resolved != real_target and not resolved.startswith(real_target + os.sep):
            raise BundleError("refusing bundle: member '%s' resolves outside the target directory" % info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise BundleError("refusing bundle: member '%s' is a symbolic link" % info.filename)
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise BundleError("refusing bundle: unpacked size exceeds %d GiB" % (MAX_UNPACKED_BYTES // 1024 ** 3))


def _damaged(path, error):
    return BundleError("archive %s is damaged or truncated (%s). Copy it again from the server" % (path, error))


def extract_tar(path, target):
    try:
        archive = tarfile.open(path, "r:*")
    except (tarfile.TarError, EOFError, zlib.error, OSError) as error:
        raise BundleError("cannot read %s: %s" % (path, error))
    with archive:
        try:
            validate_tar(archive)
            if hasattr(tarfile, "data_filter"):
                archive.extractall(target, filter="data")
            else:
                archive.extractall(target)
        except BundleError:
            raise
        except (tarfile.TarError, EOFError, zlib.error, OSError) as error:
            raise _damaged(path, error)


def extract_zip(path, target):
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, EOFError, zlib.error, OSError) as error:
        raise BundleError("cannot read %s: %s" % (path, error))
    with archive:
        try:
            validate_zip(archive, target)
            archive.extractall(target)
        except BundleError:
            raise
        except (zipfile.BadZipFile, EOFError, zlib.error, OSError) as error:
            raise _damaged(path, error)


class Bundle:
    """An opened bundle: where its files are and which runs it holds."""

    def __init__(self, source, root):
        self.source = source
        self.root = root
        self.gather_manifest = {}
        self.runs = []
        self.kernel_log_path = None
        self.checksums = None          # result of verify_checksums(), or None when not run
        self.notes = []
        self._discover()

    def _discover(self):
        gather_path = os.path.join(self.root, "gather_manifest.txt")
        kernel_path = os.path.join(self.root, "kernel_events.log")
        if os.path.isfile(gather_path):
            self.gather_manifest = read_key_values(gather_path)
        if os.path.isfile(kernel_path):
            self.kernel_log_path = kernel_path
        self.runs = discover_runs(self.root)

    def verify_checksums(self, chunk_bytes=1024 * 1024):
        """Checks every sha256.<path> line of gather_manifest.txt against the files under root.

        Returns {"verified": n, "mismatched": [path, ...], "missing": [path, ...]}.
        """
        verified, mismatched, missing = 0, [], []
        for key, expected in self.gather_manifest.items():
            if not key.startswith("sha256."):
                continue
            relative = key[len("sha256."):]
            path = os.path.join(self.root, relative)
            if not os.path.isfile(path):
                missing.append(relative)
                continue
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(chunk_bytes), b""):
                    digest.update(chunk)
            if digest.hexdigest() == expected.strip().lower():
                verified += 1
            else:
                mismatched.append(relative)
        self.checksums = {"verified": verified, "mismatched": mismatched, "missing": missing}
        return self.checksums

    def checksum_note(self):
        """One note naming the damaged files, or None when everything verified."""
        result = self.checksums
        if not result or not (result["mismatched"] or result["missing"]):
            return None
        parts = []
        for problem, names in (("mismatch", result["mismatched"]), ("missing file", result["missing"])):
            if not names:
                continue
            listed = ", ".join(names[:MAX_LISTED_CHECKSUMS])
            if len(names) > MAX_LISTED_CHECKSUMS:
                listed += ", and %d more" % (len(names) - MAX_LISTED_CHECKSUMS)
            parts.append("checksum %s for %d file%s (%s)" % (problem, len(names), "" if len(names) == 1 else "s", listed))
        return "%s: the bundle may be damaged; results for those files may be wrong" % "; ".join(parts)

    def hosts(self):
        names = []
        for run in self.runs:
            if run.host not in names:
                names.append(run.host)
        return names

    def runs_for(self, host):
        return [run for run in self.runs if run.host == host]


def _run_from_directory(path):
    name = os.path.basename(path.rstrip("/"))
    manifest = {}
    manifest_path = os.path.join(path, "manifest.txt")
    if os.path.isfile(manifest_path):
        manifest = read_key_values(manifest_path)
    match = _RUN_NAME.match(name)
    tag = match.group(2) if match else ""
    host = manifest.get("hostname") or (match.group(1) if match else "")
    if not tag:
        script = manifest.get("script", "")
        tag = {"collect-os-metrics.sh": "os", "psar.sh": "psar", "collect-hw-inventory.sh": "hw",
               "collect-postgresql-metrics.sh": "postgresql", "collect-mysql-metrics.sh": "mysql",
               "collect-oracle-metrics.sh": "oracle", "collect-mssql-metrics.sh": "mssql"}.get(script, "")
    if not manifest and not match:
        return None
    if not (os.path.isfile(manifest_path) or os.path.isfile(os.path.join(path, "hardware.txt"))):
        return None
    return Run(name=name, tag=tag, host=host or "unknown", path=path, manifest=manifest)


def discover_runs(root):
    """Run directories at `root` (a single run) or up to two levels below it."""
    single = _run_from_directory(root)
    if single is not None:
        return [single]
    runs = []
    for first in sorted(os.listdir(root)):
        first_path = os.path.join(root, first)
        if not os.path.isdir(first_path):
            continue
        run = _run_from_directory(first_path)
        if run is not None:
            runs.append(run)
            continue
        for second in sorted(os.listdir(first_path)):
            second_path = os.path.join(first_path, second)
            if os.path.isdir(second_path):
                run = _run_from_directory(second_path)
                if run is not None:
                    runs.append(run)
    return runs


@contextlib.contextmanager
def open_bundle(path, keep_extracted=False, announce=None, verify=True):
    """Context manager yielding a Bundle.

    `announce(text)` reports a kept directory and a checksum problem. With verify=False
    the sha256 lines of gather_manifest.txt are not checked.
    """
    if not os.path.exists(path):
        raise BundleError("not found: %s" % path)
    temp_dir = None
    try:
        if os.path.isdir(path):
            root = os.path.abspath(path)
        else:
            temp_dir = tempfile.mkdtemp(prefix="perfdiag-")
            lowered = path.lower()
            if lowered.endswith(".zip"):
                extract_zip(path, temp_dir)
            elif lowered.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2", ".tar.xz")):
                extract_tar(path, temp_dir)
            else:
                raise BundleError("unrecognised bundle type: %s (expected .tar.gz, .zip or a directory)" % path)
            root = _single_child(temp_dir)
        opened = Bundle(path, root)
        if verify and opened.gather_manifest:
            opened.verify_checksums()
            note = opened.checksum_note()
            if note:
                opened.notes.append(note)
                if announce:
                    announce(note)
        yield opened
    finally:
        if temp_dir is not None:
            if keep_extracted:
                if announce:
                    announce("extracted files kept in %s" % temp_dir)
            else:
                shutil.rmtree(temp_dir, ignore_errors=True)


def _single_child(directory):
    """A gather bundle holds one top directory; use it as the root."""
    entries = [name for name in os.listdir(directory)]
    if len(entries) == 1 and os.path.isdir(os.path.join(directory, entries[0])):
        return os.path.join(directory, entries[0])
    return directory
