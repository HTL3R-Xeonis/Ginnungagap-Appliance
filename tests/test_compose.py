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


def test_only_the_gateway_publishes_ports(compose):
    published = {n for n, s in compose["services"].items() if "ports" in s}
    assert published == {"gateway"}


def test_the_legacy_ports_file_only_adds_ports_to_existing_services(compose):
    legacy = yaml.safe_load(
        (ROOT / "compose.legacy-ports.yml").read_text(encoding="utf-8")
    )
    assert set(legacy["services"]) <= set(compose["services"])
    assert all(set(s) == {"ports"} for s in legacy["services"].values())


def test_the_ova_deploy_service_is_its_own_container_with_the_password_as_a_secret(
    compose,
):
    service = compose["services"]["ova-deploy"]
    assert service["profiles"] == ["ova-deploy"]
    assert "depends_on" not in service
    assert service["secrets"] == ["esxi_password"]
    assert (
        compose["secrets"]["esxi_password"]["file"]
        == "./rendered/ova-deploy/esxi_password"
    )
    assert "template-store:/templates/esxi:ro" in service["volumes"]
    assert (
        "data:/host-data:ro" in service["volumes"]
    )  # the hosts' passwords, as the app stored them
    assert service["environment"]["HOST_PROFILES_DIR"] == "/host-data/settings_profiles"
    assert "/health" in " ".join(service["healthcheck"]["test"])
    assert service["build"]["additional_contexts"]["service"].endswith(
        "/TopologyBuilderServices"
    )
    assert not any(
        "PASSWORD" in str(v) for v in (service.get("environment") or {}).values()
    )


def test_services_with_a_health_endpoint_have_a_health_check(compose):
    for name in ("ginnungagap", "template-esxi", "template-gns3", "generator"):
        assert "healthcheck" in compose["services"][name], name
        assert "/health" in " ".join(compose["services"][name]["healthcheck"]["test"])


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
    everything = make_config(
        services={"ova_deploy": True}, ova_deploy={"esxi_password": "x"}
    )
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
    pyproject = ROOT.parent / "TopologyOperator" / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("TopologyOperator is not next to this repository")
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
