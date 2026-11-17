"""Bringing the appliance up before any ginnungagap.yml exists (ginnungagap/bootstrap.py) - the crux of the whole
feature: without this, a fresh VM (or one with a rejected guestinfo config) has nothing listening on 80/443 at all,
and the setup wizard is unreachable. Traces the exact files and the exact `docker compose` invocation, the same way
tests/test_operate.py traces `apply`."""

from __future__ import annotations

import pytest

from ginnungagap import bootstrap


class Recorder:
    def __init__(self, codes=()):
        self.calls: list[list[str]] = []
        self.codes = list(codes)

    def __call__(self, args):
        self.calls.append(args)
        return self.codes.pop(0) if self.codes else 0


@pytest.fixture
def bundle(tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    return folder


def test_it_writes_a_minimal_env_and_a_static_caddyfile_then_starts_gateway_and_setup(
    bundle, monkeypatch
):
    monkeypatch.delenv("GINNUNGAGAP_PROJECT", raising=False)
    run = Recorder()
    code = bootstrap.run(bundle, runner=run)
    assert code == 0
    env = (bundle / "rendered" / "compose.env").read_text()
    assert "COMPOSE_PROFILES=gateway,setup" in env
    assert "HTTP_PORT=80" in env and "HTTPS_PORT=443" in env
    caddy = (bundle / "rendered" / "Caddyfile").read_text()
    assert "reverse_proxy setup:8000" in caddy
    assert "basic_auth" not in caddy  # nothing exists yet to protect
    assert (
        "tls internal" not in caddy
    )  # no public_url is known yet to name a certificate for
    assert run.calls == [
        [
            "docker", "compose", "--project-directory", str(bundle),
            "--env-file", str(bundle / "rendered" / "compose.env"),
            "up", "-d", "--remove-orphans", "--build",
        ]
    ]  # fmt: skip


def test_no_build_is_honoured(bundle):
    run = Recorder()
    bootstrap.run(bundle, runner=run, build=False)
    assert "--build" not in run.calls[0]


def test_a_failed_compose_up_is_reported(bundle):
    lines = []
    code = bootstrap.run(bundle, runner=Recorder(codes=[1]), out=lines.append)
    assert code == 1
    assert any("failed" in line for line in lines)


def test_running_it_again_is_idempotent_and_only_recreates_what_changed(bundle):
    bootstrap.run(bundle, runner=Recorder())
    first_caddy = (bundle / "rendered" / "Caddyfile").read_text()
    bootstrap.run(bundle, runner=Recorder())
    assert (bundle / "rendered" / "Caddyfile").read_text() == first_caddy


def test_host_paths_from_the_environment_reach_the_bootstrap_env_too(
    bundle, monkeypatch
):
    monkeypatch.setenv("GINNUNGAGAP_CONFIG_DIR", "/custom/config")
    monkeypatch.setenv("GINNUNGAGAP_SETUP_STAGING_DIR", "/custom/staging")
    bootstrap.run(bundle, runner=Recorder())
    env = (bundle / "rendered" / "compose.env").read_text()
    assert "GINNUNGAGAP_CONFIG_DIR=/custom/config" in env
    assert "GINNUNGAGAP_SETUP_STAGING_DIR=/custom/staging" in env


def test_bootstrap_forgets_what_an_earlier_apply_recorded(tmp_path):
    from ginnungagap import operate

    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / operate.APPLIED).write_text('{"Caddyfile": "old"}', encoding="utf-8")
    bootstrap.run(tmp_path, runner=lambda args: 0, build=False, out=lambda line: None)
    assert not (rendered / operate.APPLIED).exists()
