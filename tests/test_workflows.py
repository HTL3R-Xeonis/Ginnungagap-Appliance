"""The GitHub Actions files: valid, and shaped so that nothing is published by accident."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True)  # YAML 1.1 reads the key `on` as true
    return data


def steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_the_workflow_is_valid_and_grants_only_read_access_by_default(name):
    workflow = load(name)
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]


def test_ci_runs_the_tests_and_both_ruff_checks_on_every_push_and_pull_request():
    workflow = load("ci.yml")
    assert set(workflow["on"]) == {"push", "pull_request"}
    commands = "\n".join(
        step.get("run", "") for step in steps(workflow, "tests_and_linter")
    )
    for command in ("pytest", "ruff check", "ruff format"):
        assert command in commands
    assert "--check" in commands


def test_the_release_workflow_starts_only_from_a_pushed_version_tag_or_by_hand():
    workflow = load("release.yml")
    trigger = workflow["on"]
    assert set(trigger) == {"push", "workflow_dispatch"}
    assert trigger["push"] == {"tags": ["v*"]}  # no branch push builds a release
    dispatch = trigger["workflow_dispatch"]["inputs"]
    assert (
        dispatch["dry_run"]["default"] is True
        and dispatch["dry_run"]["type"] == "boolean"
    )
    assert dispatch["tag"]["required"] is True


def test_only_the_publish_step_makes_a_release_and_a_dry_run_never_reaches_it():
    workflow = load("release.yml")
    release_steps = steps(workflow, "release")
    publishing = [s for s in release_steps if "gh release create" in s.get("run", "")]
    assert len(publishing) == 1
    assert publishing[0]["if"] == "env.DRY_RUN != 'true'"
    assert "--verify-tag" in publishing[0]["run"]  # never creates a tag from a branch
    assert "--prerelease" in publishing[0]["run"]  # for a tag with a dash
    assert not [
        s
        for s in release_steps
        if "gh release create" in s.get("run", "") and s is not publishing[0]
    ]
    kept = [
        s
        for s in release_steps
        if s.get("uses", "").startswith("actions/upload-artifact")
    ]
    assert len(kept) == 1 and kept[0]["if"] == "env.DRY_RUN == 'true'"
    assert workflow["jobs"]["release"]["permissions"] == {"contents": "write"}


def test_the_gate_runs_before_anything_is_built_and_the_files_are_verified_after():
    names = [s.get("name", "") for s in steps(load("release.yml"), "release")]
    gate, build, check, publish = (
        names.index(n)
        for n in (
            "Tests and style checks",
            "Build the release files",
            "Check what was built",
            "Publish the release",
        )
    )
    assert gate < build < check < publish


def test_secrets_reach_the_job_only_through_environment_variables_and_are_never_printed():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert text.count("secrets.") == 2
    env = load("release.yml")["jobs"]["release"]["env"]
    assert env["COMPONENTS_READ_TOKEN"] == "${{ secrets.COMPONENTS_READ_TOKEN }}"
    assert env["RELEASE_SIGNING_KEY"] == "${{ secrets.RELEASE_SIGNING_KEY }}"
    for step in steps(load("release.yml"), "release"):
        run = step.get("run", "")
        assert (
            "echo $RELEASE_SIGNING_KEY" not in run
            and 'echo "$RELEASE_SIGNING_KEY"' not in run
        )
        assert (
            "${{ secrets." not in run and "${{ inputs." not in run
        )  # no expression is pasted into a shell script
    assert "trap " in text and "release_key" in text  # the key file is removed again


def test_both_secrets_are_optional_because_the_repositories_are_public():
    names = [s.get("name") for s in steps(load("release.yml"), "release")]
    assert "Say what is missing" in names
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert (
        "RELEASE_SIGNING_KEY is not set" in text
        and "COMPONENTS_READ_TOKEN is not set" in text
    )


# ---- the component repositories' release workflows name the same repository the manifest expects ----

SIBLINGS = Path(__file__).resolve().parent.parent.parent


@pytest.mark.parametrize(
    "component", ["operator", "builder", "template-services", "generator"]
)
def test_the_release_workflow_of_a_component_repository_is_named_like_the_manifest_expects(
    component,
):
    from ginnungagap import manifest as mf

    folders = (mf.REPO_NAMES[component], *mf.LEGACY_NAMES[component])
    workflow = next(
        (
            SIBLINGS / folder / ".github" / "workflows" / "release.yml"
            for folder in folders
            if (SIBLINGS / folder / ".github" / "workflows" / "release.yml").is_file()
        ),
        None,
    )
    if workflow is None:
        pytest.skip(f"{mf.REPO_NAMES[component]} is not next to this repository")
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    # the asset is `${COMPONENT}-<version>.tar.gz`: it has to be the repository's name, which is what the manifest checks
    assert data["env"]["COMPONENT"] == mf.REPO_NAMES[component]
