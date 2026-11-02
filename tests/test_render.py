import os
import stat

import pytest
import yaml

from ginnungagap import render as rd
from ginnungagap.render import RenderError, render


def read(out, name) -> str:
    return (out / name).read_text(encoding="utf-8")


@pytest.fixture
def out(tmp_path):
    return tmp_path / "rendered"


def env_of(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if line and not line.startswith("#")
    )


def test_everything_enabled_writes_every_file(make_config, out, generator_sources):
    written = render(make_config(), out, generator_sources)
    assert written == [
        "Caddyfile",
        "collector-gns3/gns3_config.yml",
        "compose.env",
        "generator.env",
        "generator/example_topology.yml",
        "generator/prompt_template.txt",
        "generator/topology_config.yml",
        "operator.env",
    ]
    for name in written:
        assert (out / name).is_file()


def test_the_profiles_follow_the_enabled_services(make_config, out, generator_sources):
    render(make_config(), out, generator_sources)
    profiles = env_of(read(out, "compose.env"))["COMPOSE_PROFILES"].split(",")
    assert profiles == [
        "gateway",
        "ginnungagap",
        "template-esxi",
        "collector-esxi",
        "template-gns3",
        "collector-gns3",
        "generator",
    ]
    render(
        make_config(
            services={"generator": False, "template_gns3": False},
            templates={"gns3_url": "http://g:8001"},
            generator={"url": "http://gen:8002"},
        ),
        out,
    )
    assert (
        env_of(read(out, "compose.env"))["COMPOSE_PROFILES"]
        == "gateway,ginnungagap,template-esxi,collector-esxi"
    )


def test_the_template_store_volume_for_a_local_folder_and_for_nfs(
    make_config, out, generator_sources
):
    render(make_config(), out, generator_sources)
    env = env_of(read(out, "compose.env"))
    assert (
        env["TEMPLATE_STORE_VOLUME_TYPE"],
        env["TEMPLATE_STORE_VOLUME_OPTS"],
        env["TEMPLATE_STORE_VOLUME_DEVICE"],
    ) == ("none", "bind,ro", "/srv/templates")
    nfs = make_config(
        templates={
            "store": {"type": "nfs", "nfs": {"server": "10.0.0.5", "export": "/mnt/t"}}
        }
    )
    render(nfs, out, generator_sources)
    env = env_of(read(out, "compose.env"))
    assert env["TEMPLATE_STORE_VOLUME_TYPE"] == "nfs"
    assert (
        "addr=10.0.0.5" in env["TEMPLATE_STORE_VOLUME_OPTS"]
        and ",ro," in env["TEMPLATE_STORE_VOLUME_OPTS"]
    )
    assert env["TEMPLATE_STORE_VOLUME_DEVICE"] == ":/mnt/t"


def test_refresh_intervals_ports_and_legacy_ports(make_config, out, generator_sources):
    render(
        make_config(templates={"esxi_refresh_minutes": 10, "gns3_refresh_minutes": 2}),
        out,
        generator_sources,
    )
    env = env_of(read(out, "compose.env"))
    assert (env["ESXI_REFRESH_SECONDS"], env["GNS3_REFRESH_SECONDS"]) == ("600", "120")
    assert env["COMPOSE_FILE"] == "compose.yml"
    render(
        make_config(appliance={"legacy_ports": True, "http_port": 8080}),
        out,
        generator_sources,
    )
    env = env_of(read(out, "compose.env"))
    assert env["COMPOSE_FILE"] == "compose.yml:compose.legacy-ports.yml"
    assert env["HTTP_PORT"] == "8080"


def test_the_app_gets_the_service_urls_and_its_data_paths(
    make_config, out, generator_sources
):
    render(make_config(), out, generator_sources)
    env = env_of(read(out, "operator.env"))
    assert env["ESXI_TEMPLATE_SERVER_URL"] == "http://template-esxi:8000"
    assert env["GNS3_TEMPLATE_SERVER_URL"] == "http://template-gns3:8000"
    assert env["TOPOLOGY_GENERATOR_URL"] == "http://generator:8000"
    assert env["OVA_DEPLOY_URL"] == "http://10.20.20.172:8003"
    assert all(
        v.startswith("/data")
        for k, v in env.items()
        if k.startswith("TOPOLOGYOPERATOR_")
    )
    assert env["TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR"] == "/data/settings_profiles"


def test_a_service_that_runs_elsewhere_is_reached_at_its_configured_address(
    make_config, out
):
    config = make_config(
        services={"template_esxi": False, "generator": False, "ova_deploy": "off"},
        templates={"esxi_url": "http://old-host:8000/"},
        generator={"url": "http://gen-host:8002"},
    )
    render(config, out)
    env = env_of(read(out, "operator.env"))
    assert env["ESXI_TEMPLATE_SERVER_URL"] == "http://old-host:8000"
    assert env["TOPOLOGY_GENERATOR_URL"] == "http://gen-host:8002"
    assert (
        env["OVA_DEPLOY_URL"] == "http://disabled.invalid"
    )  # fails loudly instead of using a built-in address


