"""
Wraps the OVA deploy service and adds GET /health. The service's own code stays untouched. The ESXi login of each deploy
comes with the request itself (the Operator sends the active host profile's user name and password), so this container
holds no password of its own.
"""

from pathlib import Path

from fastapi.responses import JSONResponse

STORE = Path("/templates/esxi")

from app import app


@app.get("/health")
def health():
    """Degraded only when the OVAs can't be read."""
    problems = []
    if not STORE.is_dir() or not any(STORE.iterdir()):
        problems.append(f"the template store {STORE} is missing or empty")
    return JSONResponse(
        {"status": "degraded" if problems else "ok", "problems": problems},
        status_code=503 if problems else 200,
    )
