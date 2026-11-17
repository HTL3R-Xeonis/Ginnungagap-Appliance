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
        "setup",
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
        == "gateway,ginnungagap,template-esxi,collector-esxi,setup"
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
    assert "setup:8000" not in caddy


def test_the_setup_wizard_is_reached_through_the_same_basic_auth_as_everything_else(
    make_config, out, generator_sources
):
    config = make_config(
        appliance={
            "auth": {"mode": "basic", "user": "admin", "password_hash": "$2a$14$x"}
        }
    )
    render(config, out, generator_sources)
    caddy = read(out, "Caddyfile")
    # one basic_auth block for the whole site (see caddyfile()): /setup/* has no auth directive of its own
    assert caddy.count("basic_auth") == 1
    assert caddy.index("basic_auth") < caddy.index("/setup/*")


def test_the_env_file_carries_the_setup_wizards_host_paths(
    make_config, out, generator_sources, monkeypatch
):
    monkeypatch.delenv("GINNUNGAGAP_CONFIG_DIR", raising=False)
    monkeypatch.delenv("GINNUNGAGAP_SETUP_STAGING_DIR", raising=False)
    render(make_config(), out, generator_sources)
    env = env_of(read(out, "compose.env"))
    assert env["GINNUNGAGAP_CONFIG_DIR"] == "/etc/ginnungagap"
    assert env["GINNUNGAGAP_SETUP_STAGING_DIR"] == "/var/lib/ginnungagap/setup"
    monkeypatch.setenv("GINNUNGAGAP_CONFIG_DIR", "/custom/config")
    monkeypatch.setenv("GINNUNGAGAP_SETUP_STAGING_DIR", "/custom/staging")
    render(make_config(), out, generator_sources)
    env = env_of(read(out, "compose.env"))
    assert env["GINNUNGAGAP_CONFIG_DIR"] == "/custom/config"
    assert env["GINNUNGAGAP_SETUP_STAGING_DIR"] == "/custom/staging"


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
        "ginnungagap.lab, localhost, 127.0.0.1, :443 {" in caddy
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


def test_the_compose_project_is_ginnungagap_unless_the_environment_names_another(
    make_config, out, generator_sources, monkeypatch
):
    monkeypatch.delenv("GINNUNGAGAP_PROJECT", raising=False)
    render(make_config(), out, generator_sources)
    assert env_of(read(out, "compose.env"))["COMPOSE_PROJECT_NAME"] == "ginnungagap"
    monkeypatch.setenv("GINNUNGAGAP_PROJECT", "ginnsmoke")
    render(make_config(), out, generator_sources)
    assert env_of(read(out, "compose.env"))["COMPOSE_PROJECT_NAME"] == "ginnsmoke"


def test_an_invalid_project_name_is_refused(
    make_config, out, generator_sources, monkeypatch
):
    monkeypatch.setenv("GINNUNGAGAP_PROJECT", "Not Valid")
    with pytest.raises(RenderError, match="compose project name"):
        render(make_config(), out, generator_sources)


PASSWORD = "p'a$s\"w#rd \\x"


def ova_config(make_config):
    return make_config(
        services={"ova_deploy": True}, ova_deploy={"esxi_password": PASSWORD}
    )


def test_without_a_fallback_password_the_secret_file_is_empty_not_missing(
    make_config, out, generator_sources
):
    render(make_config(services={"ova_deploy": True}), out, generator_sources)
    assert (out / "ova-deploy" / "esxi_password").read_text(encoding="utf-8") == ""


def test_ova_deploy_as_a_container_gets_a_profile_a_route_and_the_password_as_a_file(
    make_config, out, generator_sources
):
    config = ova_config(make_config)
    written = render(config, out, generator_sources)
    assert "ova-deploy/esxi_password" in written
    secret = out / "ova-deploy" / "esxi_password"
    assert (
        secret.read_text(encoding="utf-8") == PASSWORD
    )  # exactly, whatever characters it has
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert "ova-deploy" in env_of(read(out, "compose.env"))["COMPOSE_PROFILES"].split(
        ","
    )
    assert rd.service_urls(config)["ova"] == "http://ova-deploy:8000"
    assert (
        env_of(read(out, "operator.env"))["OVA_DEPLOY_URL"] == "http://ova-deploy:8000"
    )
    assert "handle_path /ova/* {\n\t\treverse_proxy ova-deploy:8000" in read(
        out, "Caddyfile"
    )


def test_the_esxi_password_is_written_nowhere_but_its_own_file(
    make_config, out, generator_sources
):
    render(ova_config(make_config), out, generator_sources)
    for path in out.rglob("*"):
        if path.is_file() and path.name != "esxi_password":
            assert PASSWORD not in path.read_text(encoding="utf-8"), path


def test_switching_the_container_off_removes_the_password_file_again(
    make_config, out, generator_sources
):
    render(ova_config(make_config), out, generator_sources)
    render(make_config(), out, generator_sources)
    assert not (out / "ova-deploy").exists()


def test_external_ova_deploy_still_routes_to_the_configured_address(
    make_config, out, generator_sources
):
    config = make_config()
    render(config, out, generator_sources)
    assert "reverse_proxy http://10.20.20.172:8003" in read(out, "Caddyfile")
    assert "ova-deploy" not in env_of(read(out, "compose.env"))["COMPOSE_PROFILES"]


def tls_caddyfile(make_config, out, generator_sources, **appliance):
    config = make_config(
        appliance={
            "public_url": "https://ginnungagap.lab",
            "tls": {"mode": "self-signed"},
            **appliance,
        }
    )
    render(config, out, generator_sources)
    return read(out, "Caddyfile")


def test_tls_answers_a_client_that_connects_by_address_with_the_sites_certificate(
    make_config, out, generator_sources
):
    caddy = tls_caddyfile(make_config, out, generator_sources)
    assert "default_sni ginnungagap.lab" in caddy.split("ginnungagap.lab, localhost")[0]


def test_plain_http_is_sent_to_https_on_the_published_port(
    make_config, out, generator_sources
):
    standard = tls_caddyfile(make_config, out, generator_sources)
    assert "redir https://{host}{uri} 308" in standard  # 443: no port in the address
    assert (
        "auto_https disable_redirects" in standard
    )  # Caddy's own redirect would always name 443
    other = tls_caddyfile(make_config, out, generator_sources, https_port=8443)
    assert "redir https://{host}:8443{uri} 308" in other


def test_without_tls_there_is_no_redirect_and_no_name_to_expect(
    make_config, out, generator_sources
):
    render(make_config(), out, generator_sources)
    caddy = read(out, "Caddyfile")
    assert "redir" not in caddy and "default_sni" not in caddy
    assert "auto_https off" in caddy


def test_the_gateway_also_answers_for_the_machines_own_address(
    make_config, out, generator_sources
):
    """Without a site for it Caddy answers a request for 127.0.0.1 - or the VM's own IP - with an empty 200 (found on real machines)."""
    caddy = tls_caddyfile(make_config, out, generator_sources)
    assert "ginnungagap.lab, localhost, 127.0.0.1, :443 {" in caddy
    local = tls_caddyfile(
        make_config, out, generator_sources, public_url="https://localhost"
    )
    assert "\nlocalhost, 127.0.0.1, :443 {" in local  # no name twice
    ip = tls_caddyfile(
        make_config, out, generator_sources, public_url="https://10.20.20.222"
    )
    assert (
        "default_sni 10.20.20.222" in ip
        and "10.20.20.222, localhost, 127.0.0.1, :443 {" in ip
    )
