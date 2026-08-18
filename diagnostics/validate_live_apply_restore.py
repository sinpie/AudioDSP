#!/usr/bin/env python3
"""Exercise generated-FIR preview/apply transactions and always restore live audio.

Run this on the target Pi as root.  The supplied snapshot is restored in a
``finally`` block, followed by the optional pre-existing preview FIR.  The JSON
report is written only after both the transaction checks and final-state checks
have completed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manager(executable: Path, *arguments: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["/usr/bin/python3", str(executable), *arguments],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode:
        raise RuntimeError(
            f"profile manager {' '.join(arguments)} failed "
            f"({completed.returncode}): {completed.stdout.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"profile manager returned invalid JSON: {completed.stdout}") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manager", type=Path, default=Path("/usr/local/bin/audiodsp-profile-manager.py"))
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--front", type=Path, required=True)
    parser.add_argument("--rear", type=Path, required=True)
    parser.add_argument("--expected-original-front-sha", required=True)
    parser.add_argument("--restore-preview-front", type=Path)
    parser.add_argument("--expected-restore-preview-sha")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    generated_front_sha = sha256(args.front)
    generated_rear_sha = sha256(args.rear)
    managed_front = Path("/etc/camilladsp/profiles/Speaker_Front_LR.wav")
    managed_rear = Path("/etc/camilladsp/profiles/Speaker_Rear_LR.wav")
    active_config = Path("/run/camilladsp-active.yml")
    checks: list[dict[str, Any]] = []
    failure: str | None = None

    def record(name: str, condition: bool, detail: Any) -> None:
        checks.append({"name": name, "pass": bool(condition), "detail": detail})
        require(condition, f"{name}: {detail}")

    try:
        manager(args.manager, "restore-profile")
        initial = manager(args.manager, "status")
        record(
            "initial_managed_front_is_original",
            sha256(managed_front) == args.expected_original_front_sha,
            sha256(managed_front),
        )

        preview = manager(args.manager, "preview-pair", "speaker", str(args.front), str(args.rear))
        preview_status = manager(args.manager, "status")
        record("preview_front_sha", preview_status["preview"]["front"]["sha256"] == generated_front_sha, preview_status["preview"]["front"]["sha256"])
        record("preview_rear_sha", preview_status["preview"]["rear"]["sha256"] == generated_rear_sha, preview_status["preview"]["rear"]["sha256"])
        record("preview_did_not_overwrite_managed_front", sha256(managed_front) == args.expected_original_front_sha, sha256(managed_front))
        record("preview_config_uses_generated_front", str(args.front) in active_config.read_text(encoding="utf-8"), str(args.front))

        manager(args.manager, "restore-profile")
        restored = manager(args.manager, "status")
        record("preview_restore_clears_preview", not restored["preview"]["active"], restored["preview"])
        record("preview_restore_returns_to_original", sha256(managed_front) == args.expected_original_front_sha, sha256(managed_front))

        applied = manager(
            args.manager,
            "install-pair",
            "speaker",
            str(args.front),
            str(args.rear),
            "--woofer-trim",
            "0",
        )
        applied_status = manager(args.manager, "status")
        record("apply_overwrites_front_with_generated_fir", sha256(managed_front) == generated_front_sha, sha256(managed_front))
        record("apply_installs_generated_rear", managed_rear.is_file() and sha256(managed_rear) == generated_rear_sha, sha256(managed_rear) if managed_rear.is_file() else None)
        record("apply_status_reports_speaker", applied_status["settings"]["requested_profile"] == "speaker", applied_status["settings"]["requested_profile"])
        record("apply_response_completed", isinstance(applied, dict) and bool(applied), sorted(applied))
    except Exception as exc:  # The final restore below is mandatory even on failure.
        failure = str(exc)
    finally:
        restore_error: str | None = None
        try:
            manager(args.manager, "restore-profile")
            manager(args.manager, "restore-snapshot", str(args.snapshot))
            if args.restore_preview_front:
                manager(args.manager, "preview-pair", "speaker", str(args.restore_preview_front))
        except Exception as exc:
            restore_error = str(exc)

        try:
            final_status = manager(args.manager, "status")
            final_front_sha = sha256(managed_front)
            final_config = active_config.read_text(encoding="utf-8")
            checks.extend(
                [
                    {"name": "final_managed_front_is_original", "pass": final_front_sha == args.expected_original_front_sha, "detail": final_front_sha},
                    {"name": "final_profile_is_speaker", "pass": final_status["settings"]["requested_profile"] == "speaker", "detail": final_status["settings"]["requested_profile"]},
                    {"name": "final_chunk_is_2048", "pass": final_status["settings"]["chunksize"] == 2048, "detail": final_status["settings"]["chunksize"]},
                    {"name": "final_volume_is_minus_10_db", "pass": final_status["settings"]["output_volume_db"] == -10, "detail": final_status["settings"]["output_volume_db"]},
                ]
            )
            if args.restore_preview_front:
                preview_sha = final_status.get("preview", {}).get("front", {}).get("sha256")
                checks.extend(
                    [
                        {"name": "final_preview_is_active", "pass": final_status["preview"]["active"], "detail": final_status["preview"]},
                        {"name": "final_preview_sha", "pass": preview_sha == args.expected_restore_preview_sha, "detail": preview_sha},
                        {"name": "final_config_uses_restored_preview", "pass": str(args.restore_preview_front) in final_config, "detail": str(args.restore_preview_front)},
                    ]
                )
        except Exception as exc:
            final_status = None
            checks.append({"name": "final_state_readable", "pass": False, "detail": str(exc)})

        if restore_error:
            checks.append({"name": "mandatory_restore", "pass": False, "detail": restore_error})
        else:
            checks.append({"name": "mandatory_restore", "pass": True, "detail": str(args.snapshot)})

    failed_checks = [check["name"] for check in checks if not check["pass"]]
    report = {
        "result": "PASS" if not failure and not failed_checks else "FAIL",
        "timestamp_unix": time.time(),
        "generated": {"front_sha256": generated_front_sha, "rear_sha256": generated_rear_sha},
        "failure": failure,
        "failed_checks": failed_checks,
        "checks": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
