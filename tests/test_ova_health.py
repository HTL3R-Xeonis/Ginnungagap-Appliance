"""The health wrapper around the OVA deploy service (docker/ova_deploy_health.py), run against a stand-in for the service."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

WRAPPER = Path(__file__).resolve().parent.parent / "docker" / "ova_deploy_health.py"


def load(monkeypatch, tmp_path, password: str | None, store_files: int):
    """Imports the wrapper fresh, with the secret file and the template store set up as asked."""
    secret = tmp_path / "esxi_password"
    if password is not None:
        secret.write_text(password, encoding="utf-8")
    store = tmp_path / "store"
    store.mkdir()
    for index in range(store_files):
        (store / f"t{index}.ova").write_bytes(b"x")
    # a stand-in for the service's app.py that notes what the environment held when it was imported (settings.py reads it then)
    (tmp_path / "app.py").write_text(
        "import os\nfrom fastapi import FastAPI\napp = FastAPI()\nPASSWORD_AT_IMPORT = os.environ.get('ESXI_PASSWORD')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "app", raising=False)
    monkeypatch.setenv("ESXI_PASSWORD_FILE", str(secret))
    monkeypatch.delenv("ESXI_PASSWORD", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ova_deploy_health_under_test", WRAPPER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STORE = store
    return module, TestClient(module.app)


def test_healthy_with_a_password_and_a_store(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, "geheim", 1)
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {
        "status": "ok",
        "problems": [],
    }


def test_the_password_is_in_the_environment_before_the_service_is_imported_and_has_no_trailing_line_break(
    monkeypatch, tmp_path
):
    load(monkeypatch, tmp_path, "p'a$s w#rd \\\n", 1)
    assert sys.modules["app"].PASSWORD_AT_IMPORT == "p'a$s w#rd \\"


def test_a_missing_password_is_a_503_that_names_the_cause_but_not_the_secret(
    monkeypatch, tmp_path
):
    _, client = load(monkeypatch, tmp_path, None, 1)
    response = client.get("/health")
    assert response.status_code == 503
    assert "no ESXi password" in response.json()["problems"][0]


def test_an_empty_store_is_a_503(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, "geheim", 0)
    response = client.get("/health")
    assert (
        response.status_code == 503
        and "template store" in response.json()["problems"][0]
    )
    assert "geheim" not in response.text
