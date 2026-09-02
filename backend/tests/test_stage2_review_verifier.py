from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_stage2_review_verifier_closes_human_approval_chain() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "verify_stage2_review.py"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output_lines = completed.stdout.strip().splitlines()
    assert output_lines
    result = json.loads(output_lines[-1])
    assert result["status"] == "ok"
    assert result["package_versions"] == [1, 2]
    assert result["artifact_identity"] == "clean_script"
    assert result["artifact_versions"] == [2, 1]
    assert result["parent_chain_valid"] is True
    assert result["old_approved_superseded"] is True
    assert result["approved_unique"] is True
    assert result["cross_package_isolated"] is True
    assert result["old_package_artifact_count"] == 2
    assert result["old_package_hash_unchanged"] is True
    assert result["stage1_final_unchanged"] is True
    assert result["json_export_bytes"] > 0
    assert result["cloud_calls"] == 0
