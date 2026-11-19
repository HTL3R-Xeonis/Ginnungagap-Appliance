"""The setup wizard's own logic (ginnungagap/setup_wizard.py): building, validating and rendering the config the form
submits, and the hand-off file - all without FastAPI, so this runs in any environment that can run the rest of the
test suite (see tests/test_setup_app.py for the thin FastAPI shim on top, skipped where fastapi is not installed)."""

from __future__ import annotations

import re

import pytest
import yaml

from ginnungagap import config as config_module
from ginnungagap import setup_wizard as wizard

MINIMAL = {
    "public_url": "https://ginn.lab",
    "esxi_mode": "external",
    "esxi_url": "http://10.0.0.1:8000",
    "gns3_mode": "external",
    "gns3_url": "http://10.0.0.1:8001",
    "generator_mode": "external",
    "generator_url": "http://10.0.0.1:8002",
    "ova_mode": "external",
    "ova_base_url": "http://10.0.0.1:8088",
    "store_type": "local",
    "store_local_path": "/srv/templates",
}


def normalised(values: dict[str, str], **kw) -> dict:
    raw = wizard.build_raw_config({**MINIMAL, **values}, **kw)
    return config_module.normalise(raw)


def test_a_minimal_submission_normalises_cleanly():
    config = normalised({})
    assert config["appliance"]["public_url"] == "https://ginn.lab"
    assert config["services"]["template_esxi"] is False
    assert config["templates"]["esxi_url"] == "http://10.0.0.1:8000"
    assert config["services"]["ova_store"] is False
    assert config["templates"]["ova_base_url"] == "http://10.0.0.1:8088"


def test_template_store_management_is_opt_in_and_prefilled_on_reconfigure():
    assert normalised({})["templates"]["store"]["manage"] is False
    config = normalised({"store_manage": "on"})
    assert config["templates"]["store"]["manage"] is True
    assert wizard.prefill_from(config)["store_manage"] == "on"
    assert 'name="store_manage" checked' in wizard.render_page(
        wizard.prefill_from(config)
    )


def test_bundled_services_need_no_url_and_the_url_fields_are_ignored():
    config = normalised(
        {
            "esxi_mode": "bundled",
            "esxi_url": "",  # not needed: template_esxi runs here
            "gns3_mode": "bundled",
            "gns3_url": "",
            "generator_mode": "bundled",
            "generator_url": "",
            "llm_1_name": "ollama-1",
            "llm_1_url": "http://10.0.0.5:11434",
            "llm_1_model": "m",
        },
        hash_password=lambda _pw: "$2a$ignored",
    )
    assert config["services"]["template_esxi"] is True
    assert config["services"]["template_gns3"] is True
    assert config["services"]["generator"] is True


def test_a_deliberately_broken_submission_is_rejected_with_the_normal_problem_list():
    """Leaving services external but blank makes the normal config.normalise error fire - the wizard raises no error
    of its own and adds no extra redaction: the problem list it would show is exactly the CLI's own."""
    raw = wizard.build_raw_config({**MINIMAL, "esxi_url": "", "esxi_mode": "external"})
    with pytest.raises(config_module.ConfigError) as excinfo:
        config_module.normalise(raw)
    assert any("esxi_url" in p for p in excinfo.value.problems)
    assert not any("sekret" in p for p in excinfo.value.problems)


def test_a_typed_password_is_hashed_through_the_given_function_only_when_basic_auth_is_chosen():
    calls = []

    def fake_hash(plaintext: str) -> str:
        calls.append(plaintext)
        return "$2a$14$fakehash"

    raw = wizard.build_raw_config(
        {
            **MINIMAL,
            "auth_mode": "basic",
            "auth_user": "admin",
            "auth_password": "s3cret",
        },
        hash_password=fake_hash,
    )
    assert calls == ["s3cret"]
    assert raw["appliance"]["auth"]["password_hash"] == "$2a$14$fakehash"
    config = config_module.normalise(raw)
    assert config["appliance"]["auth"]["password_hash"] == "$2a$14$fakehash"

    # auth left at "none": the hashing function is never called, even if a password field happened to be filled
    calls.clear()
    wizard.build_raw_config(
        {**MINIMAL, "auth_mode": "none", "auth_password": "ignored"},
        hash_password=fake_hash,
    )
    assert calls == []


