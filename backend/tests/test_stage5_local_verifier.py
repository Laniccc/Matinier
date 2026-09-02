from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_stage5_local_verifier_preserves_source_and_rejects_invalid_output() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "verify_stage5_local.py"),
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
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {
        "status": "ok",
        "versions": [2, 1],
        "artifact_count": 2,
        "source_segment_count": 2,
        "referenced_source_ids": ["seg-1", "seg-2"],
        "source_rows_unchanged": True,
        "source_exports_unchanged": True,
        "invalid_output_rejected": True,
        "provider_call_count": 4,
        "clean_shutdown": True,
    }
