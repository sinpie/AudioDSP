#!/usr/bin/env python3
"""Merge a selectively regenerated AudioDSP option matrix into a full matrix.

The base and overlay manifests are treated as immutable evidence.  Every copied
FIR is checked against its manifest SHA before and after the merge, and the
result records which variants were regenerated versus byte-identically reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import time
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / "manifest.json"
    if not path.is_file():
        raise RuntimeError(f"manifest is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value.get("variants"), list):
        raise RuntimeError(f"manifest has no variants: {path}")
    return value


def validate_entry(directory: Path, entry: dict[str, Any]) -> None:
    if entry.get("status") != "PASS":
        raise RuntimeError(f"variant is not PASS: {entry.get('id')}")
    variant_dir = directory / "filters" / entry["id"]
    for name, key in (("Front_LR_32768.wav", "front_sha256"), ("Woofer_LR_32768.wav", "rear_sha256")):
        path = variant_dir / name
        if not path.is_file() or sha256(path) != entry.get(key):
            raise RuntimeError(f"FIR SHA mismatch: {entry.get('id')} / {name}")
    for name in ("Room_Tuning_Report.json", "Room_Tuning_Report.md", "result-state.json"):
        if not (variant_dir / name).is_file():
            raise RuntimeError(f"variant artifact is missing: {entry.get('id')} / {name}")


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--baseline-proof", type=Path, required=True, help="selective matrix built by the new engine and containing baseline")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    if args.output.exists():
        raise RuntimeError(f"output already exists: {args.output}")
    base = load_manifest(args.base)
    overlay = load_manifest(args.overlay)
    baseline_proof = load_manifest(args.baseline_proof)
    base_by_id = {entry["id"]: entry for entry in base["variants"]}
    overlay_by_id = {entry["id"]: entry for entry in overlay["variants"]}
    proof_by_id = {entry["id"]: entry for entry in baseline_proof["variants"]}
    if len(base_by_id) != 67 or base.get("passed") != 67 or base.get("failed") != 0:
        raise RuntimeError("base is not a complete passing 67-variant matrix")
    if not overlay_by_id or not set(overlay_by_id) <= set(base_by_id):
        raise RuntimeError("overlay contains no variants or unknown variant IDs")
    if "baseline" not in proof_by_id:
        raise RuntimeError("baseline proof does not contain baseline")
    for entry in base_by_id.values():
        validate_entry(args.base, entry)
    for entry in overlay_by_id.values():
        validate_entry(args.overlay, entry)
    validate_entry(args.baseline_proof, proof_by_id["baseline"])
    baseline_fir_sha_unchanged = all(
        base_by_id["baseline"].get(key) == proof_by_id["baseline"].get(key)
        for key in ("front_sha256", "rear_sha256")
    )
    if not baseline_fir_sha_unchanged:
        raise RuntimeError("new-engine baseline FIR differs; selective reuse is unsafe")

    shutil.copytree(args.base, args.output, ignore=shutil.ignore_patterns("work"))
    for variant_id in overlay_by_id:
        destination = args.output / "filters" / variant_id
        shutil.rmtree(destination)
        shutil.copytree(args.overlay / "filters" / variant_id, destination)
    baseline_destination = args.output / "filters" / "baseline"
    shutil.rmtree(baseline_destination)
    shutil.copytree(args.baseline_proof / "filters" / "baseline", baseline_destination)

    merged_entries: list[dict[str, Any]] = []
    regenerated_ids = set(overlay_by_id) | {"baseline"}
    for original in base["variants"]:
        variant_id = original["id"]
        source = proof_by_id["baseline"] if variant_id == "baseline" else overlay_by_id.get(variant_id, original)
        entry = dict(source)
        entry["provenance"] = "regenerated" if variant_id in regenerated_ids else "reused-byte-identical"
        merged_entries.append(entry)

    finished = time.time()
    manifest = dict(base)
    manifest.update({
        "schema": 2,
        "started_unix": min(base.get("started_unix", finished), overlay.get("started_unix", finished)),
        "finished_unix": finished,
        "updated_unix": finished,
        "variants": merged_entries,
        "completed": 67,
        "passed": 67,
        "failed": 0,
        "total": 67,
        "full_matrix_total": 67,
        "engine_sha256": sha256(args.engine),
        "merge": {
            "base": str(args.base),
            "overlay": str(args.overlay),
            "baseline_proof": str(args.baseline_proof),
            "regenerated_variant_ids": sorted(regenerated_ids),
            "reused_variant_ids": sorted(set(base_by_id) - regenerated_ids),
            "reason": args.reason,
            "baseline_fir_sha_unchanged": baseline_fir_sha_unchanged,
        },
        "unique_front_sha256": len({entry["front_sha256"] for entry in merged_entries}),
        "unique_rear_sha256": len({entry["rear_sha256"] for entry in merged_entries}),
    })
    for entry in merged_entries:
        validate_entry(args.output, entry)
    atomic_json(args.output / "manifest.json", manifest)
    atomic_json(args.output / "progress.json", manifest)
    print(json.dumps({
        "output": str(args.output),
        "variants": len(merged_entries),
        "regenerated": len(regenerated_ids),
        "reused": len(base_by_id) - len(regenerated_ids),
        "engine_sha256": manifest["engine_sha256"],
        "unique_front_sha256": manifest["unique_front_sha256"],
        "unique_rear_sha256": manifest["unique_rear_sha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
