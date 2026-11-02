"""
Wraps the OVA deploy service: reads the fallback ESXi password from the mounted secret file (the service reads the
environment variable ESXI_PASSWORD) and adds GET /health. The service's own code stays untouched. The password of each
host comes from the app's host profiles (HOST_PROFILES_DIR), see passwords.py of the service.
"""

import os
from pathlib import Path

from fastapi.responses import JSONResponse

PASSWORD_FILE = Path(os.environ.get("ESXI_PASSWORD_FILE", "/run/secrets/esxi_password"))
STORE = Path("/templates/esxi")

try:
    # only line breaks at the end are dropped; every other character belongs to the password
    fallback = PASSWORD_FILE.read_text(encoding="utf-8").rstrip("\r\n")
    if fallback:
        os.environ["ESXI_PASSWORD"] = fallback
except OSError:
    pass

# imported only now: settings.py reads the password from the environment when it is imported
from app import app
from passwords import known_hosts


@app.get("/health")
def health():
    """Degraded only when the OVAs can't be read; no stored password yet is normal on a fresh machine."""
    problems = []
    if not STORE.is_dir() or not any(STORE.iterdir()):
        problems.append(f"the template store {STORE} is missing or empty")
    return JSONResponse(
        {
            "status": "degraded" if problems else "ok",
            "problems": problems,
            "hosts_with_password": known_hosts(),
            "fallback_password": bool(os.environ.get("ESXI_PASSWORD")),
        },
        status_code=503 if problems else 200,
    )
