"""
The compose file against the principle that every service stays separate (own container, own profile, no dependency on
another service, so each one can be started and tested alone) and against what the renderer really writes.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ginnungagap.render import render, service_urls

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))


def test_every_service_has_exactly_its_own_profile(compose):
    for name, service in compose["services"].items():
        assert service["profiles"] == [name], name


def test_no_service_depends_on_another(compose):
    for name, service in compose["services"].items():
        assert "depends_on" not in service, f"{name} must stay startable alone"
        assert "links" not in service and "network_mode" not in service, name


def test_only_the_gateway_and_the_ova_server_publish_ports(compose):
    published = {n for n, s in compose["services"].items() if "ports" in s}
    assert published == {"gateway", "ova-store"}


def test_the_legacy_ports_file_only_adds_ports_to_existing_services(compose):
    legacy = yaml.safe_load(
        (ROOT / "compose.legacy-ports.yml").read_text(encoding="utf-8")
    )
    assert set(legacy["services"]) <= set(compose["services"])
    assert all(set(s) == {"ports"} for s in legacy["services"].values())


def test_the_ova_server_is_its_own_read_only_container_on_its_own_port(compose):
    service = compose["services"]["ova-store"]
    assert service["profiles"] == ["ova-store"]
    assert "depends_on" not in service
    # the one service that is published without the gateway: ESXi hosts cannot log in, so it must not sit behind it
    assert service["ports"] == ["${OVA_PORT:-8088}:80"]
    # the store and its own rendered Caddyfile, both read-only; nothing else (no app data, no host profiles, no secrets)
    assert sorted(service["volumes"]) == [
        "./rendered/ova-store/Caddyfile:/etc/caddy/Caddyfile:ro",
        "template-store:/store:ro",
    ]
    assert "secrets" not in service and "environment" not in service
    assert "/health" in " ".join(service["healthcheck"]["test"])
    assert "ova-deploy" not in compose["services"]


def test_the_legacy_ports_file_has_no_port_for_the_removed_ova_deploy_service():
    text = (ROOT / "compose.legacy-ports.yml").read_text(encoding="utf-8")
    assert "ova-deploy" not in text and "8003" not in text


def test_services_with_a_health_endpoint_have_a_health_check(compose):
    for name in ("ginnungagap", "template-esxi", "template-gns3", "generator", "setup"):
        assert "healthcheck" in compose["services"][name], name
        assert "/health" in " ".join(compose["services"][name]["healthcheck"]["test"])


def test_the_setup_wizard_is_its_own_container_with_no_extra_privilege(compose):
    service = compose["services"]["setup"]
    assert service["profiles"] == ["setup"]
    assert "depends_on" not in service and "ports" not in service
    volumes = service["volumes"]
    assert any(
        v.endswith(":/etc/ginnungagap:ro") for v in volumes
    )  # read-only: show, never write
    staging = next(v for v in volumes if v.endswith(":/staging"))
    assert not staging.endswith(
        ":ro"
    )  # the one writable path it has: the staging folder
    assert "docker.sock" not in " ".join(volumes)
    assert service["build"]["additional_contexts"]["caddy"] == "docker-image://caddy:2"


def test_the_operator_may_read_the_update_state_and_write_nothing_but_one_request(
    compose,
):
    volumes = compose["services"]["ginnungagap"]["volumes"]
    state = next(v for v in volumes if v.endswith(":/update-state:ro"))
    assert (
        "GINNUNGAGAP_UPDATE_DIR" in state
    )  # the state is read-only: show, never change
    requests = next(v for v in volumes if v.endswith(":/update-requests"))
    assert "GINNUNGAGAP_UPDATE_REQUEST_DIR" in requests and not requests.endswith(":ro")
    assert "docker.sock" not in " ".join(volumes)


def test_the_store_is_mounted_read_only_and_the_collector_data_read_only_for_the_apis(
    compose,
):
    for name in ("template-esxi", "collector-esxi"):
        assert "template-store:/store:ro" in compose["services"][name]["volumes"]
    for name in ("template-esxi", "template-gns3"):
        assert "template-data:/data:ro" in compose["services"][name]["volumes"]
    assert (
        "data:/host-data:ro" in compose["services"]["collector-gns3"]["volumes"]
    )  # host profiles: read only


def test_dockerfiles_only_use_build_contexts_the_compose_file_defines(compose):
    for name, service in compose["services"].items():
        build = service.get("build")
        if (
            not isinstance(build, dict)
            or "dockerfile" not in build
            or build.get("context") != "."
        ):
            continue
        text = (ROOT / build["dockerfile"]).read_text(encoding="utf-8")
        used = set(re.findall(r"--from=([a-z0-9_-]+)", text))
        assert used <= set(build.get("additional_contexts", {})), (name, used)


def test_the_files_dockerfiles_copy_from_the_bundle_exist(compose):
    for dockerfile in (ROOT / "docker").glob("*.Dockerfile"):
        for source in re.findall(
            r"^COPY (docker/\S+)",
            dockerfile.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        ):
            assert (ROOT / source).is_file(), (dockerfile.name, source)


def test_every_rendered_file_the_compose_file_reads_is_written_by_the_renderer(
    compose, make_config, generator_sources, tmp_path
):
    out = tmp_path / "rendered"
    written = set(render(make_config(), out, generator_sources))
    wanted = set()
    for service in compose["services"].values():
        for entry in service.get("env_file", []):
            wanted.add(entry["path"] if isinstance(entry, dict) else entry)
        for volume in service.get("volumes", []):
            if isinstance(volume, str) and volume.startswith("./rendered/"):
                wanted.add(volume.split(":")[0])
    for path in wanted:
        relative = path.removeprefix("./").removeprefix("rendered/")
        assert relative in written or any(
            w.startswith(relative + "/") for w in written
        ), path


def test_every_variable_the_compose_file_uses_is_provided_or_has_a_default(
    compose, make_config, generator_sources, tmp_path
):
    out = tmp_path / "rendered"
    render(make_config(), out, generator_sources)
    provided = {
        line.split("=", 1)[0]
        for line in (out / "compose.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    text = (ROOT / "compose.yml").read_text(encoding="utf-8")
    for name, default in re.findall(r"\$\{([A-Z_]+)(:-[^}]*)?\}", text):
        assert name in provided or default or name == "REPOS_DIR", name


def test_the_profiles_the_renderer_selects_all_exist_in_the_compose_file(
    compose, make_config
):
    from ginnungagap.render import profiles

    declared = {p for s in compose["services"].values() for p in s["profiles"]}
    everything = make_config()
    assert set(profiles(make_config())) <= declared
    assert (
        set(profiles(everything)) == declared
    )  # and with everything on, every service is covered


def test_the_urls_between_services_use_the_compose_service_names_and_container_ports(
    compose, make_config
):
    urls = service_urls(make_config())
    assert (
        urls["esxi"] == "http://template-esxi:8000"
        and "template-esxi" in compose["services"]
    )
    assert (
        urls["gns3"] == "http://template-gns3:8000"
        and "template-gns3" in compose["services"]
    )
    assert (
        urls["generator"] == "http://generator:8000"
        and "generator" in compose["services"]
    )


def test_the_requirements_script_leaves_out_topologybuilder_and_keeps_the_pins(
    tmp_path,
):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\ndependencies = [\n "topologybuilder @ git+https://example/tb.git@dev",\n "fastapi==0.1.0",\n "uvicorn[standard]==0.2.0",\n]\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "docker" / "operator_requirements.py"),
            str(pyproject),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == ["fastapi==0.1.0", "uvicorn[standard]==0.2.0"]


def test_the_requirements_script_on_the_real_app_if_it_is_next_to_this_repository():
    # the sibling folder is called Igdrasyl (the old folder name works as well)
    pyproject = next(
        (
            ROOT.parent / folder / "pyproject.toml"
            for folder in ("Igdrasyl", "TopologyOperator")
            if (ROOT.parent / folder / "pyproject.toml").is_file()
        ),
        ROOT.parent / "Igdrasyl" / "pyproject.toml",
    )
    if not pyproject.is_file():
        pytest.skip("Igdrasyl is not next to this repository")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "docker" / "operator_requirements.py"),
            str(pyproject),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = result.stdout.split()
    assert lines and not any(line.startswith("topologybuilder") for line in lines)
    assert any(line.startswith("fastapi==") for line in lines)
