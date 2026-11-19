"""
The component code names (PLAN_component_codenames.md): one table of names, the spelling that must not be "corrected", and the
old names that may only appear where they are read for compatibility.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from ginnungagap import manifest as mf

ROOT = Path(__file__).resolve().parent.parent
THIS = Path(__file__).resolve()

# The code names are the user's own spellings. Tools and agents like to "correct" them to the usual mythological ones; these
# are those (written in pieces, so that this file does not trip over itself).
STANDARD_SPELLINGS = [
    "Ygg" + "drasil",
    "Ygg" + "drasill",
    "Mjol" + "nir",
    "Mjöl" + "nir",
    "Mi" + "mir",
    "Mímir",
    "Hu" + "ginn",
    "Mu" + "ninn",
    "Idu" + "nn",
    "Iðu" + "nn",
    "Heim" + "dall",
    "Heim" + "dallr",
]
CODE_NAMES = ["Igdrasyl", "Mjonyr", "Mimyr", "Hugyn", "Munyn", "Idynoun", "Haymdal"]
OLD_NAMES = re.compile(
    r"TopologyOperator|TopologyGenerator|topology-generator|Docker-Server-Files|docker-server-files"
)
# where the old names may stay: the table that reads them, the installer that reads old folders, the README notes that say
# "formerly", and the tests (and the smoke case) of exactly that
OLD_NAMES_ALLOWED = (
    "ginnungagap/manifest.py",
    "install.sh",
    "README.md",
    "smoke/update-smoke.sh",  # its one old-layout case
    "tests/",
)
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".sh",
    ".yml",
    ".yaml",
    ".json",
    ".txt",
    ".toml",
    ".service",
    ".cfg",
    ".ini",
    "",
}


def tracked_files() -> list[Path]:
    """The files of the repository (git's list when this is a checkout, else everything but caches)."""
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode()
        names = [n for n in listed.split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [
            str(p.relative_to(ROOT))
            for p in ROOT.rglob("*")
            if p.is_file()
            and not {".git", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache"}
            & set(p.parts)
        ]
    files = []
    for name in names:
        path = ROOT / name
        if path.is_file() and path.suffix in TEXT_SUFFIXES:
            files.append(path)
    return files


def text_of(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def test_the_scan_sees_the_files_it_is_meant_to_check():
    names = {p.relative_to(ROOT).as_posix() for p in tracked_files()}
    assert {
        "README.md",
        "ginnungagap/manifest.py",
        "install.sh",
        "compose.yml",
    } <= names


def test_no_standard_mythological_spelling_was_put_in_place_of_a_code_name():
    offenders = []
    for path in tracked_files():
        if path == THIS or path.name == "THIRD_PARTY_NOTICES.md":
            continue
        lowered = text_of(path).lower()
        offenders += [
            f"{path.relative_to(ROOT)}: {word}"
            for word in STANDARD_SPELLINGS
            if word.lower() in lowered
        ]
    assert offenders == [], (
        "the code names are spelled Igdrasyl, Mjonyr, Mimyr, Hugyn, Munyn, Idynoun, Haymdal - not the usual spellings: "
        f"{offenders}"
    )


def test_the_guard_catches_a_corrected_spelling():
    assert any(w.lower() in "the Huginn service".lower() for w in STANDARD_SPELLINGS)


def test_the_code_names_in_the_table_are_spelled_as_decided():
    assert set(mf.CODE_NAMES.values()) == {
        "Igdrasyl",
        "Mjonyr",
        "Mimyr",
        "Hugyn and Munyn",
    }
    assert set(mf.SERVICE_CODE_NAMES.values()) == {
        "Igdrasyl",
        "Mimyr",
        "Hugyn",
        "Munyn",
    }
    assert mf.SERVICE_CODE_NAMES["template-esxi"] == "Hugyn"  # the ESXi side
    assert mf.SERVICE_CODE_NAMES["template-gns3"] == "Munyn"  # the GNS3 side


def test_the_readme_introduces_every_code_name_it_uses():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("Igdrasyl", "Mjonyr", "Mimyr", "Hugyn and Munyn", "Idynoun"):
        assert name in readme, name
    assert "Hugyn-and-Munyn" in readme  # the repository


def test_old_names_stay_only_where_they_are_read_for_compatibility():
    offenders = []
    for path in tracked_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(OLD_NAMES_ALLOWED[-1]) or relative in OLD_NAMES_ALLOWED:
            continue
        for number, line in enumerate(text_of(path).splitlines(), 1):
            if OLD_NAMES.search(line):
                offenders.append(f"{relative}:{number}: {line.strip()[:90]}")
    assert offenders == []


def test_the_readme_mentions_an_old_name_only_as_a_formerly_note():
    for number, line in enumerate(
        (ROOT / "README.md").read_text(encoding="utf-8").splitlines(), 1
    ):
        if OLD_NAMES.search(line):
            assert re.search(
                r"formerly|before the code names|\bold\b|legacy", line, re.IGNORECASE
            ), (
                f"README.md:{number}: an old name without a 'formerly' note: {line.strip()[:90]}"
            )


@pytest.mark.parametrize("name", ["README.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md"])
def test_no_document_links_to_a_repository_under_its_old_name(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert not re.search(rf"github\.com/[\w.-]+/({OLD_NAMES.pattern})", text)


# ---- one table of names ----


def test_every_component_has_exactly_one_current_name_and_the_checkout_is_called_the_same():
    assert list(mf.REPO_NAMES) == list(mf.COMPONENT_NAMES)
    assert len(set(mf.REPO_NAMES.values())) == len(mf.COMPONENT_NAMES)
    assert mf.CHECKOUT_NAMES == mf.REPO_NAMES
    assert (
        mf.REPO_NAMES
        == {
            "appliance": "Ginnungagap-Appliance",
            "operator": "Igdrasyl",
            "builder": "TopologyBuilder",  # the repository keeps its name, Mjonyr is the code name
            "template-services": "Hugyn-and-Munyn",
            "generator": "Mimyr",
        }
    )


def test_the_keys_are_roles_and_none_of_them_is_a_repository_name_old_or_new():
    names = {n.lower() for n in mf.REPO_NAMES.values()}
    for old in mf.LEGACY_NAMES.values():
        names |= {n.lower() for n in old}
    assert not names & set(mf.COMPONENT_NAMES)
    assert set(mf.LEGACY_KEYS.values()) <= set(mf.COMPONENT_NAMES)
    assert not set(mf.LEGACY_KEYS) & set(mf.COMPONENT_NAMES)


def test_the_pin_file_names_the_repositories_of_the_table():
    pins = yaml.safe_load((ROOT / "release" / "components.in.yml").read_text())
    assert list(pins["components"]) == list(mf.COMPONENT_NAMES)
    for key, entry in pins["components"].items():
        assert entry["repo"] == f"HTL3R-Xeonis/{mf.REPO_NAMES[key]}"


def test_compose_builds_each_service_from_the_folder_the_update_state_links():
    text = (ROOT / "compose.yml").read_text(encoding="utf-8")
    contexts = set(re.findall(r"\$\{REPOS_DIR:-\.\.\}/([A-Za-z0-9-]+)", text))
    assert contexts == {
        mf.CHECKOUT_NAMES[n]
        for n in ("operator", "builder", "template-services", "generator")
    }
