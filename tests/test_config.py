import copy

import pytest
import yaml

from ginnungagap import config as cfg
from ginnungagap.config import ConfigError, normalise

MINIMAL = {
    "generator": {
        "llm_servers": [{"name": "o1", "url": "http://ollama:11434", "model": "m"}]
    },
}


def problems(raw) -> list[str]:
    with pytest.raises(ConfigError) as exc_info:
        normalise(raw)
    return exc_info.value.problems


def test_the_example_file_is_valid(example):
    config = normalise(example)
    assert config["version"] == 1
    assert config["services"]["ova_store"] is True
    assert config["templates"]["ova_port"] == 8088
    assert config["templates"]["ova_base_url"] == ""


def test_a_minimal_file_is_completed_with_defaults():
    config = normalise(copy.deepcopy(MINIMAL))
    assert config["templates"]["esxi_refresh_minutes"] == 15
    assert config["appliance"]["tls"] == {"mode": "off"}
    assert config["services"]["template_esxi"] is True


def test_every_problem_is_reported_at_once():
    found = problems(
        {
            "services": {"ova_store": "native"},
            "logging": {"level": "LOUD"},
            "appliance": {"http_port": 0},
        }
    )
    assert any("services.ova_store" in p for p in found)
    assert any("logging.level" in p for p in found)
    assert any("appliance.http_port" in p for p in found)
    assert len(found) >= 3


def test_a_misspelled_setting_is_named():
    found = problems({"servises": {}, "appliance": {"timezon": "UTC"}})
    assert any("servises: unknown setting" in p for p in found)
    assert any("appliance.timezon: unknown setting" in p for p in found)


def test_the_file_must_be_a_mapping():
    assert "mapping" in problems(["not", "a", "mapping"])[0]


def test_an_unsupported_version_is_refused():
    assert any(p.startswith("version:") for p in problems({**MINIMAL, "version": 2}))


def test_services_that_do_not_exist_yet_are_refused_not_ignored():
    found = problems(
        {
            **MINIMAL,
            "services": {
                "filebrowser": True,
            },
        }
    )
    assert any("services.filebrowser" in p for p in found)


def test_the_ova_server_runs_here_by_default_and_needs_no_address():
    config = normalise(copy.deepcopy(MINIMAL))
    assert config["services"]["ova_store"] is True


def test_an_ova_server_that_runs_elsewhere_needs_the_address_the_esxi_hosts_use():
    base = {**MINIMAL, "services": {"ova_store": False}}
    found = problems(base)
    assert any(
        "templates.ova_base_url" in p and "ova_store is false" in p for p in found
    )
    config = normalise({**base, "templates": {"ova_base_url": "http://10.0.0.9:80"}})
    assert config["templates"]["ova_base_url"] == "http://10.0.0.9:80"


def test_the_ova_base_url_and_port_are_checked():
    found = problems(
        {**MINIMAL, "templates": {"ova_base_url": "ftp://x", "ova_port": 0}}
    )
    assert any("templates.ova_base_url: must be an http" in p for p in found)
    assert any("templates.ova_port: must be a port number" in p for p in found)
    assert any(
        "templates.ova_port" in p
        for p in problems({**MINIMAL, "templates": {"ova_port": "8088"}})
    )


def test_the_ova_port_may_not_collide_with_a_port_the_appliance_publishes():
    assert any(
        "templates.ova_port: 80 is already used" in p
        for p in problems({**MINIMAL, "templates": {"ova_port": 80}})
    )
    assert any(
        "templates.ova_port: 8123" in p
        for p in problems(
            {
                **MINIMAL,
                "appliance": {"legacy_ports": True},
                "templates": {"ova_port": 8123},
            }
        )
    )
    normalise({**MINIMAL, "templates": {"ova_port": 8123}})  # fine without legacy ports


def test_the_removed_ova_deploy_settings_are_refused_with_the_way_forward():
    found = problems({**MINIMAL, "ova_deploy": {"external_url": "http://x:8003"}})
    assert len(found) == 1 and found[0].startswith("ova_deploy: was removed")
    assert "services.ova_store" in found[0]
    found = problems({**MINIMAL, "services": {"ova_deploy": "external"}})
    assert len(found) == 1 and found[0].startswith("services.ova_deploy: was removed")


