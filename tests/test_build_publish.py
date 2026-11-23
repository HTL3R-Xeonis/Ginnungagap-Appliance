"""An image becomes visible only after inspection and acceptance, without overwrites."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote, urlparse

import pytest
import yaml

from image.build import config, names, pipeline, publish, status
from image.build.ports import (
    AlreadyExists,
    AuthFailed,
    HeadResult,
    NotOurs,
    PermanentError,
    Unreachable,
)
from tests.helpers.fake_esxi import FakeClock
from tests.helpers.fake_template_api import FakeTemplateApi


class ServingShare(publish.LocalShare):
    def head(self, url):
        folder, file = unquote(urlparse(url).path).strip("/").split("/")
        return HeadResult(200, self.size(folder, file), "bytes")


def context(tmp_path):
    store = tmp_path / "share"
    store.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    run = state / "runs" / "1.3.0-20261120T100000Z"
    run.mkdir(parents=True)
    ova = run / "export.ova"
    ova.write_bytes(b"inspected OVA bytes")
    templates = FakeTemplateApi()
    templates.add("old-image", "A" * 64, "old.ova", "old")
    share = ServingShare(store, "http://store")
    cfg = config.BuildHostConfig(
        esxi=config.EsxiConfig(datastore="ds", network="VM Network"),
        paths=config.PathsConfig(state_dir=state),
    )
    ctx = SimpleNamespace(
        config=cfg,
        version="1.3.0",
        names=names.for_version("1.3.0"),
        run_id="1.3.0-20261120T100000Z",
        state_dir=state,
        run_dir=run,
        share=share,
        templates=templates,
        clock=FakeClock(),
        summary=pipeline.BuildSummary("1.3.0-20261120T100000Z", "1.3.0"),
        redactor=pipeline.Redactor(["super-secret-value"]),
        cleanup=Mock(),
        options=pipeline.RunOptions(),
        tracked=[],
        results={
            "P10": {
                "path": str(ova),
                "sha256": hashlib.sha256(ova.read_bytes()).hexdigest(),
                "size": ova.stat().st_size,
            },
            "P11": {
                "passed": True,
                "checks": [{"name": "disk clean", "passed": True, "required": True}],
            },
            "P13": {
                "passed": True,
                "checks": [{"name": "boot", "passed": True, "required": True}],
            },
        },
    )
    ctx.result_of = lambda phase: ctx.results[phase]
    ctx.track_share_folder = lambda folder: ctx.tracked.append(folder)
    ctx.note = lambda key, value: ctx.summary.facts.__setitem__(key, value)

    def refreshed():
        doc = yaml.safe_load(share.read_text(ctx.names.folder, ctx.names.yml))
        row = doc["templates"][0]
        return templates.entries + [
            publish.TemplateEntry(
                row["name"], row["id"], row["file"], ctx.names.folder, row
            )
        ]

    templates.on_refresh = refreshed
    registry = pipeline.PhaseRegistry()
    publish.register_phases(registry)
    return ctx, registry


def run(ctx, registry, phase):
    result = registry.get(phase).fn(ctx)
    ctx.results[phase] = result
    return result


def reviewed_catalog_context(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx, registry = context(tmp_path)
    base = ctx.templates.entries[0]
    ctx.templates.add("unrelated", "B" * 64, "unrelated.ova", "unrelated")
    ctx.results["P1"] = {
        "catalog": [{"name": e.name, "id": e.id} for e in ctx.templates.entries]
    }
    ctx.results["P3"] = {
        "name": base.name,
        "id": base.id,
        "file": base.file,
        "source_dir": base.source_dir,
    }
    ctx.options = pipeline.RunOptions(resume=ctx.run_id, refresh_catalog=True)
    for folder, filename in (("old", "old.ova"), ("unrelated", "unrelated.ova")):
        ctx.share.make_folder(folder)
        ctx.share.put_text(folder, filename, "existing third-party image")
    return ctx, registry


@pytest.mark.parametrize("change", ["changed-id", "added", "removed"])
def test_manual_catalog_review_refreshes_only_unrelated_baseline(tmp_path, change):
    from dataclasses import replace

    ctx, registry = reviewed_catalog_context(tmp_path)
    original = json.loads(json.dumps(ctx.results["P1"]))
    if change == "changed-id":
        ctx.templates.entries[1] = replace(ctx.templates.entries[1], id="C" * 64)
    elif change == "added":
        ctx.templates.add("new-unrelated", "D" * 64, "new.ova", "another")
    else:
        ctx.templates.entries.pop()
    reviewed = {(e.name, e.id) for e in ctx.templates.entries}
    staged = run(ctx, registry, "P12")
    assert ctx.results["P1"] == original
    assert {(e["name"], e["id"]) for e in staged["catalog"]} == reviewed
    audit = staged["catalog_refresh"]
    assert audit["previous"] != audit["current"]
    assert any(audit[k] for k in ("added", "removed", "changed"))
    if change == "changed-id":
        assert audit["changed"] == [
            {"name": "unrelated", "before_id": "B" * 64, "after_id": "C" * 64}
        ]
    assert ctx.summary.facts["catalog_refresh"] == audit
    assert ctx.share.read_text(ctx.names.folder, ctx.names.yml) is None
    assert run(ctx, registry, "P14")["published"]
    for folder, filename in (("old", "old.ova"), ("unrelated", "unrelated.ova")):
        assert ctx.share.read_text(folder, filename) == "existing third-party image"


def test_catalog_drift_still_rejected_without_explicit_manual_refresh(tmp_path):
    ctx, registry = reviewed_catalog_context(tmp_path)
    ctx.options = pipeline.RunOptions(resume=ctx.run_id)
    ctx.templates.add("new", "C" * 64, "new.ova", "new")
    with pytest.raises(PermanentError, match="catalog changed"):
        run(ctx, registry, "P12")
    assert ctx.names.folder not in ctx.share.list_folders()


@pytest.mark.parametrize("change", ["id", "missing", "file", "source", "duplicate"])
def test_manual_refresh_refuses_any_base_identity_change(tmp_path, change):
    from dataclasses import replace

    ctx, registry = reviewed_catalog_context(tmp_path)
    base = ctx.templates.entries[0]
    if change == "missing":
        ctx.templates.entries.pop(0)
    elif change == "duplicate":
        ctx.templates.entries.append(base)
    else:
        key = {"id": "id", "file": "file", "source": "source_dir"}[change]
        ctx.templates.entries[0] = replace(base, **{key: "changed"})
    with pytest.raises(PermanentError, match="base image|duplicate"):
        run(ctx, registry, "P12")
    assert ctx.names.folder not in ctx.share.list_folders()


def test_manual_refresh_requires_original_snapshot_and_this_run(tmp_path):
    ctx, registry = reviewed_catalog_context(tmp_path)
    del ctx.results["P1"]
    with pytest.raises(PermanentError, match="original preflight"):
        run(ctx, registry, "P12")
    ctx, registry = reviewed_catalog_context(tmp_path / "second")
    ctx.options = pipeline.RunOptions(resume="different-run", refresh_catalog=True)
    with pytest.raises(PermanentError, match="this run"):
        run(ctx, registry, "P12")


@pytest.mark.parametrize("collision", ["foreign", "missing-file", "changed-file"])
def test_manual_review_never_blesses_a_conflicting_target(tmp_path, collision):
    ctx, registry = reviewed_catalog_context(tmp_path)
    if collision == "foreign":
        ctx.templates.add(
            ctx.names.template,
            ctx.results["P10"]["sha256"].upper(),
            "foreign.ova",
            "foreign",
        )
    else:
        first = run(ctx, registry, "P12")
        ctx.share.put_text(ctx.names.folder, ctx.names.yml, first["yaml"])
        ctx.templates.refresh()
        path = ctx.share.root / ctx.names.folder / first["file"]
        if collision == "missing-file":
            path.unlink()
        else:
            path.write_bytes(b"changed")
    with pytest.raises(PermanentError, match="conflicting target"):
        run(ctx, registry, "P12")


def test_manual_refresh_can_resume_only_its_verified_published_image(tmp_path):
    from dataclasses import replace

    ctx, registry = reviewed_catalog_context(tmp_path)
    first = run(ctx, registry, "P12")
    ctx.share.put_text(ctx.names.folder, ctx.names.yml, first["yaml"])
    ctx.templates.refresh()
    ctx.templates.entries[1] = replace(ctx.templates.entries[1], id="C" * 64)
    again = run(ctx, registry, "P12")
    assert again["sha256"] == first["sha256"]
    assert run(ctx, registry, "P14")["published"]


@pytest.mark.parametrize("when", ["staging", "acceptance"])
def test_catalog_refresh_does_not_relax_staging_or_publication_races(tmp_path, when):
    ctx, registry = reviewed_catalog_context(tmp_path)
    if when == "staging":
        real_put = ctx.share.put_file

        def put(*args, **kwargs):
            real_put(*args, **kwargs)
            ctx.templates.add("raced", "F" * 64, "race.ova", "race")

        ctx.share.put_file = put
        with pytest.raises(PermanentError, match="while staging"):
            run(ctx, registry, "P12")
    else:
        run(ctx, registry, "P12")
        ctx.templates.add("raced", "F" * 64, "race.ova", "race")
        with pytest.raises(PermanentError, match="during acceptance"):
            run(ctx, registry, "P14")
        assert ctx.share.read_text(ctx.names.folder, ctx.names.yml) is None


def test_staging_does_not_publish_and_publish_preserves_old_catalog(tmp_path):
    ctx, registry = context(tmp_path)
    old = list(ctx.templates.entries)
    staged = run(ctx, registry, "P12")
    assert ctx.tracked == [ctx.names.folder]
    assert ctx.share.read_text(ctx.names.folder, ctx.names.yml) is None
    assert ctx.templates.entries == old
    assert staged["file"] == ctx.names.ova(staged["sha256"])
    assert run(ctx, registry, "P14")["published"] is True
    assert {(e.name, e.id) for e in ctx.templates.entries} == {
        (old[0].name, old[0].id),
        (ctx.names.template, staged["sha256"].upper()),
    }
    ctx.cleanup.commit.assert_called_once()


def test_staging_waits_only_for_404_then_accepts_exact_visible_ova(tmp_path):
    ctx, registry = context(tmp_path)
    started = ctx.clock.monotonic()
    size = ctx.results["P10"]["size"]
    answers = iter(
        [HeadResult(404, 99, None), HeadResult(404), HeadResult(200, size, "bytes")]
    )
    ctx.share.head = lambda url: next(answers)
    staged = run(ctx, registry, "P12")
    assert staged["size"] == size
    assert ctx.clock.monotonic() - started == 4
    assert ctx.summary.facts["ova_visibility_wait_seconds"] == 4
    assert ctx.share.read_text(ctx.names.folder, ctx.names.yml) is None


def test_persistent_404_times_out_and_registered_cleanup_removes_only_own_stage(
    tmp_path,
):
    ctx, registry = context(tmp_path)
    started = ctx.clock.monotonic()
    ctx.share.make_folder("third-party")
    ctx.share.put_text("third-party", "keep", "other image")
    state = SimpleNamespace(data={}, save=lambda: None)
    ctx.cleanup = pipeline.CleanupStack(
        state, lambda: logging.getLogger("visibility-test"), ctx.clock, ctx.redactor
    )
    ctx.on_cleanup = lambda *args, **kwargs: ctx.cleanup.add(*args, **kwargs)
    ctx.track_share_folder = lambda folder: pipeline.PhaseContext.track_share_folder(
        ctx, folder
    )
    count = []

    def missing(url):
        count.append(url)
        return HeadResult(404)

    ctx.share.head = missing
    with pytest.raises(
        PermanentError,
        match="120 seconds: HTTP 404; Content-Length=None.*Accept-Ranges='None'",
    ):
        run(ctx, registry, "P12")
    assert ctx.clock.monotonic() - started == 120
    assert len(count) == 61
    assert ctx.cleanup.run("failure") == []
    assert not (ctx.share.root / ctx.names.folder).exists()
    assert ctx.share.read_text("third-party", "keep") == "other image"
    assert "refresh" not in ctx.templates.calls


@pytest.mark.parametrize(
    "status,length,ranges",
    [
        (200, "wrong", "bytes"),
        (200, "expected", None),
        (200, "expected", "none"),
        (403, "expected", "bytes"),
        (500, "expected", "bytes"),
    ],
)
def test_staging_never_retries_wrong_size_ranges_or_other_http_status(
    tmp_path, status, length, ranges
):
    ctx, registry = context(tmp_path)
    started = ctx.clock.monotonic()
    size = ctx.results["P10"]["size"]
    observed = HeadResult(status, size + 1 if length == "wrong" else size, ranges)
    calls = []

    def head(url):
        calls.append(url)
        return observed

    ctx.share.head = head
    with pytest.raises(
        PermanentError,
        match=f"HTTP {status}; Content-Length=.*expected {size}.*Accept-Ranges",
    ):
        run(ctx, registry, "P12")
    assert len(calls) == 1
    assert ctx.clock.monotonic() == started
    assert ctx.share.read_text(ctx.names.folder, ctx.names.yml) is None


def test_staged_bytes_are_rechecked_after_the_web_visibility_wait(tmp_path):
    ctx, registry = context(tmp_path)
    calls = []

    def head(url):
        calls.append(url)
        if len(calls) == 1:
            return HeadResult(404)
        path = (
            ctx.share.root
            / ctx.names.folder
            / ctx.names.ova(ctx.results["P10"]["sha256"])
        )
        path.write_bytes(b"X" * ctx.results["P10"]["size"])
        return HeadResult(200, ctx.results["P10"]["size"], "bytes")

    ctx.share.head = head
    with pytest.raises(PermanentError, match="changed while waiting"):
        run(ctx, registry, "P12")


@pytest.mark.parametrize("phase", ["P11", "P13"])
@pytest.mark.parametrize(
    "report",
    [
        {"passed": True, "checks": []},
        {"passed": True, "checks": [{"name": "bad", "passed": False}]},
        {"passed": False, "checks": [{"name": "good", "passed": True}]},
    ],
)
def test_failed_or_incomplete_reports_never_publish(tmp_path, phase, report):
    ctx, registry = context(tmp_path)
    run(ctx, registry, "P12")
    ctx.results[phase] = report
    with pytest.raises(PermanentError):
        run(ctx, registry, "P14")
    assert ctx.share.read_text(ctx.names.folder, ctx.names.yml) is None
    assert "refresh" not in ctx.templates.calls


def test_changed_export_cannot_be_staged(tmp_path):
    ctx, registry = context(tmp_path)
    Path(ctx.results["P10"]["path"]).write_bytes(b"different OVA")
    with pytest.raises(PermanentError, match="changed"):
        run(ctx, registry, "P12")
    assert ctx.share.list_folders() == []


def test_changed_staged_ova_cannot_be_published(tmp_path):
    ctx, registry = context(tmp_path)
    staged = run(ctx, registry, "P12")
    ctx.share.put_text(staged["folder"], staged["file"], "modified", replace=True)
    with pytest.raises(PermanentError, match="changed"):
        run(ctx, registry, "P14")
    assert ctx.share.read_text(ctx.names.folder, ctx.names.yml) is None


def test_an_existing_share_folder_is_not_changed_or_owned(tmp_path):
    ctx, registry = context(tmp_path)
    ctx.share.make_folder(ctx.names.folder)
    ctx.share.put_text(ctx.names.folder, "precious", "keep")
    with pytest.raises(AlreadyExists):
        run(ctx, registry, "P12")
    assert ctx.share.read_text(ctx.names.folder, "precious") == "keep"
    assert ctx.tracked == []


def test_publication_refuses_a_folder_with_a_different_marker(tmp_path):
    ctx, registry = context(tmp_path)
    run(ctx, registry, "P12")
    ctx.share.put_text(ctx.names.folder, names.RUN_MARKER, "foreign-run", replace=True)
    with pytest.raises(NotOurs):
        run(ctx, registry, "P14")


def test_refresh_failure_after_atomic_yaml_keeps_the_accepted_folder(tmp_path):
    ctx, registry = context(tmp_path)
    run(ctx, registry, "P12")
    ctx.templates.inject("refresh", Unreachable("temporarily down"))
    with pytest.raises(PermanentError, match="preserved folder"):
        run(ctx, registry, "P14")
    ctx.cleanup.commit.assert_called_once()
    assert ctx.share.read_text(ctx.names.folder, ctx.names.yml)


def test_catalog_changes_during_acceptance_block_publication(tmp_path):
    ctx, registry = context(tmp_path)
    run(ctx, registry, "P12")
    ctx.templates.entries.clear()
    with pytest.raises(PermanentError, match="catalog changed"):
        run(ctx, registry, "P14")
    assert ctx.share.read_text(ctx.names.folder, ctx.names.yml) is None


def test_summary_is_redacted_and_written_next_to_published_ova(tmp_path):
    ctx, registry = context(tmp_path)
    run(ctx, registry, "P12")
    run(ctx, registry, "P14")
    ctx.summary.published = True
    ctx.summary.result = "published"
    ctx.summary.facts["check-output"] = "super-secret-value"
    run(ctx, registry, "P16")
    assert status.latest(ctx.state_dir)["result"] == "published"
    assert len(status.list_builds(ctx.state_dir)) == 1
    assert "super-secret-value" not in json.dumps(status.show(ctx.state_dir, "1.3.0"))
    assert "super-secret-value" not in ctx.share.read_text(
        ctx.names.folder, "BUILD-INFO.json"
    )


def test_local_share_never_overwrites_and_requires_ownership_for_delete(tmp_path):
    share = publish.LocalShare(tmp_path, "http://store")
    share.make_folder("image")
    share.put_text("image", "file", "first")
    with pytest.raises(AlreadyExists):
        share.put_text("image", "file", "replacement")
    assert share.read_text("image", "file") == "first"
    with pytest.raises(NotOurs):
        share.remove_folder("image", marker_name=names.RUN_MARKER, marker_text="ours")
    share.put_text("image", names.RUN_MARKER, "ours")
    assert share.remove_folder(
        "image", marker_name=names.RUN_MARKER, marker_text="ours"
    )
    assert (
        share.remove_folder("image", marker_name=names.RUN_MARKER, marker_text="ours")
        is False
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX service permissions")
@pytest.mark.parametrize("transport", ["local", "ssh"])
def test_new_share_folders_and_files_are_readable_under_private_service_umask(
    tmp_path, transport
):
    def runner(argv, **kwargs):
        command = shlex.split(argv[-1])
        kwargs.pop("check", None)
        return subprocess.run(
            [sys.executable, *command[1:]], check=False, umask=0o077, **kwargs
        )

    share = (
        publish.LocalShare(tmp_path, "http://store")
        if transport == "local"
        else publish.SshShare(
            tmp_path,
            "http://store",
            host="test",
            user="builder",
            key_file=tmp_path / "key",
            runner=runner,
            sudo=False,
        )
    )
    source = tmp_path / "source.ova"
    source.write_bytes(b"OVA bytes")
    previous = os.umask(0o077)
    try:
        share.make_folder("new")
        share.put_text("new", "template.yml", "published")
        share.put_file("new", "image.ova", source)
    finally:
        os.umask(previous)
    assert (tmp_path / "new").stat().st_mode & 0o777 == 0o755
    for filename in ("template.yml", "image.ova"):
        assert (tmp_path / "new" / filename).stat().st_mode & 0o777 == 0o644
    (tmp_path / "existing").mkdir(mode=0o700)
    (tmp_path / "existing" / "keep").write_text("other image")
    with pytest.raises(AlreadyExists):
        share.make_folder("existing")
    assert (tmp_path / "existing").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "existing" / "keep").read_text() == "other image"
    (tmp_path / "link").symlink_to(tmp_path / "existing", target_is_directory=True)
    with pytest.raises(PermanentError):
        share.make_folder("link")
    assert (tmp_path / "existing").stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory descriptors")
@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_directory_replacement_before_open_cannot_chmod_another_target(
    tmp_path, monkeypatch, replacement
):
    share = publish.LocalShare(tmp_path, "http://store")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    target = tmp_path / "new"
    original_open = publish.os.open

    def racing_open(path, flags, *args, **kwargs):
        if Path(path) == target:
            target.rename(tmp_path / "created")
            if replacement == "symlink":
                target.symlink_to(outside, target_is_directory=True)
            else:
                target.mkdir(mode=0o700)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(publish.os, "open", racing_open)
    with pytest.raises(PermanentError):
        share.make_folder("new")
    assert outside.stat().st_mode & 0o777 == 0o700
    if replacement == "directory":
        assert target.stat().st_mode & 0o777 == 0o700


def test_ova_http_403_names_the_ova_server_without_guessing_authentication(tmp_path):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError(
        "http://store/image.ova", 403, "Forbidden", {}, None
    )
    with pytest.raises(
        PermanentError, match="OVA web server refused access: HTTP 403"
    ) as caught:
        publish.LocalShare(tmp_path, "http://store", opener=opener).head(
            "http://store/image.ova"
        )
    assert not isinstance(caught.value, AuthFailed)
    assert "authentication" not in str(caught.value)
    with pytest.raises(
        PermanentError, match="template service refused access: HTTP 403"
    ):
        publish.TemplateApi("http://api", opener=opener).list_templates()


def test_ova_http_401_keeps_authentication_classification_with_correct_context(
    tmp_path,
):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError(
        "http://store/image.ova", 401, "Unauthorized", {}, None
    )
    with pytest.raises(
        AuthFailed, match="OVA web server refused authentication: HTTP 401"
    ):
        publish.LocalShare(tmp_path, "http://store", opener=opener).head(
            "http://store/image.ova"
        )


@pytest.mark.parametrize("bad", ["../escape", "/absolute", "x/y", ".."])
def test_share_paths_cannot_escape_root(tmp_path, bad):
    share = publish.LocalShare(tmp_path, "http://store")
    with pytest.raises(PermanentError):
        share.make_folder(bad)


def test_template_adapter_uses_real_http_contract(tmp_path):
    fake = FakeTemplateApi()
    fake.add("base", "A" * 64, "base.ova", "base", users=["junioradmin"])
    with fake.server() as url:
        api = publish.TemplateApi(url)
        api.check()
        assert api.list_templates()[0].source_dir == "base"
        api.refresh()
    assert "refresh" in fake.calls


def test_ssh_share_streams_files_and_uses_the_same_filesystem_guards(tmp_path):
    def runner(argv, **kwargs):
        remote = shlex.split(argv[-1])
        assert remote[0] == "python3"
        kwargs.pop("check", None)
        return subprocess.run([sys.executable, *remote[1:]], check=False, **kwargs)

    share = publish.SshShare(
        tmp_path,
        "http://store",
        host="test",
        user="builder",
        key_file=tmp_path / "key",
        runner=runner,
        sudo=False,
    )
    share.make_folder("image")
    source = tmp_path / "source.ova"
    source.write_bytes(b"binary OVA\x00data")
    share.put_file("image", "image.ova", source)
    assert (
        share.sha256("image", "image.ova")
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    with pytest.raises(AlreadyExists):
        share.put_file("image", "image.ova", source)
    share.put_text("image", names.RUN_MARKER, "ours")
    assert share.remove_folder(
        "image", marker_name=names.RUN_MARKER, marker_text="ours"
    )


def test_a_failed_noop_cannot_replace_a_published_version_summary(tmp_path):
    ctx, registry = context(tmp_path)
    ctx.summary.published = True
    ctx.summary.result = "published"
    status.write_json(
        ctx.state_dir / "builds" / ctx.version / "summary.json", ctx.summary.to_dict()
    )
    ctx.summary.published = False
    ctx.summary.result = "deferred"
    run(ctx, registry, "P16")
    assert status.show(ctx.state_dir, ctx.version)["result"] == "published"
    assert status.latest(ctx.state_dir)["result"] == "deferred"


def test_share_copy_does_not_overwrite_a_file_created_mid_copy(tmp_path, monkeypatch):
    share = publish.LocalShare(tmp_path, "http://store")
    share.make_folder("image")
    real_link = publish.os.link

    def racing_writer(source, destination):
        Path(destination).write_text("the other writer's data")
        return real_link(source, destination)

    monkeypatch.setattr(publish.os, "link", racing_writer)
    with pytest.raises(AlreadyExists):
        share.put_text("image", "file", "our data")
    assert share.read_text("image", "file") == "the other writer's data"


@pytest.mark.parametrize("already_listed", [False, True])
def test_resume_reuses_only_its_verified_staged_image(tmp_path, already_listed):
    ctx, registry = context(tmp_path)
    ctx.results["P1"] = {
        "catalog": [{"name": e.name, "id": e.id} for e in ctx.templates.entries]
    }
    first = run(ctx, registry, "P12")
    ctx.share.put_text(ctx.names.folder, ctx.names.yml, first["yaml"])
    if already_listed:
        ctx.templates.refresh()
    ctx.options = pipeline.RunOptions(resume=ctx.run_id)
    second = run(ctx, registry, "P12")
    assert second["sha256"] == first["sha256"]
    assert run(ctx, registry, "P14")["published"] is True


def test_resume_refuses_an_existing_file_with_a_different_hash(tmp_path):
    ctx, registry = context(tmp_path)
    first = run(ctx, registry, "P12")
    ctx.share.put_text(ctx.names.folder, first["file"], "modified", replace=True)
    ctx.options = pipeline.RunOptions(resume=ctx.run_id)
    with pytest.raises(PermanentError, match="checksum"):
        run(ctx, registry, "P12")