def test_a_blank_password_with_basic_auth_fails_the_normal_bcrypt_check():
    raw = wizard.build_raw_config(
        {**MINIMAL, "auth_mode": "basic", "auth_user": "admin", "auth_password": ""}
    )
    with pytest.raises(config_module.ConfigError, match="password_hash"):
        config_module.normalise(raw)


def test_half_filled_llm_rows_are_reported_not_silently_dropped():
    raw = wizard.build_raw_config({**MINIMAL, "llm_1_name": "ollama-1"})
    with pytest.raises(config_module.ConfigError, match=r"llm_servers\[0\]"):
        config_module.normalise(raw)


def test_blank_llm_rows_are_skipped():
    config = normalised({"llm_1_name": "", "llm_1_url": "", "llm_1_model": ""})
    assert config["generator"]["llm_servers"] == []


def test_hosted_presets_need_only_the_api_key():
    config = normalised(
        {"hosted_gemini_enabled": "on", "hosted_gemini_api_key": "my-key"}
    )
    assert config["generator"]["hosted"] == {"gemini": {"api_key": "my-key"}}


def test_a_custom_hosted_service_needs_base_url_and_model_like_the_cli_does():
    raw = wizard.build_raw_config(
        {**MINIMAL, "hosted_custom_name": "my-llm", "hosted_custom_api_key": "k"}
    )
    with pytest.raises(config_module.ConfigError) as excinfo:
        config_module.normalise(raw)
    assert any("my-llm.base_url" in p for p in excinfo.value.problems)


def test_render_yaml_roundtrips_through_normalise(tmp_path):
    config = normalised({})
    text = wizard.render_yaml(config)
    again = config_module.normalise(yaml.safe_load(text))
    assert again == config
    (tmp_path / "ginnungagap.yml").write_text(text, encoding="utf-8")
    assert config_module.load(tmp_path / "ginnungagap.yml") == config


def test_write_staged_is_atomic_and_signals_last(tmp_path):
    staging = tmp_path / "staging"
    wizard.write_staged(staging, "version: 1\n")
    assert (staging / wizard.STAGED).read_text() == "version: 1\n"
    assert (staging / wizard.MARKER).exists()
    assert (staging / wizard.STAGED).stat().st_mode & 0o777 == 0o600
    # a second submission simply replaces both files, never leaves a half-written one behind
    wizard.write_staged(staging, "version: 1\nextra: true\n")
    assert "extra" in (staging / wizard.STAGED).read_text()
    assert not (staging / (wizard.STAGED + ".new")).exists()


def test_existing_config_is_none_without_a_file_and_none_for_an_invalid_one(tmp_path):
    assert wizard.existing_config(tmp_path) is None
    (tmp_path / "ginnungagap.yml").write_text("servises: {}\n", encoding="utf-8")
    assert wizard.existing_config(tmp_path) is None


def test_existing_config_normalises_a_valid_file(tmp_path, example):
    example["templates"]["store"]["local"]["path"] = str(tmp_path / "store")
    (tmp_path / "ginnungagap.yml").write_text(yaml.safe_dump(example), encoding="utf-8")
    config = wizard.existing_config(tmp_path)
    assert (
        config is not None
        and config["appliance"]["public_url"] == example["appliance"]["public_url"]
    )


def test_prefill_uses_defaults_without_an_existing_config():
    values = wizard.prefill_from(None)
    assert values["tls_mode"] == "off"
    assert values["esxi_mode"] == "bundled"
    assert "hosted_gemini_enabled" not in values


