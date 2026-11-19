import copy
from pathlib import Path

import pytest
import yaml

from ginnungagap import __main__ as cli_module
from ginnungagap import config as config_module
from ginnungagap import firstboot as firstboot_module
from ginnungagap import operate as operate_module

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def example() -> dict:
    """The example file as the user-written dictionary (before defaults)."""
    return yaml.safe_load(
        (ROOT / "ginnungagap.example.yml").read_text(encoding="utf-8")
    )


@pytest.fixture
def make_config(example):
    """A normalised config: the example with the given nested changes, e.g. make_config(services={"generator": False})."""

    def build(**changes):
        raw = copy.deepcopy(example)
        for section, values in changes.items():
            if isinstance(values, dict) and isinstance(raw.get(section), dict):
                raw[section].update(values)
            else:
                raw[section] = values
        return config_module.normalise(raw)

    return build


@pytest.fixture
def generator_sources(tmp_path) -> Path:
    folder = tmp_path / "generator-config"
    folder.mkdir()
    (folder / "prompt_template.txt").write_text("PROMPT {request}\n")
    (folder / "example_topology.yml").write_text("nodes: []\n")
    return folder


@pytest.fixture(autouse=True)
def no_real_store_folder(monkeypatch):
    """apply() creates a missing local template store folder; a test must never create /srv/templates on the machine."""
    monkeypatch.setattr(operate_module, "_make_store_dir", lambda path: None)


@pytest.fixture(autouse=True)
def no_real_password_change(monkeypatch):
    """First boot sets the login user's password with chpasswd; a test must never do that to the machine it runs on."""
    monkeypatch.setattr(firstboot_module, "_set_password", lambda user, password: 0)


@pytest.fixture(autouse=True)
def console_refreshes(monkeypatch):
    """The command line rewrites the console login text after bootstrap/apply; tests record it instead of touching /etc."""
    calls = []
    monkeypatch.setattr(
        cli_module,
        "_refresh_console",
        lambda config_dir, clear_screens=False: calls.append(Path(config_dir)),
    )
    return calls


@pytest.fixture(autouse=True)
def update_state_in_tmp(monkeypatch, tmp_path_factory):
    """The update state folder and its lock live under /var/lib/ginnungagap; a test must never create them on the machine."""
    monkeypatch.setenv(
        "GINNUNGAGAP_UPDATE_DIR", str(tmp_path_factory.mktemp("update-state"))
    )
