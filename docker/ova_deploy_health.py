"""
Wraps the OVA deploy service: reads the ESXi password from the mounted secret file (the service itself reads the environment
variable ESXI_PASSWORD) and adds GET /health. The service's own code stays untouched.
"""

import os
from pathlib import Path

from fastapi.responses import JSONResponse

PASSWORD_FILE = Path(os.environ.get("ESXI_PASSWORD_FILE", "/run/secrets/esxi_password"))
STORE = Path("/templates/esxi")

try:
    # only line breaks at the end are dropped; every other character belongs to the password
    os.environ["ESXI_PASSWORD"] = PASSWORD_FILE.read_text(encoding="utf-8").rstrip(
        "\r\n"
    )
except OSError:
    pass

# imported only now: settings.py reads the password from the environment when it is imported
from app import app


@app.get("/health")
def health():
    problems = []
    if not os.environ.get("ESXI_PASSWORD"):
        problems.append(f"no ESXi password ({PASSWORD_FILE} is missing or empty)")
    if not STORE.is_dir() or not any(STORE.iterdir()):
        problems.append(f"the template store {STORE} is missing or empty")
    return JSONResponse(
        {"status": "degraded" if problems else "ok", "problems": problems},
        status_code=503 if problems else 200,
    )