def test_a_switched_off_service_needs_the_address_of_the_one_that_runs_elsewhere():
    found = problems(
        {
            **MINIMAL,
            "services": {
                "template_esxi": False,
                "template_gns3": False,
                "generator": False,
            },
        }
    )
    assert any("templates.esxi_url" in p for p in found)
    assert any("templates.gns3_url" in p for p in found)
    assert any("generator.url" in p for p in found)
    config = normalise(
        {
            "services": {
                "template_esxi": False,
                "template_gns3": False,
                "generator": False,
            },
            "templates": {"esxi_url": "http://e:8000", "gns3_url": "http://g:8001"},
            "generator": {"url": "http://gen:8002"},
        }
    )
    assert (
        config["services"]["generator"] is False
    )  # no LLM server needed for a generator that does not run here


def test_the_generator_needs_an_llm_server_only_while_it_runs():
    found = problems({})
    assert any("at least one LLM server" in p for p in found)


def test_tls_and_auth_rules():
    assert any(
        "https://" in p
        for p in problems({**MINIMAL, "appliance": {"tls": {"mode": "self-signed"}}})
    )
    assert any(
        "tls.mode" in p
        for p in problems({**MINIMAL, "appliance": {"tls": {"mode": "acme"}}})
    )
    found = problems(
        {
            **MINIMAL,
            "appliance": {
                "auth": {"mode": "basic", "user": "admin", "password_hash": "plaintext"}
            },
        }
    )
    assert any("bcrypt" in p for p in found)
    config = normalise(
        {
            **MINIMAL,
            "appliance": {
                "public_url": "https://g.lab",
                "tls": {"mode": "self-signed"},
                "auth": {"mode": "basic", "user": "a", "password_hash": "$2a$14$abc"},
            },
        }
    )
    assert config["appliance"]["auth"]["mode"] == "basic"


def test_store_settings_are_checked_per_type():
    assert any(
        "local.path" in p
        for p in problems(
            {
                **MINIMAL,
                "templates": {
                    "store": {"type": "local", "local": {"path": "relative"}}
                },
            }
        )
    )
    found = problems(
        {
            **MINIMAL,
            "templates": {
                "store": {
                    "type": "nfs",
                    "nfs": {"server": "bad host", "export": "no-slash"},
                }
            },
        }
    )
    assert any("nfs.server" in p for p in found) and any(
        "nfs.export" in p for p in found
    )
    normalise(
        {
            **MINIMAL,
            "templates": {
                "store": {
                    "type": "nfs",
                    "nfs": {"server": "10.0.0.5", "export": "/mnt/t"},
                }
            },
        }
    )


def test_llm_server_entries_are_checked():
    found = problems(
        {
            "generator": {
                "llm_servers": [
                    {"name": "a", "url": "ftp://x"},
                    "text",
                    {"name": "a", "url": "http://x:1", "model": "m", "color": "red"},
                ]
            },
        }
    )
    assert any("llm_servers[0].url" in p for p in found)
    assert any("llm_servers[0].model" in p for p in found)
    assert any("llm_servers[1]" in p for p in found)
    assert any("unknown setting color" in p for p in found)
    assert any("'a' is used twice" in p for p in found)


def test_hosted_services_use_presets_or_need_their_own_address():
    base: dict = {}
    normalise({**base, "generator": {"hosted": {"gemini": {"api_key": "k"}}}})
    found = problems({**base, "generator": {"hosted": {"myllm": {"api_key": "k"}}}})
    assert any("myllm.base_url" in p for p in found) and any(
        "myllm.model" in p for p in found
    )
    normalise(
        {
            **base,
            "generator": {
                "hosted": {
                    "myllm": {"api_key": "k", "base_url": "http://h/v1", "model": "m"}
                }
            },
        }
    )
    assert any(
        "api_key" in p
        for p in problems({**base, "generator": {"hosted": {"gemini": {}}}})
    )
    assert any(
        "one word" in p
        for p in problems(
            {**base, "generator": {"hosted": {"gemini": {"api_key": "two words"}}}}
        )
    )


