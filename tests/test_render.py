import os
import re
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
        "ova-store/Caddyfile",
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
        "ova-store",
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
        == "gateway,ginnungagap,template-esxi,collector-esxi,ova-store,setup"
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


@pytest.mark.parametrize("kind", ["local", "nfs"])
def test_optional_template_management_mounts_the_same_store_writable(
    make_config, out, generator_sources, kind
):
    config = make_config(
        templates={
            "store": {
                "type": kind,
                "manage": True,
                "nfs": {"server": "nfs.test", "export": "/templates"},
            },
            "esxi_refresh_minutes": 5,
        }
    )
    render(config, out, generator_sources)
    compose = env_of(read(out, "compose.env"))
    operator = env_of(read(out, "operator.env"))
    assert compose["COMPOSE_FILE"] == "compose.yml:compose.template-share.yml"
    assert "rw" in compose["TEMPLATE_STORE_VOLUME_OPTS"].split(",")
    assert operator["TOPOLOGYOPERATOR_TEMPLATE_SHARE_DIR"] == "/template-share"
    assert operator["TOPOLOGYOPERATOR_TEMPLATE_REFRESH_MINUTES"] == "5"

    render(make_config(), out, generator_sources)
    assert "TOPOLOGYOPERATOR_TEMPLATE_SHARE_DIR" not in env_of(
        read(out, "operator.env")
    )
    assert env_of(read(out, "compose.env"))["COMPOSE_FILE"] == "compose.yml"


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
    assert env["OVA_BASE_URL"] == "http://localhost:8088"
    assert "OVA_DEPLOY_URL" not in env
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
        services={"template_esxi": False, "generator": False, "ova_store": False},
        templates={
            "esxi_url": "http://old-host:8000/",
            "ova_base_url": "http://ova-host:80/",
        },
        generator={"url": "http://gen-host:8002"},
    )
    render(config, out)
    env = env_of(read(out, "operator.env"))
    assert env["ESXI_TEMPLATE_SERVER_URL"] == "http://old-host:8000"
    assert env["TOPOLOGY_GENERATOR_URL"] == "http://gen-host:8002"
    assert env["OVA_BASE_URL"] == "http://ova-host:80"


def test_the_gateway_routes_only_enabled_services(make_config, out, generator_sources):
    render(make_config(), out, generator_sources)
    caddy = read(out, "Caddyfile")
    for path, upstream in (
        ("/appliance/health", "setup:8000"),
        ("/templates/esxi/*", "template-esxi:8000"),
        ("/templates/gns3/*", "template-gns3:8000"),
        ("/generator/*", "generator:8000"),
        ("handle {", "ginnungagap:8123"),
    ):
        assert path in caddy
        assert upstream in caddy
    assert "auto_https off" in caddy and ":80 {" in caddy
    render(
        make_config(
            services={"generator": False},
            generator={"url": "http://g:1"},
        ),
        out,
    )
    caddy = read(out, "Caddyfile")
    assert "/generator/" not in caddy and "/templates/esxi/" in caddy
    assert "handle_path /setup/*" not in caddy


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
        [
            "Caddyfile",
            "collector-gns3/gns3_config.yml",
            "compose.env",
            "operator.env",
            "ova-store/Caddyfile",
        ]
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


def test_the_gateway_has_no_route_to_the_ova_server(
    make_config, out, generator_sources
):
    # the gateway may ask for a login or redirect to https, which an ESXi host cannot handle: the OVA server has its own port
    render(make_config(), out, generator_sources)
    assert "/ova" not in read(out, "Caddyfile")
    assert "ova-store" not in read(out, "Caddyfile")


def test_the_ova_server_gets_a_profile_its_own_caddyfile_and_a_published_port(
    make_config, out, generator_sources
):
    config = make_config(templates={"ova_port": 8090})
    written = render(config, out, generator_sources)
    assert "ova-store/Caddyfile" in written
    compose = env_of(read(out, "compose.env"))
    assert "ova-store" in compose["COMPOSE_PROFILES"].split(",")
    assert compose["OVA_PORT"] == "8090"
    assert read(out, "operator.env").count("OVA_BASE_URL=http://localhost:8090\n") == 1


def test_a_switched_off_ova_server_has_no_profile_and_no_file(
    make_config, out, generator_sources
):
    config = make_config(
        services={"ova_store": False}, templates={"ova_base_url": "http://ova-host"}
    )
    written = render(config, out, generator_sources)
    assert "ova-store/Caddyfile" not in written and not (out / "ova-store").exists()
    assert "ova-store" not in env_of(read(out, "compose.env"))["COMPOSE_PROFILES"]


@pytest.mark.parametrize(
    "public_url, expected",
    [
        ("http://10.20.20.181", "http://10.20.20.181:8088"),
        ("https://ginn.lab:8443/", "http://ginn.lab:8088"),
        ("http://[fd00::5]", "http://[fd00::5]:8088"),
    ],
)
def test_the_default_ova_address_is_the_appliance_host_on_the_ova_port(
    make_config, public_url, expected
):
    tls = "self-signed" if public_url.startswith("https") else "off"
    config = make_config(appliance={"public_url": public_url, "tls": {"mode": tls}})
    assert rd.ova_base_url(config) == expected
    assert rd.service_urls(config)["ova"] == expected


def test_a_configured_ova_address_wins_and_loses_its_trailing_slash(make_config):
    config = make_config(templates={"ova_base_url": "http://other:81/"})
    assert rd.ova_base_url(config) == "http://other:81"


def test_the_ova_servers_caddyfile_serves_only_ova_files_read_only_without_listing():
    text = rd.ova_store_caddyfile()
    assert text.startswith("# Generated by")
    assert "root * /store" in text and "file_server" in text
    assert "browse" not in text  # no directory listing
    assert "method GET HEAD" in text  # nothing that writes
    assert "reverse_proxy" not in text and "basic_auth" not in text
    assert "tls" not in text
    assert "admin off" in text and "auto_https off" in text
    # the path rule: *.ova only, no segment that starts with a dot
    pattern = re.search(r"path_regexp `([^`]*)`", text).group(1)
    served = re.compile(pattern)
    for path in ("/debian/deb12.ova", "//deb12.ova", "/a/b/Win_2022.OVA"):
        assert served.match(path), path
    for path in (
        "/debian/debian.yml",
        "/debian/",
        "/deb12.ova.bak",
        "/.hidden/deb12.ova",
        "/../deb12.ova",
        "/debian/../deb12.ova",
        "/debian/.deb12.ova",
    ):
        assert not served.match(path), path


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
