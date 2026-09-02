from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_stage6_fake_asr_pipeline_is_durable_exported_and_isolated() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "verify_stage6_local.py"),
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
        "session_count": 2,
        "state_sequence": [
            "running",
            "running",
            "finalizing",
            "completed",
        ],
        "published_revisions": [1, 2, 3],
        "durable_final_count": 2,
        "draft_row_count": 0,
        "json_export_segment_counts": [1, 1],
        "runs_isolated": True,
        "providers_closed": True,
        "clean_shutdown": True,
    }