def test_redacted_hides_keys_and_hashes_without_touching_the_original():
    config = normalise(
        {
            "appliance": {
                "public_url": "https://g.lab",
                "tls": {"mode": "self-signed"},
                "auth": {
                    "mode": "basic",
                    "user": "a",
                    "password_hash": "$2a$14$secrethash",
                },
            },
            "generator": {"hosted": {"gemini": {"api_key": "sekret-key"}}},
        }
    )
    shown = yaml.safe_dump(cfg.redacted(config))
    assert (
        "sekret-key" not in shown and "secrethash" not in shown and "<hidden>" in shown
    )
    assert config["generator"]["hosted"]["gemini"]["api_key"] == "sekret-key"


def test_load_reports_a_missing_file_and_broken_yaml(tmp_path):
    with pytest.raises(ConfigError, match="can't be read"):
        cfg.load(tmp_path / "nope.yml")
    broken = tmp_path / "broken.yml"
    broken.write_text("a: [unclosed")
    with pytest.raises(ConfigError, match="isn't valid YAML"):
        cfg.load(broken)


def test_load_reads_a_file(tmp_path):
    path = tmp_path / "g.yml"
    path.write_text(yaml.safe_dump(MINIMAL))
    assert cfg.load(path)["generator"]["llm_servers"][0]["name"] == "o1"


def test_an_unquoted_off_and_a_section_with_only_comments_work():
    # YAML reads `mode: off` as false and `hosted:` followed only by comments as null
    config = cfg.normalise(
        yaml.safe_load(
            "appliance:\n  tls: { mode: off }\n"
            + yaml.safe_dump(MINIMAL)
            + "\ntemplates:\n"
        )
    )
    assert config["appliance"]["tls"]["mode"] == "off"
    assert config["templates"]["store"]["type"] == "local"
    hosted = cfg.normalise(
        {**MINIMAL, "generator": {**MINIMAL["generator"], "hosted": None}}
    )
    assert hosted["generator"]["hosted"] == {}


def test_a_typo_does_not_hide_the_other_problems():
    found = problems(
        {"servises": {}, "services": {"monitoring": True}, "logging": {"level": "LOUD"}}
    )
    assert any("servises" in p for p in found)
    assert any("services.monitoring: monitoring is not part" in p for p in found)
    assert any("logging.level" in p for p in found)


@pytest.mark.parametrize(
    "broken",
    [
        {"appliance": {"tls": "yes"}},
        {"templates": {"store": "here"}},
        {"generator": {"llm_servers": 5}},
        {"services": {"ova_store": ["yes"]}},
    ],
)
def test_a_wrongly_shaped_value_is_a_problem_not_a_crash(broken):
    assert problems({**MINIMAL, **broken})


def test_monitoring_is_not_part_of_the_solution_and_says_so():
    found = problems({**MINIMAL, "services": {"monitoring": True}})
    assert found == [
        "services.monitoring: monitoring is not part of the one-VM solution (it runs separately)"
    ]
    assert problems(
        {**MINIMAL, "services": {"monitoring": False}}
    )  # not even "false": the setting does not exist


# ---- the update section (`ginnungagap update`): public sources, an optional token that never lives in this file


def test_the_update_section_has_public_defaults_and_needs_no_token():
    config = normalise(copy.deepcopy(MINIMAL))
    update = config["update"]
    assert update["source"] == "github"
    assert update["repo"] == "HTL3R-Xeonis/Ginnungagap-Appliance"
    assert update["channel"] == "stable" and update["check"] == "off"
    assert update["require_signature"] == "auto"
    assert update["keep_releases"] == 2 and update["step_timeout_seconds"] == 90
    assert update["token_file"] == "/etc/ginnungagap/update-token"
    assert (
        update["api"] == "auto"
    )  # the latest stable release without GitHub's rate-limited API
    assert not any(
        "token" in problem for problem in problems({"update": {"source": "x"}})
    )


def test_a_missing_token_file_is_not_a_problem_and_an_empty_one_means_no_token(
    tmp_path,
):
    assert normalise({**MINIMAL, "update": {"token_file": str(tmp_path / "none")}})
    assert (
        normalise({**MINIMAL, "update": {"token_file": ""}})["update"]["token_file"]
        == ""
    )


def test_the_example_file_documents_the_update_section_with_its_defaults(example):
    assert example["update"]["source"] == "github"
    config = normalise(example)
    assert {
        k: v for k, v in config["update"].items() if k in example["update"]
    } == example["update"] | {"check": "off"}


