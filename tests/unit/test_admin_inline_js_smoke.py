import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_admin_inline_js_smoke_script_against_live_server():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; run scripts/smoke_admin_inline_js.mjs on the host for JS execution smoke")

    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "smoke_admin_inline_js.mjs"
    env = {
        **os.environ,
        "ADMIN_UI_BASE_URL": os.environ.get("ADMIN_UI_BASE_URL", "http://127.0.0.1:8000"),
        "PROJECT_ROOT": str(project_root),
    }
    result = subprocess.run(
        [node, str(script)],
        cwd=project_root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "admin inline JS smoke passed" in result.stdout
