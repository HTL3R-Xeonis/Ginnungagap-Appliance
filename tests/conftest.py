import copy
from pathlib import Path

import pytest
import yaml

from ginnungagap import config as config_module

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