def test_the_gateway_routes_only_enabled_services(make_config, out, generator_sources):
    render(make_config(), out, generator_sources)
    caddy = read(out, "Caddyfile")
    for path, upstream in (
        ("/templates/esxi/*", "template-esxi:8000"),
        ("/templates/gns3/*", "template-gns3:8000"),
        ("/generator/*", "generator:8000"),
        ("/ova/*", "http://10.20.20.172:8003"),
        ("handle {", "ginnungagap:8123"),
    ):
        assert path in caddy
        assert upstream in caddy
    assert "auto_https off" in caddy and ":80 {" in caddy
    render(
        make_config(
            services={"generator": False, "ova_deploy": "off"},
            generator={"url": "http://g:1"},
        ),
        out,
    )
    caddy = read(out, "Caddyfile")
    assert (
        "/generator/" not in caddy
        and "/ova/" not in caddy
        and "/templates/esxi/" in caddy
    )


def test_self_signed_tls_and_basic_auth_in_the_gateway(
    make_config, out, generator_sources
):
    config = make_config(
        appliance={
            "public_url": "https://ginnungagap.lab",
            "tls": {"mode": "self-signed"},
            "auth": {
                "mode": "basic",
                "user": "admin",
                "password_hash": "$2a$14$abcdef",
            },
        }
    )
    render(config, out, generator_sources)
    caddy = read(out, "Caddyfile")
    assert (
        "ginnungagap.lab {" in caddy
        and "tls internal" in caddy
        and "auto_https off" not in caddy
    )
    assert "basic_auth {" in caddy and "admin $2a$14$abcdef" in caddy


def test_the_generator_gets_its_own_config_and_its_keys_only_in_its_env_file(
    make_config, out, generator_sources
):
    config = make_config(
        generator={
            "hosted": {
                "gemini": {"api_key": "sekret-key-123"},
                "my-llm": {
                    "api_key": "other-key",
                    "base_url": "http://h/v1",
                    "model": "m1",
                },
            }
        }
    )
    render(config, out, generator_sources)
    generated = yaml.safe_load(read(out, "generator/topology_config.yml"))
    assert generated["gns3_api_url"] == "http://template-gns3:8000"
    assert generated["template_api_url"] == "http://template-esxi:8000"
    names = [s["name"] for s in generated["ollama_servers"]]
    assert names == ["ollama-1", "ollama-2", "gemini", "my-llm"]
    gemini = generated["ollama_servers"][2]
    assert (
        gemini["type"] == "openai"
        and gemini["api_key_env"] == "GEMINI_API_KEY"
        and "generativelanguage" in gemini["base_url"]
    )
    assert generated["ollama_servers"][3]["api_key_env"] == "MY_LLM_API_KEY"
    assert env_of(read(out, "generator.env")) == {
        "GEMINI_API_KEY": "sekret-key-123",
        "MY_LLM_API_KEY": "other-key",
    }
    assert stat.S_IMODE((out / "generator.env").stat().st_mode) == 0o600
    for name in (
        n for n in render(config, out, generator_sources) if n != "generator.env"
    ):
        assert "sekret-key-123" not in read(out, name), name
    assert read(out, "generator/prompt_template.txt") == "PROMPT {request}\n"


def test_the_gns3_collector_takes_its_hosts_from_the_profiles_not_from_a_list(
    make_config, out, generator_sources
):
    render(make_config(), out, generator_sources)
    collector = yaml.safe_load(read(out, "collector-gns3/gns3_config.yml"))
    assert "servers" not in collector
    assert collector["output_file"] == "/data/gns3_templates.yml"


def test_the_generator_needs_its_prompt_and_example_files(make_config, out, tmp_path):
    with pytest.raises(RenderError, match="prompt_template.txt"):
        render(make_config(), out, tmp_path / "nowhere")
    with pytest.raises(RenderError):
        render(make_config(), out, None)


def test_files_are_rewritten_in_place_so_bind_mounts_keep_working(
    make_config, out, generator_sources
):
    render(make_config(), out, generator_sources)
    before = {
        n: os.stat(out / n).st_ino
        for n in (
            "Caddyfile",
            "compose.env",
            "operator.env",
            "generator/topology_config.yml",
        )
    }
    render(make_config(templates={"esxi_refresh_minutes": 30}), out, generator_sources)
    after = {n: os.stat(out / n).st_ino for n in before}
    assert before == after
    assert env_of(read(out, "compose.env"))["ESXI_REFRESH_SECONDS"] == "1800"


def test_a_file_that_is_no_longer_produced_is_removed(
    make_config, out, generator_sources
):
    render(make_config(), out, generator_sources)
    assert (out / "generator.env").exists()
    render(
        make_config(services={"generator": False}, generator={"url": "http://g:1"}), out
    )
    assert not (out / "generator.env").exists()
    assert not (out / "generator").exists()  # the folder went with its last file
    assert yaml.safe_load(read(out, ".manifest.json")) == sorted(
        ["Caddyfile", "collector-gns3/gns3_config.yml", "compose.env", "operator.env"]
    )


def test_every_generated_file_says_it_is_generated(make_config, out, generator_sources):
    for name in render(make_config(), out, generator_sources):
        if name.endswith((".txt",)) or name == "generator/example_topology.yml":
            continue  # copied from the generator's own repository
        assert read(out, name).startswith("# Generated by"), name


def test_service_urls_helper_matches_the_profiles(make_config):
    config = make_config()
    assert set(rd.service_urls(config)) == {"esxi", "gns3", "generator", "ova"}
    assert rd.profiles(config)[:2] == ["gateway", "ginnungagap"]
