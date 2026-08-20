#!/usr/bin/env python3
"""Verify and atomically import one AudioDSP room-tuning session archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import time


SESSION_ID = re.compile(r"^[0-9]{8}_[0-9]{6}$")
MAX_ARCHIVE_BYTES = 1 << 30
MAX_FILES = 256


def import_session(archive_path: Path, measurement_root: Path, state_root: Path) -> dict:
    archive_path = archive_path.resolve(strict=True)
    if archive_path.is_symlink() or not archive_path.is_file() or archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("session migration archive is not a safe regular file")
    measurement_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        by_name = {member.name: member for member in members}
        if len(by_name) != len(members) or "manifest.json" not in by_name:
            raise ValueError("session migration has duplicate paths or no manifest")
        manifest_member = by_name["manifest.json"]
        if not manifest_member.isfile() or manifest_member.size > 1024 * 1024:
            raise ValueError("session migration manifest is invalid")
        manifest_source = archive.extractfile(manifest_member)
        if manifest_source is None:
            raise ValueError("session migration manifest cannot be read")
        manifest = json.load(manifest_source)
        session_id = str(manifest.get("session_id", ""))
        files = manifest.get("files")
        if manifest.get("format") != "AudioDSP Session Migration" or manifest.get("schema_version") != 1:
            raise ValueError("unsupported session migration format")
        if not SESSION_ID.fullmatch(session_id) or not isinstance(files, dict) or not 0 < len(files) <= MAX_FILES:
            raise ValueError("session migration identity or file count is invalid")
        expected = {"manifest.json"} | {f"session/{session_id}/{name}" for name in files}
        if set(by_name) != expected or any(not member.isfile() for member in members):
            raise ValueError("session migration contains unexpected, missing, or non-regular files")

        destination = measurement_root / session_id
        if destination.exists() or destination.is_symlink():
            raise ValueError("destination session already exists")
        staging = Path(tempfile.mkdtemp(prefix=f".migration-{session_id}-", dir=measurement_root))
        try:
            total_bytes = 0
            for relative, expected_meta in sorted(files.items()):
                pure = PurePosixPath(relative)
                if pure.is_absolute() or ".." in pure.parts or not pure.parts or not isinstance(expected_meta, dict):
                    raise ValueError(f"unsafe migration path or metadata: {relative}")
                member = by_name[f"session/{session_id}/{relative}"]
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"cannot read migration file: {relative}")
                target = staging.joinpath(*pure.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with target.open("xb") as output:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)
                        digest.update(block)
                        size += len(block)
                        total_bytes += len(block)
                        if total_bytes > MAX_ARCHIVE_BYTES:
                            raise ValueError("session migration expands beyond the 1 GiB safety limit")
                if size != int(expected_meta.get("bytes", -1)) or digest.hexdigest() != expected_meta.get("sha256"):
                    raise ValueError(f"migration hash/size mismatch: {relative}")
                target.chmod(0o644)

            state_path = staging / "session.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or str(state.get("session_id")) != session_id:
                raise ValueError("migrated session.json identity mismatch")
            state.update({
                "session_dir": str(destination),
                "preview_active": False,
                "preview_profile": None,
                "applied_profile": None,
                "worker_pid": None,
                "active_pids": [],
                "cancel_requested": False,
                "dsp_mode": "restored",
                "migration": {"source": "single-session SD migration", "imported_unix": time.time()},
            })
            encoded = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            state_path.write_bytes(encoded)
            state_path.chmod(0o644)
            os.replace(staging, destination)
            current_tmp = measurement_root / ".current.json.migration"
            current_tmp.write_bytes(encoded)
            current_tmp.chmod(0o644)
            os.replace(current_tmp, measurement_root / "current.json")
            record = {"status": "success", "session_id": session_id, "files": len(files), "bytes": total_bytes}
            record_tmp = state_root / ".session-migration.json.tmp"
            record_tmp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            os.replace(record_tmp, state_root / "session-migration.json")
            return record
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--measurement-root", type=Path, default=Path("/var/lib/audiodsp/measurements"))
    parser.add_argument("--state-root", type=Path, default=Path("/var/lib/audiodsp"))
    args = parser.parse_args()
    print(json.dumps(import_session(args.archive, args.measurement_root, args.state_root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
