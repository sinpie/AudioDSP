#!/usr/bin/env python3
"""Assemble deterministic Pi bundles from one canonical AudioDSP source tree."""

from __future__ import annotations

import argparse
import filecmp
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
COMMON_PAYLOAD = SOURCE / "common" / "payload"
COMMON_TESTS = SOURCE / "common" / "tests"
BUILD = ROOT / "build"


def load_platform(platform: str) -> tuple[dict[str, object], Path, Path]:
    manifest_path = SOURCE / "platforms" / platform / "platform.json"
    if not manifest_path.is_file():
        raise SystemExit(f"unknown platform: {platform}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    inherited = str(manifest.get("inherits", platform))
    overlay = SOURCE / "platforms" / inherited / "payload"
    release = ROOT / str(manifest["release"])
    return manifest, overlay, release


def expected_files(platform: str) -> tuple[Path, dict[Path, Path]]:
    _manifest, overlay, release = load_platform(platform)
    output = BUILD / platform
    expected: dict[Path, Path] = {}
    for source in sorted(COMMON_PAYLOAD.iterdir()):
        if source.is_file():
            expected[output / "payload" / source.name] = source
    for source in sorted(overlay.iterdir()):
        if source.is_file():
            expected[output / "payload" / source.name] = source
    for source in sorted(COMMON_TESTS.iterdir()):
        if source.is_file():
            expected[output / source.name] = source

    # CamillaDSP is architecture-specific and too large for Git. Keep the
    # downloaded binary beside each release writer, then include it at build time.
    binary = release / "payload" / "camilladsp"
    expected[output / "payload" / "camilladsp"] = binary
    return output, expected


def safe_unlink(path: Path, output: Path) -> None:
    resolved = path.resolve()
    root = output.resolve()
    if resolved.parent != root and resolved.parent != (root / "payload"):
        raise SystemExit(f"refusing to remove file outside build bundle: {resolved}")
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"refusing to remove unexpected build entry: {resolved}")
    path.unlink()


def assemble(platform: str) -> int:
    output, expected = expected_files(platform)
    output.mkdir(parents=True, exist_ok=True)
    (output / "payload").mkdir(parents=True, exist_ok=True)

    missing = [source for source in expected.values() if not source.is_file()]
    if missing:
        for source in missing:
            print(f"MISSING {source.relative_to(ROOT)}", file=sys.stderr)
        return 2

    expected_destinations = set(expected)
    existing = [path for path in output.iterdir() if path.is_file()]
    existing += [path for path in (output / "payload").iterdir() if path.is_file()]
    stale = [path for path in existing if path.name != "bundle-manifest.json" and path not in expected_destinations]
    for path in stale:
        safe_unlink(path, output)

    changed = 0
    for destination, source in expected.items():
        if not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            changed += 1

    manifest = {
        "platform": platform,
        "source_files": len(expected),
        "payload_files": sum(destination.parent.name == "payload" for destination in expected),
        "test_files": sum(destination.parent == output for destination in expected),
    }
    manifest_path = output / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"ASSEMBLED {platform}: {len(expected)} files; {changed} copied; {len(stale)} stale removed")
    return 0


def check(platform: str) -> int:
    output, expected = expected_files(platform)
    different: list[tuple[Path, Path]] = []
    for destination, source in expected.items():
        if not source.is_file() or not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
            different.append((source, destination))
    expected_destinations = set(expected)
    stale: list[Path] = []
    if output.is_dir():
        stale.extend(path for path in output.iterdir() if path.is_file() and path.name != "bundle-manifest.json" and path not in expected_destinations)
        payload = output / "payload"
        if payload.is_dir():
            stale.extend(path for path in payload.iterdir() if path.is_file() and path not in expected_destinations)
    for source, destination in different:
        print(f"DIVERGED {destination.relative_to(ROOT)} <- {source.relative_to(ROOT)}")
    for path in stale:
        print(f"STALE {path.relative_to(ROOT)}")
    if different or stale:
        return 1
    print(f"OK {platform}: {len(expected)} assembled files match canonical source")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("pi2", "pi3", "pi4", "pi5"), required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--assemble", action="store_true")
    action.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return assemble(args.platform) if args.assemble else check(args.platform)


if __name__ == "__main__":
    sys.exit(main())
