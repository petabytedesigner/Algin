"""Category 28: offline, copy-only, verifiable evidence snapshots.

Never rotate, truncate, repair or restore an active journal/log. In particular,
CSV event IDs must remain available to the existing idempotent projector.
"""
from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import stat
import zlib

from storage.decision_trace import DecisionTrace, MAX_BYTES as TRACE_LIMIT, TraceError
from storage.event_log import AuditError, EventLog, MAX_BYTES as EVENT_LIMIT
from utils.time_tools import parse_utc


class ArchiveError(RuntimeError):
    """Cannot prove complete, durable archive evidence."""


SOURCES = ("decision_trace.jsonl", "events.csv")
LIMITS = {"decision_trace.jsonl": TRACE_LIMIT, "events.csv": EVENT_LIMIT}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _secure_directory(directory):
    if directory.is_symlink():
        raise ArchiveError("Symlink directory forbidden")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)


def _read_regular(path, limit):
    if path.is_symlink():
        raise ArchiveError("Symlink evidence forbidden")
    flags = os.O_RDONLY | (getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(os.open(str(path), flags), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ArchiveError("Non-regular evidence file")
            raw = handle.read(limit + 1)
        if not raw or len(raw) > limit:
            raise ArchiveError("Missing or oversized evidence")
        return raw
    except OSError as exc:
        raise ArchiveError("Cannot read evidence") from exc


def _write_new(path, data):
    """Write a new immutable-named file; never overwrite prior evidence."""
    if path.exists() or path.is_symlink():
        raise ArchiveError("Archive destination already exists")
    tmp = path.with_name(".tmp-" + secrets.token_hex(18))
    try:
        with os.fdopen(os.open(str(tmp), os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0o600),
                       "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # The archive lock prevents two cooperative publishers racing.
        if path.exists() or path.is_symlink():
            raise ArchiveError("Archive destination replaced")
        os.replace(tmp, path)
        if hasattr(os, "O_DIRECTORY"):
            fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def _locks(paths):
    """Refuse busy/stale writer locks; release only locks still owned by us."""
    owned = []
    try:
        for path in sorted(paths):
            if path.is_symlink():
                raise ArchiveError("Lock symlink forbidden")
            token = secrets.token_hex(24)
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except OSError as exc:
                raise ArchiveError("Archive or log writer already active") from exc
            info = os.fstat(fd)
            owned.append((path, token, info.st_dev, info.st_ino))
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
        yield
    finally:
        for path, token, device, inode in reversed(owned):
            try:
                info = path.lstat()
                if (path.is_symlink() or info.st_dev != device or info.st_ino != inode
                        or path.read_text(encoding="ascii") != token):
                    raise ArchiveError("Archive lock ownership changed")
                path.unlink()
            except OSError as exc:
                raise ArchiveError("Unable to release archive lock") from exc


class EvidenceArchive:
    """Content-addressed compressed copies, verified manifests, no deletions."""

    def __init__(self, source_dir="csv", archive_dir=".local/audit-archives"):
        self.source = Path(source_dir)
        self.archive = Path(archive_dir)

    def _manifest_paths(self):
        if self.archive.is_symlink():
            raise ArchiveError("Symlink archive directory forbidden")
        if not self.archive.exists():
            return []
        return sorted(self.archive.glob("snapshot-*.json"))

    def verify(self):
        """Verify EVERY published snapshot before allowing a new one."""
        verified = []
        verified_blobs = {}
        for manifest_path in self._manifest_paths():
            raw = _read_regular(manifest_path, 16 * 1024)
            try:
                manifest = json.loads(raw)
                if (set(manifest) != {"version", "snapshot_id", "captured_at",
                                     "sources", "previous", "checksum"}
                        or manifest["version"] != 1
                        or not isinstance(manifest["sources"], list)
                        or not manifest["sources"]):
                    raise ArchiveError("Malformed archive manifest")
                description = []
                seen = set()
                for entry in manifest["sources"]:
                    if (not isinstance(entry, dict)
                        or set(entry) != {"name", "sha256", "size", "records", "blob"}
                        or entry["name"] not in SOURCES or entry["name"] in seen):
                        raise ArchiveError("Invalid archived source")
                    seen.add(entry["name"])
                    name, digest = entry["name"], entry["sha256"]
                    if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)
                        or entry["blob"] != name + "." + digest + ".gz"):
                        raise ArchiveError("Invalid archive blob identity")
                    blob = self.archive / entry["blob"]
                    if (not isinstance(entry["records"], int)
                            or isinstance(entry["records"], bool)
                            or entry["records"] < 1):
                        raise ArchiveError("Invalid archive record count")
                    if entry["blob"] in verified_blobs:
                        if verified_blobs[entry["blob"]] != (
                            entry["size"], entry["records"]
                        ):
                            raise ArchiveError("Conflicting reused archive blob")
                    else:
                        compressed = _read_regular(blob, LIMITS[name] + 1024 * 1024)
                        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                            evidence = stream.read(LIMITS[name] + 1)
                            if stream.read(1):
                                raise ArchiveError("Archive decompression exceeded size")
                        if len(evidence) != entry["size"] or _hash(evidence) != digest:
                            raise ArchiveError("Archived evidence digest mismatch")
                        verified_blobs[entry["blob"]] = (
                            entry["size"], entry["records"]
                        )
                    description.append(entry)
                if description != sorted(description, key=lambda v: v["name"]):
                    raise ArchiveError("Noncanonical archive source order")
                expected = "archive-" + _hash(_canonical(description))[:40]
                if (manifest["snapshot_id"] != expected
                        or manifest_path.name != "snapshot-" + expected + ".json"):
                    raise ArchiveError("Archive manifest identity mismatch")
                if (not isinstance(manifest["captured_at"], str)
                        or not manifest["captured_at"].endswith("Z")):
                    raise ArchiveError("Invalid archive capture timestamp")
                parse_utc(manifest["captured_at"])
                previous = manifest["previous"]
                if previous is not None and (not isinstance(previous, str)
                    or not previous.startswith("archive-") or len(previous) != 48):
                    raise ArchiveError("Invalid previous snapshot identity")
                if manifest["checksum"] != _hash(_canonical({
                    key: value for key, value in manifest.items() if key != "checksum"
                })):
                    raise ArchiveError("Archive manifest checksum mismatch")
                verified.append(manifest)
            except (OSError, ValueError, TypeError, KeyError, gzip.BadGzipFile,
                    EOFError, UnicodeError, zlib.error) as exc:
                raise ArchiveError("Corrupt archive snapshot") from exc
        # Link manifests so removing/replacing an *intermediate* snapshot is
        # detected. The first and last snapshot still need external backups.
        if verified:
            by_id = {m["snapshot_id"]: m for m in verified}
            if len(by_id) != len(verified):
                raise ArchiveError("Duplicate archive snapshot identity")
            starts = [m for m in verified if m["previous"] is None]
            if len(starts) != 1:
                raise ArchiveError("Missing or multiple archive chain roots")
            ordered = [starts[0]]
            visited = {starts[0]["snapshot_id"]}
            while len(ordered) < len(verified):
                next_steps = [m for m in verified
                              if m["previous"] == ordered[-1]["snapshot_id"]]
                if len(next_steps) != 1 or next_steps[0]["snapshot_id"] in visited:
                    raise ArchiveError("Broken/forked archive manifest chain")
                ordered.append(next_steps[0])
                visited.add(next_steps[0]["snapshot_id"])
            verified = ordered
        return tuple(verified)

    def snapshot(self):
        """Copy verified logs while both writers are locked; never rotate."""
        if not self.source.is_dir() or self.source.is_symlink():
            raise ArchiveError("Audit source directory unavailable")
        _secure_directory(self.archive)
        with _locks((self.archive / "archive.lock", self.source / "events.lock",
                     self.source / "decision_trace.lock")):
            previous = self.verify()
            originals = {}
            descriptions = []
            for name in SOURCES:
                path = self.source / name
                if not path.exists() and not path.is_symlink():
                    continue
                if name == "events.csv":
                    count = len(EventLog(self.source)._existing())
                else:
                    count = len(DecisionTrace(self.source).read_all())
                if count == 0:
                    raise ArchiveError("Empty validated evidence")
                raw = _read_regular(path, LIMITS[name])
                digest = _hash(raw)
                originals[name] = raw
                descriptions.append({
                    "name": name, "sha256": digest, "size": len(raw),
                    "records": count, "blob": name + "." + digest + ".gz",
                })
            if not descriptions:
                raise ArchiveError("No audit evidence to archive")
            descriptions.sort(key=lambda v: v["name"])
            snapshot_id = "archive-" + _hash(_canonical(descriptions))[:40]
            for existing in previous:
                if existing["snapshot_id"] == snapshot_id:
                    return existing
            known_blobs = {entry["blob"] for manifest in previous
                           for entry in manifest["sources"]}
            for entry in descriptions:
                blob = self.archive / entry["blob"]
                if blob.exists() or blob.is_symlink():
                    if blob.is_symlink() or entry["blob"] not in known_blobs:
                        raise ArchiveError("Unindexed archive blob collision")
                    continue  # Already verified under the archive lock.
                _write_new(blob, gzip.compress(originals[entry["name"]], mtime=0))
            manifest = {
                "version": 1, "snapshot_id": snapshot_id,
                "previous": previous[-1]["snapshot_id"] if previous else None,
                "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
                    "+00:00", "Z"),
                "sources": descriptions,
            }
            manifest["checksum"] = _hash(_canonical(manifest))
            _write_new(self.archive / ("snapshot-" + snapshot_id + ".json"),
                       _canonical(manifest) + b"\n")
            return manifest

    def find(self, *, cycle_id=None, signal_id=None):
        """Read archived observations for incident review; never use for orders."""
        if not cycle_id and not signal_id:
            raise ArchiveError("A cycle_id or signal_id is required")
        verified = self.verify()
        results = {"events": [], "decisions": []}
        seen = {"events": {}, "decisions": {}}
        for manifest in verified:
            for entry in manifest["sources"]:
                with gzip.open(self.archive / entry["blob"], "rt", encoding="utf-8",
                               newline="") as stream:
                    if entry["name"] == "events.csv":
                        records = csv.DictReader(stream)
                        kind, key = "events", "event_id"
                    else:
                        records = (json.loads(line) for line in stream)
                        kind, key = "decisions", "trace_id"
                    for row in records:
                        if key in row and row[key] in seen[kind]:
                            if seen[kind][row[key]] != row:
                                raise ArchiveError("Conflicting archived event identity")
                            continue
                        if ((cycle_id is None or row.get("correlation_id", row.get(
                             "cycle_id")) == cycle_id)
                            and (signal_id is None or row.get("signal_id") == signal_id)):
                            results[kind].append(row)
                        if key in row:
                            seen[kind][row[key]] = row
        return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline, read-only source audit archive")
    parser.add_argument("action", choices=("snapshot", "verify", "find"))
    parser.add_argument("--source-dir", default="csv")
    parser.add_argument("--archive-dir", default=".local/audit-archives")
    parser.add_argument("--cycle-id")
    parser.add_argument("--signal-id")
    args = parser.parse_args(argv)
    archive = EvidenceArchive(args.source_dir, args.archive_dir)
    try:
        if args.action == "snapshot":
            result = archive.snapshot()
        elif args.action == "verify":
            result = {"verified_snapshots": len(archive.verify())}
        else:
            result = archive.find(cycle_id=args.cycle_id, signal_id=args.signal_id)
    except (ArchiveError, AuditError, TraceError) as exc:
        parser.exit(3, "AUDIT_ARCHIVE_BLOCKED: " + type(exc).__name__ + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
