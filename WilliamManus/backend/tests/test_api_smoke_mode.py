import os
import subprocess
import sys
from pathlib import Path


def test_api_import_smoke_mode_exposes_health_route():
    env = {
        **os.environ,
        "ROYS_ALPHA_BACKEND_SMOKE_MODE": "true",
        "JWT_SECRET_KEY": "test-smoke-mode-secret-value-with-enough-length",
        "ENV_MODE": "local",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import api; "
                "print(any(getattr(route, 'path', None) == '/api/health' "
                "for route in api.app.routes))"
            ),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("True")
