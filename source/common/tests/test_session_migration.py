#!/usr/bin/env python3
"""Silent regression for one-session export/import and traversal rejection."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exporter", type=Path, required=True)
    parser.add_argument("--importer", type=Path, required=True)
    args = parser.parse_args()
    importer = load_module(args.importer)

    with tempfile.TemporaryDirectory(prefix="audiodsp-session-migration-") as temporary:
        root = Path(temporary)
        source = root / "source" / "20260820_192126"
        source.mkdir(parents=True)
        state = {
            "session_id": source.name,
            "session_dir": f"/var/lib/audiodsp/measurements/{source.name}",
            "state": "built",
            "result": {"algorithm_revision": "fixture", "self_validation": {"overall_pass": True}},
            "preview_active": True,
            "worker_pid": 123,
        }
        (source / "session.json").write_text(json.dumps(state), encoding="utf-8")
        (source / "session-note.txt").write_text("single session fixture\n", encoding="utf-8")
        (source / "Generated_Front_LR_32768.wav").write_bytes(b"RIFF-session-fixture")
        archive = root / "session.tar.gz"
        subprocess.run(
            ["python", str(args.exporter), "--session-dir", str(source), "--output", str(archive)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        measurements = root / "destination" / "measurements"
        state_root = root / "destination"
        result = importer.import_session(archive, measurements, state_root)
        assert result["status"] == "success" and result["session_id"] == source.name
        current = json.loads((measurements / "current.json").read_text(encoding="utf-8"))
        assert current["session_dir"] == str(measurements / source.name)
        assert current["preview_active"] is False and current["worker_pid"] is None
        assert (measurements / source.name / "Generated_Front_LR_32768.wav").read_bytes() == b"RIFF-session-fixture"

        malicious = root / "malicious.tar.gz"
        manifest = {
            "format": "AudioDSP Session Migration",
            "schema_version": 1,
            "session_id": "20260820_202020",
            "files": {"../escape": {"bytes": 1, "sha256": "0" * 64}},
        }
        encoded = json.dumps(manifest).encode()
        with tarfile.open(malicious, "w:gz") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(encoded)
            tar.addfile(info, io.BytesIO(encoded))
            payload = tarfile.TarInfo("session/20260820_202020/../escape")
            payload.size = 1
            tar.addfile(payload, io.BytesIO(b"x"))
        try:
            importer.import_session(malicious, root / "bad-measurements", root / "bad-state")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe migration archive was accepted")
        assert not (root / "escape").exists()

    print(json.dumps({"result": "PASS", "single_session": True, "traversal_rejected": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
