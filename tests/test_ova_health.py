"""The health wrapper around the OVA deploy service (docker/ova_deploy_health.py), run against a stand-in for the service."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

WRAPPER = Path(__file__).resolve().parent.parent / "docker" / "ova_deploy_health.py"


def load(monkeypatch, tmp_path, store_files: int):
    """Imports the wrapper fresh, with the template store set up as asked."""
    store = tmp_path / "store"
    store.mkdir()
    for index in range(store_files):
        (store / f"t{index}.ova").write_bytes(b"x")
    # a stand-in for the service's own app.py
    (tmp_path / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "app", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ova_deploy_health_under_test", WRAPPER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STORE = store
    return module, TestClient(module.app)


def test_healthy_with_templates_in_the_store(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, 1)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "problems": []}


def test_the_health_answer_has_no_password_related_fields(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, 1)
    body = client.get("/health").json()
    assert not {"hosts_with_password", "fallback_password"} & set(body)


def test_the_wrapper_no_longer_reads_a_password_or_the_services_password_lookup():
    source = WRAPPER.read_text(encoding="utf-8")
    for removed in ("ESXI_PASSWORD", "known_hosts", "passwords", "run/secrets"):
        assert removed not in source


def test_an_empty_store_is_a_503(monkeypatch, tmp_path):
    _, client = load(monkeypatch, tmp_path, 0)
    response = client.get("/health")
    assert (
        response.status_code == 503
        and "template store" in response.json()["problems"][0]
    )
