from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_stage2_local_verifier_reports_protocol_flow_and_clean_shutdown() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "verify_stage2_local.py"),
            "--file",
            str(PROJECT_ROOT / "backend" / "tests" / "fixtures" / "demo_audio.wav"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output_lines = completed.stdout.strip().splitlines()
    assert output_lines
    result = json.loads(output_lines[-1])
    assert result == {
        "status": "ok",
        "frame_count": 50,
        "source_audio_bytes": 32_000,
        "sent_audio_chunk_count": 10,
        "sent_audio_bytes": 32_000,
        "partial_event_count": 1,
        "final_event_count": 1,
        "provider_error_count": 0,
        "clean_shutdown": True,
    }
