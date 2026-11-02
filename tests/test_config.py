import copy

import pytest
import yaml

from ginnungagap import config as cfg
from ginnungagap.config import ConfigError, normalise

MINIMAL = {
    "ova_deploy": {"external_url": "http://ova:8003"},
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
    assert config["services"]["ova_deploy"] == "external"


def test_a_minimal_file_is_completed_with_defaults():
    config = normalise(copy.deepcopy(MINIMAL))
    assert config["templates"]["esxi_refresh_minutes"] == 15
    assert config["appliance"]["tls"] == {"mode": "off"}
    assert config["services"]["template_esxi"] is True


def test_every_problem_is_reported_at_once():
    found = problems(
        {
            "services": {"ova_deploy": "native"},
            "logging": {"level": "LOUD"},
            "appliance": {"http_port": 0},
        }
    )
    assert any("services.ova_deploy" in p for p in found)
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
                "ova_deploy": "container",
            },
        }
    )
    assert any("services.filebrowser" in p for p in found)
    assert any("services.ova_deploy" in p and "true" in p for p in found)


def test_ova_deploy_external_needs_its_address_but_off_does_not():
    assert any(
        "ova_deploy.external_url" in p
        for p in problems({"generator": MINIMAL["generator"]})
    )
    normalise({"generator": MINIMAL["generator"], "services": {"ova_deploy": "off"}})


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
            "ova_deploy": MINIMAL["ova_deploy"],
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
    found = problems({"ova_deploy": MINIMAL["ova_deploy"]})
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
            "ova_deploy": MINIMAL["ova_deploy"],
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
    base = {"ova_deploy": MINIMAL["ova_deploy"]}
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
            "ova_deploy": MINIMAL["ova_deploy"],
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
    assert cfg.load(path)["ova_deploy"]["external_url"] == "http://ova:8003"


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
        {"services": {"ova_deploy": ["external"]}},
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


def test_ova_deploy_as_a_container_needs_the_esxi_password():
    base = {"generator": MINIMAL["generator"], "services": {"ova_deploy": True}}
    assert any("ova_deploy.esxi_password" in p for p in problems(base))
    assert any(
        "ova_deploy.esxi_password" in p and "line break" in p
        for p in problems({**base, "ova_deploy": {"esxi_password": "a\nb"}})
    )
    config = normalise({**base, "ova_deploy": {"esxi_password": "p'a$s\"w#rd"}})
    assert config["services"]["ova_deploy"] is True


def test_the_esxi_password_is_hidden_when_the_config_is_shown():
    config = normalise(
        {
            "generator": MINIMAL["generator"],
            "services": {"ova_deploy": True},
            "ova_deploy": {"esxi_password": "sehr-geheim"},
        }
    )
    shown = cfg.redacted(config)
    assert shown["ova_deploy"]["esxi_password"] == "<hidden>"
    assert "sehr-geheim" not in str(shown)
    assert (
        config["ova_deploy"]["esxi_password"] == "sehr-geheim"
    )  # the original is untouched
