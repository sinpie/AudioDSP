#!/usr/bin/env python3
"""Record one pre-convolved option-validation WAV through U7 and UMIK-1.

The production measurement engine owns the exclusive audio lock, stops and
restores CamillaDSP, disables the U7 Mic/Line capture switches, and records the
UMIK directly.  This wrapper deliberately reuses that proven bypass path.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct


def load_engine(path: Path):
    specification = importlib.util.spec_from_file_location("audiodsp_measurement_capture", path)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load measurement engine: {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def validate_playback(path: Path) -> dict[str, int]:
    with path.open("rb") as handle:
        header = handle.read(44)
    if len(header) != 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise RuntimeError("validation playback is not a canonical WAV")
    code, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", header, 20)
    if (code, channels, rate, block_align, bits) != (1, 4, 48_000, 12, 24):
        raise RuntimeError(
            f"validation playback must be PCM24/48k/4ch: "
            f"code={code}, channels={channels}, rate={rate}, block={block_align}, bits={bits}"
        )
    if byte_rate != rate * block_align:
        raise RuntimeError("validation playback has an invalid byte rate")
    data_bytes = struct.unpack_from("<I", header, 40)[0]
    frames = data_bytes // block_align
    return {"rate": rate, "channels": channels, "bits": bits, "frames": frames}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, default=Path("/usr/local/bin/audiodsp-measurement.py"))
    parser.add_argument("--playback", type=Path, required=True)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--label", default="전체 옵션 저음량 확인")
    parser.add_argument("--record-via-tmpfs", action="store_true", help="record to /dev/shm first to avoid SD-card write stalls during long validation")
    args = parser.parse_args()
    if os.name == "posix" and os.geteuid() != 0:
        raise RuntimeError("run this capture wrapper as root")
    metadata = validate_playback(args.playback)
    args.recording.parent.mkdir(parents=True, exist_ok=True)
    engine = load_engine(args.engine)
    previous = engine.load_current()
    restore_fields = {
        key: previous.get(key)
        for key in ("stage", "progress", "eta_seconds", "dsp_mode", "u7_input", "active_pids")
    }
    capture_path = args.recording
    if args.record_via_tmpfs:
        shared_memory = Path("/dev/shm")
        if not shared_memory.is_dir():
            raise RuntimeError("--record-via-tmpfs requested but /dev/shm is unavailable")
        required_bytes = (metadata["frames"] + 2 * metadata["rate"]) * 3 + 44
        if shutil.disk_usage(shared_memory).free < required_bytes + 32 * 1024 * 1024:
            raise RuntimeError("/dev/shm does not have enough free space for the validation recording")
        capture_path = shared_memory / f"audiodsp-option-validation-{os.getpid()}.wav"
    try:
        engine.run_direct_capture_batch([(args.playback, capture_path, args.label)], 0.0, 100.0)
        if capture_path != args.recording:
            shutil.copy2(capture_path, args.recording)
    finally:
        if capture_path != args.recording:
            capture_path.unlink(missing_ok=True)
        engine.update_current(**restore_fields)
    if not args.recording.is_file() or args.recording.stat().st_size <= 44:
        raise RuntimeError("UMIK recording was not created")
    print(json.dumps({
        "playback": str(args.playback),
        "recording": str(args.recording),
        "recorded_via_tmpfs": bool(args.record_via_tmpfs),
        "playback_format": metadata,
        "recording_bytes": args.recording.stat().st_size,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
