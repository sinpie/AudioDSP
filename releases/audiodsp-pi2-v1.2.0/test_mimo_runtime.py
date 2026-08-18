#!/usr/bin/env python3
"""Silent isolated validation of the AudioDSP 2x4 MIMO runtime contract."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def write_fir(path: Path, output: int) -> str:
    samples = []
    for index in range(32768):
        left = (0.45 - 0.04 * output) if index == 1024 else 0.0
        right = (0.05 + 0.01 * output) if index == 1024 else 0.0
        samples.append(struct.pack("<ff", left, right))
    payload = b"".join(samples)
    fmt = struct.pack("<HHIIHH", 3, 2, 48000, 48000 * 8, 8, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(payload)) + payload
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path):
    spec = importlib.util.spec_from_file_location("audiodsp_mimo_runtime_test_manager", path)
    require(spec is not None and spec.loader is not None, "manager import failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", type=Path, required=True)
    parser.add_argument("--web", type=Path, required=True)
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--camilladsp", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="audiodsp-mimo-runtime-") as temporary_name:
        root = Path(temporary_name)
        config = root / "config"
        profiles = config / "profiles"
        source = root / "source"
        source.mkdir(parents=True)
        profiles.mkdir(parents=True)
        os.environ.update({
            "AUDIODSP_CONFIG_DIR": str(config),
            "AUDIODSP_STATE_DIR": str(root / "state"),
            "AUDIODSP_LOCK_PATH": str(root / "manager.lock"),
            "AUDIODSP_PREVIEW_STATE_PATH": str(root / "preview.json"),
            "AUDIODSP_SELECTOR_STATE_PATH": str(root / "selector.json"),
            "AUDIODSP_CAMILLADSP": str(args.camilladsp),
            "AUDIODSP_DISABLE_SERVICE_RESTART": "1",
            "AUDIODSP_PLATFORM_CLASS": "test",
            "AUDIODSP_PROFILE_MANAGER": str(args.manager),
            "AUDIODSP_MEASUREMENT": str(args.measurement),
            "AUDIODSP_MEASUREMENT_DIR": str(root / "measurements"),
            "AUDIODSP_CAL_DIR": str(root / "calibration"),
            "AUDIODSP_TARGET_DIR": str(root / "targets"),
            "AUDIODSP_STAGING_DIR": str(root / "staging"),
            "AUDIODSP_RESTORE_STAGING_DIR": str(root / "restore-staging"),
        })
        manager = load(args.manager)
        write_fir(manager.FACTORY_FRONT, 0)
        write_fir(manager.PROFILE_FILES["speaker"]["front"], 0)
        files = []
        for output, name in enumerate(manager.MIMO_OUTPUT_NAMES):
            path = source / name
            digest = write_fir(path, output)
            files.append({"output": output, "label": name.removesuffix("_LR_32768.wav"), "file": name, "sha256": digest, "channels": 2, "frames": 32768, "format": "float32"})
        manifest = {
            "format": "AudioDSP MIMO Bank", "schema_version": 1,
            "sample_rate": 48000, "taps": 32768, "inputs": 2, "outputs": 4,
            "topology": "mimo_one_sub", "files": files,
            "self_validation": {"overall_pass": True, "core_checks": {"synthetic": True}},
        }
        manifest_path = source / "MIMO_manifest.json"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        validated = manager.validate_mimo_bank(manifest_path)
        require(len(validated["files"]) == 4, "bank validation path count")
        installed = manager.install_mimo("speaker", manifest_path)
        require(installed["convolution_channels"] == 8, "install did not select eight paths")
        require(installed["effective_rear_mode"] == "mimo_2x4", "MIMO mode not active")
        generated = manager.CONFIG_PATH.read_text(encoding="utf-8")
        require(generated.count("type: Conv") == 8, "generated config does not contain eight Conv filters")
        require("mimo_expand_2_to_8" in generated and "mimo_sum_8_to_4" in generated, "MIMO mixers missing")
        status = manager.status()
        require(status["mimo"]["speaker"]["valid"], "managed bank status invalid")
        require(status["settings"]["chunksize"] >= 1024, "MIMO chunksize floor missing")
        web = load(args.web)
        backup_body, _backup_name, backup_manifest = web.backup_archive(status)
        require(backup_manifest["schema_version"] == 2, "MIMO backup schema mismatch")
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(backup_body)) as archive:
            names = set(archive.namelist())
            require("profiles/mimo/Speaker_MIMO.json" in names, "MIMO manifest missing from backup")
            require(all(f"profiles/mimo/{name}" in names for name in manager.MIMO_OUTPUT_NAMES), "MIMO WAV missing from backup")
        staged = web.stage_restore_archive(backup_body, "mimo-backup.zip")
        require("Speaker_MIMO.json" in staged["mimo"], "MIMO bank was not validated during restore staging")
        web.discard_restore_staging()
        require(not web.RESTORE_STATE_PATH.exists() and not list(web.RESTORE_STAGING_ROOT.glob("*")), "MIMO backup review left restore staging files")
        manager.set_mimo_enabled("speaker", False, restart=False)
        require(manager.status()["resolved"]["convolution_channels"] == 2, "SISO fallback after MIMO off failed")
        os.environ["AUDIODSP_PLATFORM_CLASS"] = "pi2"
        blocked = False
        try:
            manager.set_mimo_enabled("speaker", True, restart=False)
        except manager.ProfileError:
            blocked = True
        require(blocked, "Pi2 MIMO enable was not blocked")
        print(json.dumps({"result": "PASS", "paths": 8, "taps": 32768, "camilladsp_check": True, "backup_schema_2": True, "pi2_block": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
