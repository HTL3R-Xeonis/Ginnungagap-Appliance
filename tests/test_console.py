"""The console login text: one composer, the sshd port, the shadow state."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ginnungagap import console

VALID_CONFIG = {
    "appliance": {"public_url": "http://ginn.lab"},
    "generator": {"llm_servers": [{"name": "o", "url": "http://10.0.0.1:11434", "model": "m"}]},
}  # fmt: skip


def sshd(tmp_path: Path, main: str = "", dropins: dict[str, str] | None = None) -> Path:
    ssh = tmp_path / "ssh"
    (ssh / "sshd_config.d").mkdir(parents=True)
    (ssh / "sshd_config").write_text(main, encoding="utf-8")
    for name, text in (dropins or {}).items():
        (ssh / "sshd_config.d" / name).write_text(text, encoding="utf-8")
    return ssh


def test_the_port_is_22_without_any_setting(tmp_path):
    assert console.ssh_port(sshd(tmp_path, "PermitRootLogin no\n")) == 22
    assert console.ssh_port(tmp_path / "nothing-there") == 22


def test_a_port_line_of_the_main_file_is_used(tmp_path):
    assert console.ssh_port(sshd(tmp_path, "Port 2222\n")) == 2222
    assert console.ssh_port(sshd(tmp_path / "b", "port=2200\n")) == 2200


def test_comments_and_other_values_are_ignored(tmp_path):
    ssh = sshd(tmp_path, "#Port 2222\n   # Port 3333\nListenAddress 0.0.0.0:4444\n")
    assert console.ssh_port(ssh) == 22


def test_the_first_port_wins_and_includes_are_read_where_they_stand(tmp_path):
    main = "Include /etc/ssh/sshd_config.d/*.conf\nPort 2222\n"
    assert console.ssh_port(sshd(tmp_path, main, {"50-x.conf": "Port 3022\n"})) == 3022
    # an Include below the main file's own Port comes second
    late = "Port 2222\nInclude sshd_config.d/*.conf\n"
    assert (
        console.ssh_port(sshd(tmp_path / "b", late, {"50-x.conf": "Port 3022\n"}))
        == 2222
    )


def test_drop_ins_are_read_in_name_order(tmp_path):
    main = "Include /etc/ssh/sshd_config.d/*.conf\n"
    ssh = sshd(tmp_path, main, {"20-b.conf": "Port 2020\n", "10-a.conf": "Port 1010\n"})
    assert console.ssh_port(ssh) == 1010


def test_ports_that_are_not_numbers_or_out_of_range_are_skipped(tmp_path):
    assert (
        console.ssh_port(sshd(tmp_path, "Port abc\nPort 0\nPort 70000\nPort 2022\n"))
        == 2022
    )


def test_nothing_after_a_match_block_counts(tmp_path):
    assert console.ssh_port(sshd(tmp_path, "Match User x\nPort 2222\n")) == 22


def test_a_cyclic_include_does_not_hang(tmp_path):
    ssh = sshd(tmp_path, "Include sshd_config\nPort 2022\n")
    assert console.ssh_port(ssh) == 2022


SHADOW = "root:*:19000:0:99999:7:::\njunioradmin:$6$x$y:{last}:0:99999:7:::\nother:!:0:0:99999:7:::\n"


@pytest.mark.parametrize("last,changed", [("0", False), ("20400", True), ("", True)])
def test_the_password_counts_as_changed_unless_its_last_change_is_zero(
    tmp_path, last, changed
):
    shadow = tmp_path / "shadow"
    shadow.write_text(SHADOW.format(last=last), encoding="utf-8")
    assert console.shadow_password_changed("junioradmin", shadow) is changed


def test_an_unknown_account_or_an_unreadable_shadow_never_shows_a_password(tmp_path):
    shadow = tmp_path / "shadow"
    shadow.write_text(SHADOW.format(last="0"), encoding="utf-8")
    assert console.shadow_password_changed("nobody-here", shadow) is True
    assert console.shadow_password_changed("junioradmin", tmp_path / "missing") is True


def test_the_access_state_is_private_and_round_trips(tmp_path):
    state = tmp_path / "state"
    assert console.read_access(state) is None
    console.write_access(state, "generated", "pw")
    assert (state / console.ACCESS).stat().st_mode & 0o777 == 0o600
    assert console.read_access(state) == {"mode": "generated", "password": "pw"}
    (state / console.ACCESS).write_text("{not json", encoding="utf-8")
    assert console.read_access(state) is None
    (state / console.ACCESS).write_text('{"mode": "other"}', encoding="utf-8")
    assert console.read_access(state) is None


def refresh(tmp_path, address="192.0.2.5", user="junioradmin", changed=False):
    issue = tmp_path / "issue.d" / "ginnungagap.issue"
    lines = console.refresh(
        tmp_path / "etc",
        state_dir=tmp_path / "state",
        issue_file=issue,
        ssh_dir=tmp_path / "ssh",
        local_address_fn=lambda: address,
        login_user_fn=lambda: user,
        password_changed=lambda u: changed,
        release_fn=lambda: "1.3.0",
    )
    return lines, issue.read_text(encoding="utf-8")


def test_the_ssh_port_of_a_changed_sshd_config_is_shown(tmp_path):
    sshd(tmp_path, "Port 2222\n")
    lines, _ = refresh(tmp_path)
    assert lines[0] == "SSH: ssh junioradmin@192.0.2.5 -p 2222"


def test_the_composer_is_the_only_writer_and_ends_the_text_with_blank_lines(tmp_path):
    (tmp_path / "etc").mkdir()
    _, text = refresh(tmp_path)
    assert text.startswith(
        "\nGinnungagap appliance v1.3.0\nSSH: ssh junioradmin@192.0.2.5 -p 22\n"
    )
    assert text.endswith("\n\n")


def test_the_first_line_names_the_release_and_falls_back_to_the_product_alone(tmp_path):
    issue = tmp_path / "issue.d" / "x.issue"
    console.write_issue(issue, ["line"])
    assert issue.read_text(encoding="utf-8").startswith("\nGinnungagap appliance\nline")
    console.write_issue(issue, ["line"], "1.4.0")
    assert issue.read_text(encoding="utf-8").startswith(
        "\nGinnungagap appliance v1.4.0\nline"
    )


def test_refresh_reads_the_release_from_the_installed_state_by_default(
    tmp_path, monkeypatch
):
    from ginnungagap import manifest as mf
    from tests.helpers import fake_release

    release = fake_release.make_release(tmp_path / "r", appliance="1.9.0")
    state = mf.state_from_manifest(
        mf.load(release / "components.json"),
        {n: "1" for n in mf.COMPONENT_NAMES},
        "2026-11-19T10:00:00Z",
        None,
    )
    update = tmp_path / "update"
    mf.save_installed(update / "installed.json", state)
    monkeypatch.setenv("GINNUNGAGAP_UPDATE_DIR", str(update))
    issue = tmp_path / "issue.d" / "ginnungagap.issue"
    console.refresh(
        tmp_path / "etc",
        state_dir=tmp_path / "state",
        issue_file=issue,
        ssh_dir=tmp_path / "ssh",
        local_address_fn=lambda: None,
        login_user_fn=lambda: None,
    )
    assert issue.read_text(encoding="utf-8").startswith(
        "\nGinnungagap appliance v1.9.0\n"
    )


def test_without_an_address_the_placeholder_is_shown_in_both_sections(tmp_path):
    lines, _ = refresh(tmp_path, address=None)
    assert lines[0] == "SSH: ssh junioradmin@<this machine's address> -p 22"
    assert any("http://<this machine's address>/" in line for line in lines)


def test_with_a_configuration_the_web_interface_is_named_from_it(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "ginnungagap.yml").write_text(
        yaml.safe_dump(VALID_CONFIG),
        encoding="utf-8",
    )
    lines, _ = refresh(tmp_path)
    assert "Web interface: http://ginn.lab" in lines
    assert any(line.startswith("First login: admin / admin") for line in lines)


def test_the_console_names_the_ova_server_address_and_the_port_the_esxi_hosts_need(
    tmp_path,
):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "ginnungagap.yml").write_text(yaml.safe_dump(VALID_CONFIG), encoding="utf-8")
    lines, _ = refresh(tmp_path)
    assert (
        "OVA server for the ESXi hosts: http://ginn.lab:8088 (no login: allow TCP 8088 from the ESXi hosts only)"
        in lines
    )


def test_the_console_names_no_ova_server_when_it_runs_elsewhere(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    elsewhere = {
        **VALID_CONFIG,
        "services": {"ova_store": False},
        "templates": {"ova_base_url": "http://10.0.0.9"},
    }
    (etc / "ginnungagap.yml").write_text(yaml.safe_dump(elsewhere), encoding="utf-8")
    lines, _ = refresh(tmp_path)
    assert not any("OVA server" in line for line in lines)


def test_an_unreadable_configuration_is_named_instead_of_guessing_an_address(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "ginnungagap.yml").write_text("services: {nonsense: 1}\n", encoding="utf-8")
    lines, _ = refresh(tmp_path)
    assert any("exists in" in line and "is not valid" in line for line in lines)


def test_no_login_user_leaves_just_the_web_section(tmp_path):
    lines, _ = refresh(tmp_path, user=None)
    assert lines[0].startswith("No configuration yet.")
    assert not any(line.startswith("SSH:") for line in lines)


def test_the_password_line_is_shown_only_for_a_generated_unchanged_password(tmp_path):
    state = tmp_path / "state"
    console.write_access(state, "generated", "Secret123")
    shown, _ = refresh(tmp_path, changed=False)
    assert "Password: Secret123 (you must change it at the first login)" in shown
    hidden, text = refresh(tmp_path, changed=True)
    assert not any(line.startswith("Password:") for line in hidden)
    assert "Secret123" not in text


def test_a_message_that_can_not_be_written_never_raises(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder should be", encoding="utf-8")
    console.write_issue(blocker / "issue.d" / "x.issue", ["line"])


def test_only_terminals_nobody_is_logged_in_on_are_cleared():
    from ginnungagap import console

    who = "junioradmin tty1         2026-11-17 16:00\nroot pts/0 2026-11-17 16:01 (10.0.0.5)\n"
    assert console.idle_login_terminals(who) == ["tty2", "tty3", "tty4", "tty5", "tty6"]
    assert console.idle_login_terminals("") == list(console.LOGIN_TERMINALS)


def test_clearing_writes_the_clear_sequence_to_idle_terminals_only(
    tmp_path, monkeypatch
):
    import subprocess

    from ginnungagap import console

    for name in console.LOGIN_TERMINALS:
        (tmp_path / name).write_text(
            "old text with the initial password", encoding="utf-8"
        )

    class Done:
        stdout = "junioradmin tty2 2026-11-17 16:00\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    console.clear_idle_terminals(tmp_path)
    assert (tmp_path / "tty2").read_text(
        encoding="utf-8"
    ) == "old text with the initial password"
    for name in ("tty1", "tty3", "tty6"):
        assert (tmp_path / name).read_text(encoding="utf-8") == console.CLEAR_SCREEN
