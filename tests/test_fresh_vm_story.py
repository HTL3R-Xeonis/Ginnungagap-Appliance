"""
End-to-end trace of the whole fresh-VM story, in one test, exactly the sequence the task asks to verify rather than
trust: no configuration -> the wizard is reachable (bootstrap.run) -> a submission is built and validated the same way
the web form does (setup_wizard.build_raw_config/config.normalise) -> it is staged and signalled
(setup_wizard.write_staged) -> the host watcher picks it up and calls the REAL operate.apply (setup_watcher.apply_staged,
with only docker itself replaced by a recorder - render.render really runs) -> the rendered files have genuinely
switched from the bootstrap ones to the real ones.

This is the one place a mismatch between bootstrap.py, setup_wizard.py, setup_watcher.py and operate.py/render.py
(wrong argument order, a path type, a profile name typo) would show up even though each module's own tests mock its
neighbours - the plan explicitly warned this class of bug looks fine against mocks and breaks on a real VM's timing.
"""

from __future__ import annotations

import json

import pytest
import yaml

from ginnungagap import bootstrap, operate, setup_watcher
from ginnungagap import config as config_module
from ginnungagap import setup_wizard as wizard


class Recorder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        return 0


def healthy_fetch(_url):
    # the submitted config runs nothing bundled (every service is external or off), so status.check only ever asks
    # the app's own /health - one fixed healthy answer is enough here, this is not a test of status.py itself
    return 200, json.dumps({"warning": None}), None


@pytest.fixture
def bundle(tmp_path):
    folder = tmp_path / "bundle"
    folder.mkdir()
    return folder


def test_no_config_wizard_apply_the_app_takes_over(bundle, tmp_path, monkeypatch):
    monkeypatch.delenv("GINNUNGAGAP_PROJECT", raising=False)
    config_dir = tmp_path / "etc-ginnungagap"
    staging_dir = tmp_path / "staging"

    # 1. a completely fresh VM: nothing at config_dir yet, bootstrap brings up gateway + setup only
    boot_run = Recorder()
    assert bootstrap.run(bundle, runner=boot_run, build=False) == 0
    bootstrap_env = (bundle / "rendered" / "compose.env").read_text()
    assert "COMPOSE_PROFILES=gateway,setup" in bootstrap_env
    bootstrap_caddy = (bundle / "rendered" / "Caddyfile").read_text()
    assert (
        "reverse_proxy setup:8000" in bootstrap_caddy
        and "basic_auth" not in bootstrap_caddy
    )

    # 2. the admin fills in the wizard; everything runs elsewhere so no sibling generator-config checkout is needed
    values = {
        "public_url": "https://ginn.lab",
        "esxi_mode": "external",
        "esxi_url": "http://10.0.0.1:8000",
        "gns3_mode": "external",
        "gns3_url": "http://10.0.0.1:8001",
        "generator_mode": "external",
        "generator_url": "http://10.0.0.1:8002",
        "ova_deploy_mode": "off",
    }
    raw = wizard.build_raw_config(values)
    config = config_module.normalise(raw)  # exactly what the CLI's own `validate` does
    yaml_text = wizard.render_yaml(config)

    # 3. the container stages it (its only writable path) and signals the host
    wizard.write_staged(staging_dir, yaml_text)
    assert (staging_dir / "apply.request").exists()

    # 4. the host watcher (systemd path unit) picks it up, re-validates independently, writes the live file and
    #    calls the REAL operate.apply - only docker itself is replaced, render.render really runs
    apply_run = Recorder()
    code = setup_watcher.apply_staged(
        staging_dir,
        config_dir / "ginnungagap.yml",
        bundle,
        None,
        apply=lambda *a, **k: operate.apply(
            *a,
            run=apply_run,
            fetch=healthy_fetch,
            sleep=lambda s: None,
            inspect=lambda args: None,
            **k,
        ),
    )
    assert code == 0
    assert not (staging_dir / "apply.request").exists()
    assert yaml.safe_load((config_dir / "ginnungagap.yml").read_text()) == config

    # 5. the rendered files have genuinely switched: the real profile list (now including "setup" - always on, see
    #    render.profiles) and the real Caddyfile (the app's own routes, no more unconditional proxy to the wizard)
    real_env = (bundle / "rendered" / "compose.env").read_text()
    assert "COMPOSE_PROFILES=gateway,ginnungagap,setup" in real_env
    real_caddy = (bundle / "rendered" / "Caddyfile").read_text()
    assert "ginnungagap:8123" in real_caddy and "/setup/*" in real_caddy
    assert (
        real_caddy != bootstrap_caddy
    )  # Caddyfile is in operate.FILE_OWNERS: gateway gets recreated for this
    assert any(
        "up" in call for call in apply_run.calls
    )  # the real `docker compose up` really ran (recorder only)
