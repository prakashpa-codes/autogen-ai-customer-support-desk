"""Start the backend from the dashboard, so `streamlit run app.py` is the only command needed locally.

Only for a local backend (127.0.0.1 / localhost) that is not already running. With Docker, or with
the backend started separately, this does nothing.
"""

from __future__ import annotations

import atexit
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from desk.api import BACKEND_URL, Backend, BackendError

ROOT = Path(__file__).resolve().parent.parent
STARTUP_TIMEOUT = 90  # first start embeds the knowledge base


def _online() -> bool:
    try:
        Backend().health()
        return True
    except BackendError:
        return False


def ensure_backend() -> str:
    """Return "running", "started" or "remote"; raise RuntimeError if a local start fails."""
    url = urlparse(BACKEND_URL)
    if url.hostname not in {"127.0.0.1", "localhost"}:
        return "remote"
    if _online():
        return "running"
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(url.port or 8000)],
        cwd=ROOT,
    )
    atexit.register(process.terminate)
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"The backend exited during startup (code {process.returncode}). See the terminal output.")
        if _online():
            return "started"
        time.sleep(1)
    raise RuntimeError(f"The backend did not start within {STARTUP_TIMEOUT} s.")