def test_yaml_booleans_are_read_the_way_people_write_them(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(
        "update:\n  check: off\n  require_signature: true\n"
        "generator:\n  llm_servers:\n    - {name: o1, url: 'http://ollama:11434', model: m}\n"
    )
    update = cfg.load(path)["update"]
    assert update["check"] == "off" and update["require_signature"] is True
    path.write_text(
        path.read_text().replace(
            "require_signature: true", "require_signature: 'false'"
        )
    )
    assert cfg.load(path)["update"]["require_signature"] is False


@pytest.mark.parametrize("value", ["auto", True, False])
def test_every_signature_mode_is_accepted(value):
    assert (
        normalise({**MINIMAL, "update": {"require_signature": value}})["update"][
            "require_signature"
        ]
        is value
    )


@pytest.mark.parametrize(
    "change, key",
    [
        ({"source": "ftp"}, "update.source"),
        ({"repo": "not a repo"}, "update.repo"),
        ({"repo": ""}, "update.repo"),
        ({"source": "directory"}, "update.directory"),
        ({"source": "directory", "directory": "relative/dir"}, "update.directory"),
        ({"channel": "nightly"}, "update.channel"),
        ({"check": "hourly"}, "update.check"),
        ({"api": "sometimes"}, "update.api"),
        ({"api": True}, "update.api"),
        ({"check": True}, "update.check"),
        ({"require_signature": "maybe"}, "update.require_signature"),
        ({"keep_releases": 1}, "update.keep_releases"),
        ({"keep_releases": "2"}, "update.keep_releases"),
        ({"step_timeout_seconds": 0}, "update.step_timeout_seconds"),
        ({"step_timeout_seconds": True}, "update.step_timeout_seconds"),
        ({"token_file": "relative-token"}, "update.token_file"),
        ({"token_file": 5}, "update.token_file"),
    ],
)
def test_a_bad_update_setting_is_named(change, key):
    assert any(p.startswith(key) for p in problems({**MINIMAL, "update": change}))


def test_the_directory_source_works_with_an_absolute_folder():
    config = normalise(
        {**MINIMAL, "update": {"source": "directory", "directory": "/srv/releases/v1"}}
    )
    assert config["update"]["source"] == "directory" and config["update"]["repo"]


@pytest.mark.parametrize("name", ["token", "access_token", "github_token", "api_key"])
def test_a_token_is_refused_in_the_file_with_a_pointer_to_set_token(name):
    found = problems({**MINIMAL, "update": {name: "ghp_secret_value"}})
    assert any(f"update.{name}" in p and "--set-token" in p for p in found)
    assert not any(
        "ghp_secret_value" in p for p in found
    )  # the value is never echoed back


def test_a_misspelled_update_setting_is_named_and_the_section_must_be_a_mapping():
    assert any(
        "update.chanel: unknown setting" in p
        for p in problems({**MINIMAL, "update": {"chanel": "stable"}})
    )
    assert any(
        "update: must be a mapping" in p for p in problems({**MINIMAL, "update": ["x"]})
    )


def test_an_update_section_with_only_comments_keeps_the_defaults():
    assert normalise({**MINIMAL, "update": None})["update"] == cfg.DEFAULTS["update"]


def test_the_timer_condition_is_true_only_for_a_valid_file_that_says_daily(tmp_path):
    path = tmp_path / "c.yml"
    base = "generator:\n  llm_servers:\n    - {name: o1, url: 'http://ollama:11434', model: m}\n"
    assert cfg.update_check_is_daily(path) is False  # no file
    path.write_text(base)
    assert cfg.update_check_is_daily(path) is False
    path.write_text(base + "update: {check: daily}\n")
    assert cfg.update_check_is_daily(path) is True
    assert cfg.main(["update-check-daily", str(path)]) == 0
    path.write_text(base + "update: {check: daily, channel: nope}\n")
    assert (
        cfg.main(["update-check-daily", str(path)]) == 1
    )  # an invalid file never starts a check
    assert cfg.main(["nonsense"]) == 2


def test_the_update_section_is_shown_unredacted_because_it_holds_no_secret():
    shown = cfg.redacted(normalise(copy.deepcopy(MINIMAL)))
    assert shown["update"]["repo"] == "HTL3R-Xeonis/Ginnungagap-Appliance"
