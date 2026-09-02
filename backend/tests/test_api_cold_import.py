from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_api_imports_in_a_fresh_python_process() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.main import app; print(app.title)",
        ],
        cwd=backend_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("LiveCaption Studio API")
