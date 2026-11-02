"""The health wrapper around the OVA deploy service (docker/ova_deploy_health.py), run against a stand-in for the service."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

WRAPPER = Path(__file__).resolve().parent.parent / "docker" / "ova_deploy_health.py"


def load(monkeypatch, tmp_path, password: str | None, store_files: int, hosts: int = 0):
    """Imports the wrapper fresh, with the secret file and the template store set up as asked."""
    secret = tmp_path / "esxi_password"
    if password is not None:
        secret.write_text(password, encoding="utf-8")
    store = tmp_path / "store"
    store.mkdir()
    for index in range(store_files):
        (store / f"t{index}.ova").write_bytes(b"x")
    # stand-ins for the service's own modules; app.py notes what the environment held when it was imported (settings.py reads it then)
    (tmp_path / "app.py").write_text(
        "import os\nfrom fastapi import FastAPI\napp = FastAPI()\nPASSWORD_AT_IMPORT = os.environ.get('ESXI_PASSWORD')\n",
        encoding="utf-8",
    )
    (tmp_path / "passwords.py").write_text(
        f"def known_hosts():\n    return {hosts}\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("app", "passwords"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setenv("ESXI_PASSWORD_FILE", str(secret))
    monkeypatch.delenv("ESXI_PASSWORD", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ova_deploy_health_under_test", WRAPPER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STORE = store
    return module, TestClient(module.app)


def test_healthy_reports_the_hosts_and_whether_a_fallback_exists(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, "geheim", 1, hosts=3)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "problems": [],
        "hosts_with_password": 3,
        "fallback_password": True,
    }
    assert "geheim" not in response.text


def test_a_fresh_machine_without_any_password_is_still_healthy(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, "", 1)  # an empty secret file: no fallback
    response = client.get("/health")
    assert response.status_code == 200
    assert (
        response.json()["fallback_password"] is False
        and response.json()["hosts_with_password"] == 0
    )


def test_a_missing_secret_file_is_no_error_either(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, None, 1)
    assert client.get("/health").status_code == 200


def test_the_fallback_is_in_the_environment_before_the_service_is_imported_and_has_no_trailing_line_break(
    monkeypatch, tmp_path
):
    load(monkeypatch, tmp_path, "p'a$s w#rd \\\n", 1)
    assert sys.modules["app"].PASSWORD_AT_IMPORT == "p'a$s w#rd \\"


def test_an_empty_store_is_a_503(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, "geheim", 0)
    response = client.get("/health")
    assert (
        response.status_code == 503
        and "template store" in response.json()["problems"][0]
    )
    assert "geheim" not in response.text
