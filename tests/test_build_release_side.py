"""Release trust is checked before a build may import any VM."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ginnungagap.releases import DirectorySource
from image.build import config, names, pipeline, release
from image.build.ports import Blocked, Deferred, HeadResult, Unreachable
from tests.helpers.fake_esxi import FakeClock, FakeEsxi
from tests.helpers.fake_release import keys_of, make_release
from tests.helpers.fake_template_api import FakeTemplateApi


def context(tmp_path, source, *, allow_unsigned=False, allow_prerelease=False):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    run = state / "runs" / "test"
    run.mkdir(parents=True, exist_ok=True)
    cfg = config.BuildHostConfig(
        esxi=config.EsxiConfig(datastore="ds", network="VM Network"),
        paths=config.PathsConfig(state_dir=state, allowed_signers=keys_of(source)),
        limits=config.LimitsConfig(
            build_disk_free_gb=0, share_free_gb=0, datastore_free_gb=0
        ),
    )
    templates = FakeTemplateApi()
    templates.add(
        "ginnungagap-appliance-v1.2.1",
        "A" * 64,
        "base.ova",
        "base",
        users=["junioradmin"],
        password="never-persist-me",
    )
    esxi = FakeEsxi(host=cfg.esxi.host, datastore="ds", free_bytes=100 * 1024**3)
    facts = {}
    ctx = SimpleNamespace(
        config=cfg,
        templates=templates,
        esxi=esxi,
        share=SimpleNamespace(
            list_folders=list,
            free_bytes=lambda: 100 * 1024**3,
            url_for=lambda f, n: f"http://store/{f}/{n}",
            head=lambda u: HeadResult(200, 100, "bytes"),
        ),
        options=pipeline.RunOptions(
            allow_unsigned=allow_unsigned, allow_prerelease=allow_prerelease
        ),
        version="1.3.0",
        names=names.for_version("1.3.0"),
        run_id="1.3.0-20261120T100000Z",
        state_dir=state,
        run_dir=run,
        results={},
        clock=FakeClock(),
        note=lambda key, value: facts.__setitem__(key, value),
        subdir=lambda name: _mkdir(run / name),
    )
    ctx.result_of = lambda phase: ctx.results[phase]
    return ctx


def _mkdir(path):
    path.mkdir(exist_ok=True)
    return path


def run_phase(ctx, registry, pid):
    result = registry.get(pid).fn(ctx)
    ctx.results[pid] = result
    return result


def test_signed_offline_release_passes_all_readonly_phases(tmp_path):
    folder = make_release(tmp_path / "release", sign=True)
    ctx = context(tmp_path, folder)
    registry = pipeline.PhaseRegistry()
    release.register_phases(registry, source=DirectorySource(folder))
    for pid in ("P0", "P1", "P2", "P3"):
        run_phase(ctx, registry, pid)
    assert ctx.results["P2"]["manifest"]["appliance"]["version"] == "1.3.0"
    assert ctx.results["P2"]["signature"].startswith("SHA256:")
    assert ctx.results["P3"]["username"] == "junioradmin"
    assert "never-persist-me" not in json.dumps(ctx.results)
    assert ctx.results["P3"]["version"] == "1.2.1"


def test_unsigned_release_requires_explicit_manual_override(tmp_path):
    folder = make_release(tmp_path / "release")
    with pytest.raises(Blocked, match="signature"):
        release.verify_release_directory(folder, "1.3.0", tmp_path / "untrusted")
    doc = release.verify_release_directory(
        folder, "1.3.0", tmp_path / "untrusted", allow_unsigned=True
    )
    assert doc["signature"].startswith("none")


def test_invalid_signature_is_not_bypassed_by_unsigned_override(tmp_path):
    folder = make_release(tmp_path / "release", sign=True)
    manifest = folder / "components.json"
    manifest.write_text(manifest.read_text() + " ")
    with pytest.raises(Blocked, match="signature"):
        release.verify_release_directory(
            folder, "1.3.0", keys_of(folder), allow_unsigned=True
        )


@pytest.mark.parametrize("change", ["asset", "sums-path", "missing-sum", "version"])
def test_unsigned_does_not_bypass_integrity_or_version_checks(tmp_path, change):
    folder = make_release(tmp_path / "release")
    raw = json.loads((folder / "components.json").read_text())
    if change == "asset":
        (folder / raw["components"]["operator"]["asset"]).write_bytes(b"corrupt")
    elif change == "sums-path":
        with (folder / "SHA256SUMS").open("a") as handle:
            handle.write("0" * 64 + "  ../../not-a-release-file\n")
    elif change == "missing-sum":
        (folder / "SHA256SUMS").write_text("")
    with pytest.raises(Blocked):
        release.verify_release_directory(
            folder,
            "1.4.0" if change == "version" else "1.3.0",
            tmp_path / "trust",
            allow_unsigned=True,
        )


def test_signature_failure_prevents_tarball_download(tmp_path):
    folder = make_release(tmp_path / "release", sign=True)
    (folder / "components.json.sig").write_text("not a signature")
    ctx = context(tmp_path, folder)
    registry = pipeline.PhaseRegistry()
    release.register_phases(registry, source=DirectorySource(folder))
    run_phase(ctx, registry, "P0")
    with pytest.raises(Blocked):
        run_phase(ctx, registry, "P2")
    assert not list((ctx.run_dir / "release").glob("*.tar.gz"))


def test_poll_uses_catalog_truth_and_skips_prereleases(tmp_path):
    folder = tmp_path / "releases"
    make_release(folder / "old", appliance="1.2.1")
    make_release(folder / "next", appliance="1.3.0")
    make_release(folder / "rc", appliance="1.4.0-rc.1")
    ctx = context(tmp_path, folder)
    chosen = release.select_release(
        ctx.config, ctx.templates, source=DirectorySource(folder), clock=ctx.clock
    )
    assert chosen.version == "1.3.0"
    ctx.templates.add(names.for_version("1.3.0").template, "B" * 64, "published.ova")
    assert (
        release.select_release(
            ctx.config, ctx.templates, source=DirectorySource(folder)
        )
        is None
    )


def test_preflight_defers_before_import_when_share_is_full(tmp_path):
    folder = make_release(tmp_path / "release")
    ctx = context(tmp_path, folder)
    ctx.config = replace(
        ctx.config, limits=replace(ctx.config.limits, share_free_gb=500)
    )
    registry = pipeline.PhaseRegistry()
    release.register_phases(registry, source=DirectorySource(folder))
    with pytest.raises(Deferred, match="share"):
        run_phase(ctx, registry, "P1")


def test_base_uses_stable_older_version_and_requires_range_support(tmp_path):
    folder = make_release(tmp_path / "release")
    ctx = context(tmp_path, folder)
    ctx.templates.add("ginnungagap-appliance-v1.4.0", "B" * 64, "newer.ova")
    registry = pipeline.PhaseRegistry()
    release.register_phases(registry, source=DirectorySource(folder))
    assert run_phase(ctx, registry, "P3")["version"] == "1.2.1"
    ctx.share.head = lambda _: HeadResult(200, 100, None)
    with pytest.raises(Blocked, match="byte ranges"):
        run_phase(ctx, registry, "P3")


def test_transient_datastore_failure_is_a_preflight_deferral(tmp_path):
    folder = make_release(tmp_path / "release")
    ctx = context(tmp_path, folder)
    ctx.esxi.inject("datastore_free_bytes", Unreachable("host unavailable"))
    registry = pipeline.PhaseRegistry()
    release.register_phases(registry, source=DirectorySource(folder))
    with pytest.raises(Deferred, match="preflight"):
        run_phase(ctx, registry, "P1")


def test_share_requirement_uses_three_times_last_image_size(tmp_path):
    folder = make_release(tmp_path / "release")
    ctx = context(tmp_path, folder)
    ctx.templates.entries[0] = replace(ctx.templates.entries[0], raw={"size": "4,00GB"})
    ctx.share.free_bytes = lambda: 11 * 1024**3
    registry = pipeline.PhaseRegistry()
    release.register_phases(registry, source=DirectorySource(folder))
    with pytest.raises(Deferred, match="share"):
        run_phase(ctx, registry, "P1")