def test_prefill_never_includes_a_secret_value():
    config = config_module.normalise(
        {
            "appliance": {
                "auth": {"mode": "basic", "user": "a", "password_hash": "$2a$x"}
            },
            "generator": {
                "llm_servers": [
                    {"name": "o", "url": "http://10.0.0.1:1", "model": "m"}
                ],
                "hosted": {"gemini": {"api_key": "also-secret"}},
            },
        }
    )
    values = wizard.prefill_from(config)
    assert "also-secret" not in str(values.values())
    assert "$2a$x" not in str(values.values())
    assert values["auth_user"] == "a"
    assert (
        values["hosted_gemini_enabled"] == "on"
    )  # the fact that one is set is shown, the key itself is not


def test_render_page_shows_problems_and_escapes_user_input():
    page = wizard.render_page(
        {"public_url": "<script>alert(1)</script>"},
        problems=["appliance.public_url: must be an http:// or https:// address"],
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "must be an http" in page


def test_render_page_has_no_javascript():
    page = wizard.render_page({})
    assert "<script" not in page.lower()


def test_wizard_placeholders_do_not_suggest_example_addresses_or_settings():
    page = wizard.render_page(wizard.prefill_from(None))
    placeholders = re.findall(r'placeholder="([^"]*)"', page)
    for placeholder in placeholders:
        assert not re.search(
            r"https?://|(?:\d{1,3}\.){3}\d{1,3}|/|\b(?:example|e\.g\.|admin)\b",
            placeholder,
            re.IGNORECASE,
        )


def test_external_service_choices_reveal_their_address_fields():
    page = wizard.render_page(wizard.prefill_from(None))
    for service in ("esxi", "gns3", "generator"):
        assert f'name="{service}_mode" value="external" class="toggle"' in page
        assert f'name="{service}_url"' in page


def test_the_form_posts_to_its_own_address_so_it_works_under_setup_too():
    # Caddy's handle_path strips the /setup prefix: an absolute action="/" would land on the main app.
    page = wizard.render_page(wizard.prefill_from(None))
    assert '<form method="post" action="">' in page


def test_blank_secrets_keep_the_values_of_the_configuration_already_applied():
    existing = config_module.normalise(
        {
            "appliance": {
                "public_url": "https://ginn.lab",
                "auth": {"mode": "basic", "user": "a", "password_hash": "$2a$old"},
            },
            "services": {
                "template_esxi": False,
                "template_gns3": False,
                "generator": False,
                "ova_store": False,
            },
            "templates": {
                "esxi_url": "http://t:8000",
                "gns3_url": "http://t:8001",
                "ova_base_url": "http://t:8088",
            },
            "generator": {
                "url": "http://t:8002",
                "hosted": {"gemini": {"api_key": "old-key"}},
            },
        }
    )
    form = {
        **MINIMAL,
        "auth_mode": "basic",
        "auth_user": "a",
        "hosted_gemini_enabled": "on",
    }
    raw = wizard.keep_existing_secrets(wizard.build_raw_config(form), existing)
    config = config_module.normalise(raw)
    assert config["appliance"]["auth"]["password_hash"] == "$2a$old"
    assert config["generator"]["hosted"]["gemini"]["api_key"] == "old-key"


def test_a_typed_secret_replaces_the_existing_one_and_no_existing_config_changes_nothing():
    form = {**MINIMAL, "hosted_gemini_enabled": "on", "hosted_gemini_api_key": "new"}
    raw = wizard.build_raw_config(form)
    assert (
        wizard.keep_existing_secrets(raw, None)["generator"]["hosted"]["gemini"][
            "api_key"
        ]
        == "new"
    )
    existing = config_module.normalise(
        {
            "services": {
                "template_esxi": False,
                "template_gns3": False,
                "generator": False,
                "ova_store": False,
            },
            "templates": {
                "esxi_url": "http://t:8000",
                "gns3_url": "http://t:8001",
                "ova_base_url": "http://t:8088",
            },
            "generator": {
                "url": "http://t:8002",
                "hosted": {"gemini": {"api_key": "old"}},
            },
        }
    )
    assert (
        wizard.keep_existing_secrets(raw, existing)["generator"]["hosted"]["gemini"][
            "api_key"
        ]
        == "new"
    )


def test_the_wizard_has_no_esxi_password_field_and_ignores_a_submitted_one():
    assert "ova_esxi_password" not in wizard.render_page({})
    raw = wizard.build_raw_config({**MINIMAL, "ova_esxi_password": "typed"})
    assert "typed" not in str(raw)
    assert "ova_deploy" not in raw


def test_the_ova_server_defaults_to_running_here_on_port_8088_with_no_address():
    config = normalised({"ova_mode": "bundled", "ova_base_url": ""})
    assert config["services"]["ova_store"] is True
    assert config["templates"]["ova_port"] == 8088
    assert config["templates"]["ova_base_url"] == ""


def test_the_ova_server_port_and_address_are_taken_from_the_form():
    config = normalised(
        {
            "ova_mode": "bundled",
            "ova_port": "8090",
            "ova_base_url": " http://10.0.0.5:8090 ",
        }
    )
    assert config["templates"]["ova_port"] == 8090
    assert config["templates"]["ova_base_url"] == "http://10.0.0.5:8090"


def test_a_port_that_is_not_a_number_is_reported_by_the_config_check_not_guessed():
    raw = wizard.build_raw_config({**MINIMAL, "ova_mode": "bundled", "ova_port": "abc"})
    with pytest.raises(config_module.ConfigError, match="templates.ova_port"):
        config_module.normalise(raw)


def test_an_ova_server_elsewhere_without_an_address_is_refused():
    raw = wizard.build_raw_config({**MINIMAL, "ova_base_url": ""})
    with pytest.raises(config_module.ConfigError, match="templates.ova_base_url"):
        config_module.normalise(raw)


def test_the_ova_settings_are_prefilled_and_survive_a_round_trip():
    config = normalised({"ova_mode": "bundled", "ova_port": "8090", "ova_base_url": ""})
    values = wizard.prefill_from(config)
    assert values["ova_mode"] == "bundled" and values["ova_port"] == "8090"
    assert values["ova_base_url"] == ""
    again = config_module.normalise(wizard.build_raw_config(values))
    assert again["services"]["ova_store"] is True
    assert again["templates"]["ova_port"] == 8090


def test_the_ova_section_explains_itself_only_behind_the_information_icon():
    page = wizard.render_page(wizard.prefill_from(None))
    assert "OVA server for the ESXi hosts" in page
    start = page.index("OVA server for the ESXi hosts")
    section = page[start : page.index("</fieldset>", start)]
    assert '<details class="info">' in section and "<summary" in section
    # the explanation (reachable port, no login, LAN only) sits inside the details element and nowhere else
    outside = re.sub(r"<details.*?</details>", "", section, flags=re.DOTALL)
    assert "login" not in outside and "TCP" not in outside
    inside = re.search(r"<details.*?</details>", section, flags=re.DOTALL).group(0)
    assert "TCP" in inside and "login" in inside and "lab network" in inside
    assert "<script" not in page.lower()
    assert "title=" not in section  # no hover tooltip


def test_a_resubmitted_form_keeps_a_changed_update_section_and_adds_none_otherwise():
    existing = config_module.normalise(
        {
            "generator": {
                "llm_servers": [
                    {"name": "o1", "url": "http://ollama:11434", "model": "m"}
                ]
            },
            "update": {"channel": "prerelease", "check": "daily"},
        }
    )
    raw = wizard.keep_existing_secrets(wizard.build_raw_config(MINIMAL), existing)
    assert config_module.normalise(raw)["update"]["channel"] == "prerelease"
    untouched = config_module.normalise({"generator": existing["generator"]})
    assert "update" not in wizard.keep_existing_secrets(
        wizard.build_raw_config(MINIMAL), untouched
    )
