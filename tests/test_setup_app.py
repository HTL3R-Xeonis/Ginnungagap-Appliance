"""The thin FastAPI shim (docker/setup_app.py) around ginnungagap/setup_wizard.py - the HTTP layer only; the actual
form-to-config logic is tested without FastAPI in tests/test_setup_wizard.py. Skipped where fastapi/httpx are not
installed, same as tests/test_ova_health.py for the OVA deploy wrapper."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
bcrypt = pytest.importorskip("bcrypt")
from fastapi.testclient import TestClient

APP_FILE = Path(__file__).resolve().parent.parent / "docker" / "setup_app.py"

MINIMAL_FORM = {
    "public_url": "https://ginn.lab",
    "timezone": "UTC",
    "tls_mode": "off",
    "auth_mode": "none",
    "esxi_mode": "external",
    "esxi_url": "http://10.0.0.1:8000",
    "gns3_mode": "external",
    "gns3_url": "http://10.0.0.1:8001",
    "generator_mode": "external",
    "generator_url": "http://10.0.0.1:8002",
    "ova_deploy_mode": "off",
    "store_type": "local",
    "store_local_path": "/srv/templates",
    "esxi_refresh_minutes": "15",
    "gns3_refresh_minutes": "5",
    "generator_max_retries": "3",
    "generator_timeout_seconds": "500",
    "ova_timeout_seconds": "600",
    "logging_level": "INFO",
    "logging_max_lines": "100000",
    "http_port": "80",
    "https_port": "443",
    "action": "apply",
}


def load(monkeypatch, config_dir: Path, staging_dir: Path):
    monkeypatch.setenv("SETUP_WIZARD_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("SETUP_WIZARD_STAGING_DIR", str(staging_dir))
    monkeypatch.delitem(sys.modules, "setup_app_under_test", raising=False)
    spec = importlib.util.spec_from_file_location("setup_app_under_test", APP_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, TestClient(module.app)


@pytest.fixture
def app(monkeypatch, tmp_path):
    config_dir = tmp_path / "etc-ginnungagap"
    staging_dir = tmp_path / "staging"
    config_dir.mkdir()
    return load(monkeypatch, config_dir, staging_dir) + (config_dir, staging_dir)


def test_health(app):
    _module, client, _c, _s = app
    assert client.get("/health").json() == {"status": "ok"}


def test_unconfigured_appliance_reports_not_ready(app):
    _module, client, _config_dir, _staging_dir = app
    response = client.get("/appliance/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unconfigured", "services": {}}


@pytest.mark.parametrize("generator_available", [True, False])
def test_appliance_health_checks_bundled_services(
    app, monkeypatch, generator_available
):
    module, client, config_dir, _staging_dir = app
    (config_dir / "ginnungagap.yml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "template_esxi": False,
                    "template_gns3": False,
                    "generator": True,
                    "ova_deploy": "off",
                },
                "templates": {"esxi_url": "http://t:8000", "gns3_url": "http://t:8001"},
                "generator": {
                    "llm_servers": [
                        {"name": "test", "url": "http://model:11434", "model": "m"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    requested = []

    def fetch(url):
        requested.append(url)
        if url == "http://generator:8000/health" and not generator_available:
            return 503, '{"detail":"private diagnostic"}', None
        return 200, '{"status":"ok"}', None

    monkeypatch.setattr(module.status, "make_fetch", lambda *, timeout: fetch)
    response = client.get("/appliance/health")
    assert response.status_code == (200 if generator_available else 503)
    assert response.json() == {
        "status": "ok" if generator_available else "degraded",
        "services": {
            "ginnungagap": "ok",
            "template-esxi": "off",
            "template-gns3": "off",
            "generator": "ok" if generator_available else "degraded",
            "ova-deploy": "off",
        },
    }
    assert requested == [
        "http://ginnungagap:8123/health",
        "http://generator:8000/health",
    ]


def test_the_form_page_shows_the_defaults_and_no_javascript(app):
    _module, client, _c, _s = app
    response = client.get("/")
    assert response.status_code == 200
    assert "<script" not in response.text.lower()
    assert 'value="UTC"' in response.text


def test_downloading_builds_the_same_yaml_apply_would_use(app):
    _module, client, _c, _s = app
    response = client.post("/", data={**MINIMAL_FORM, "action": "download"})
    assert response.status_code == 200
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="ginnungagap.yml"'
    )
    parsed = yaml.safe_load(response.text)
    assert parsed["appliance"]["public_url"] == "https://ginn.lab"
    assert parsed["templates"]["esxi_url"] == "http://10.0.0.1:8000"


def test_a_broken_submission_is_rejected_with_a_problem_list_and_nothing_staged(app):
    _module, client, _c, staging_dir = app
    broken = {**MINIMAL_FORM, "public_url": "not-a-url"}
    response = client.post("/", data=broken)
    assert response.status_code == 400
    assert "public_url" in response.text
    assert "must be an http" in response.text
    assert not (staging_dir / "apply.request").exists()


def test_a_valid_apply_submission_stages_the_configuration_for_the_host_watcher(app):
    _module, client, _c, staging_dir = app
    response = client.post("/", data=MINIMAL_FORM)
    assert response.status_code == 200
    assert "Configuration accepted" in response.text
    assert (staging_dir / "apply.request").exists()
    staged = yaml.safe_load((staging_dir / "ginnungagap.yml").read_text())
    assert staged["appliance"]["public_url"] == "https://ginn.lab"


def test_a_basic_auth_password_is_hashed_through_caddy_not_stored_plain(
    app, monkeypatch
):
    module, client, _c, staging_dir = app

    def fake_run(args, **kwargs):
        assert args[:2] == ["caddy", "hash-password"]
        assert "--plaintext" in args
        return subprocess.CompletedProcess(
            args, 0, stdout="$2a$14$fakehashvalue\n", stderr=""
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    form = {
        **MINIMAL_FORM,
        "auth_mode": "basic",
        "auth_user": "admin",
        "auth_password": "typed-plaintext-password",
    }
    response = client.post("/", data=form)
    assert response.status_code == 200
    staged = (staging_dir / "ginnungagap.yml").read_text()
    assert "typed-plaintext-password" not in staged
    assert "$2a$14$fakehashvalue" in staged


def test_an_existing_configuration_is_shown_as_already_set_without_its_secrets(
    monkeypatch, tmp_path
):
    config_dir = tmp_path / "etc-ginnungagap"
    config_dir.mkdir()
    password_hash = bcrypt.hashpw(b"local-password", bcrypt.gensalt(rounds=4)).decode()
    (config_dir / "ginnungagap.yml").write_text(
        yaml.safe_dump(
            {
                "appliance": {
                    "public_url": "https://already.lab",
                    "auth": {
                        "mode": "basic",
                        "user": "test-admin",
                        "password_hash": password_hash,
                    },
                },
                "services": {
                    "template_esxi": False,
                    "template_gns3": False,
                    "generator": False,
                    "ova_deploy": "off",
                },
                "templates": {"esxi_url": "http://t:8000", "gns3_url": "http://t:8001"},
                "generator": {"url": "http://t:8002"},
            }
        ),
        encoding="utf-8",
    )
    _module, client = load(monkeypatch, config_dir, tmp_path / "staging")
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("test-admin", "wrong-password")).status_code == 401
    response = client.get("/", auth=("test-admin", "local-password"))
    assert response.status_code == 200
    assert "https://already.lab" in response.text
    assert "super-secret" not in response.text
    assert "already applied" in response.text


def test_configured_appliance_without_basic_auth_has_no_wizard(app):
    _module, client, config_dir, staging_dir = app
    (config_dir / "ginnungagap.yml").write_text(
        "appliance: {auth: {mode: none}}\n", encoding="utf-8"
    )
    assert client.get("/").status_code == 404
    assert client.post("/", data=MINIMAL_FORM).status_code == 404
    assert not (staging_dir / "apply.request").exists()
