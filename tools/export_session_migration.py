#!/usr/bin/env python3
"""Create a single-session AudioDSP migration archive with a hash manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


SESSION_ID = re.compile(r"^[0-9]{8}_[0-9]{6}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    session_dir = args.session_dir.resolve(strict=True)
    session_id = session_dir.name
    if not SESSION_ID.fullmatch(session_id) or session_dir.is_symlink():
        raise SystemExit("session directory must be a regular YYYYMMDD_HHMMSS directory")
    state_path = session_dir / "session.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or str(state.get("session_id")) != session_id:
        raise SystemExit("session.json session_id does not match the directory")
    if Path(str(state.get("session_dir", ""))).name != session_id:
        raise SystemExit("session.json session_dir does not match the directory")

    files: list[tuple[str, Path]] = []
    for path in sorted(session_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            if path.is_dir() and not path.is_symlink():
                continue
            raise SystemExit(f"unsupported session entry: {path}")
        relative = path.relative_to(session_dir).as_posix()
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise SystemExit(f"unsafe session path: {relative}")
        files.append((relative, path))
    if not files or "session.json" not in {name for name, _path in files}:
        raise SystemExit("session archive is empty or missing session.json")

    manifest = {
        "format": "AudioDSP Session Migration",
        "schema_version": 1,
        "session_id": session_id,
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_state": str(state.get("state", "unknown")),
        "files": {
            name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
            for name, path in files
        },
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing archive: {args.output}")
    with tarfile.open(args.output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mode = 0o644
        info.mtime = 0
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for relative, path in files:
            archive.add(path, arcname=f"session/{session_id}/{relative}", recursive=False)
    print(json.dumps({
        "result": "PASS",
        "session_id": session_id,
        "files": len(files),
        "uncompressed_bytes": sum(path.stat().st_size for _name, path in files),
        "archive_bytes": args.output.stat().st_size,
        "archive_sha256": sha256(args.output),
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
