import base64
import json
from pathlib import Path

import pytest
import yaml

from ginnungagap import console, image_network
from ginnungagap import firstboot as fb

VALID = {"generator": {"llm_servers": [{"name": "o", "url": "http://10.0.0.1:11434", "model": "m"}]}, "appliance": {"public_url": "http://ginn.lab"}}  # fmt: skip


def b64(data) -> str:
    text = data if isinstance(data, str) else yaml.safe_dump(data)
    return base64.b64encode(text.encode()).decode()


class Machine:
    """The folders and the fakes a first boot works with."""

    def __init__(
        self,
        tmp_path: Path,
        guestinfo: dict[str, str] | None = None,
        with_keys: bool = False,
    ):
        self.config_dir = tmp_path / "etc-ginnungagap"
        self.state_dir = tmp_path / "state"
        self.issue = tmp_path / "issue.d" / "ginnungagap.issue"
        self.ssh_dir = tmp_path / "ssh"
        self.ssh_dir.mkdir(parents=True)
        if with_keys:
            (self.ssh_dir / "ssh_host_ed25519_key").write_text("existing")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.info = guestinfo or {}
        self.commands: list[list[str]] = []
        self.keys: list[tuple[str, str]] = []
        self.lines: list[str] = []
        self.made: list[Path] = []
        self.sudoers_dir = tmp_path / "sudoers.d"
        self.sudoers_dir.mkdir(parents=True, exist_ok=True)
        self.passwords: list[tuple[str, str]] = []
        self.password_status = 0
        self.changed = False

    def set_password(self, user, password):
        self.passwords.append((user, password))
        return self.password_status

    def runner(self, args):
        self.commands.append(args)
        return 0

    def run(self, user: str | None = "junioradmin") -> int:
        return fb.run(
            config_dir=self.config_dir,
            state_dir=self.state_dir,
            issue_file=self.issue,
            ssh_dir=self.ssh_dir,
            guestinfo=lambda key: self.info.get(key.removeprefix(fb.PREFIX)),
            runner=self.runner,
            login_user=lambda: user,
            install_key=lambda u, k: self.keys.append((u, k)),
            make_dir=self.made.append,
            sudoers_dir=self.sudoers_dir,
            local_address=lambda: None,
            out=self.lines.append,
            set_password=self.set_password,
            new_password=lambda: "Abcd1234Efgh5678",
            password_changed=lambda user: self.changed,
        )


@pytest.fixture
def machine(tmp_path):
    made = []

    def build(info=None, **kw):
        made.append(1)  # every machine of a test gets its own folders
        return Machine(tmp_path / f"machine{len(made)}", info, **kw)

    return build


def test_a_valid_configuration_from_guestinfo_is_saved_privately_and_the_first_boot_is_done(
    machine,
):
    m = machine({"config": b64(VALID)})
    assert m.run() == 0
    saved = m.config_dir / "ginnungagap.yml"
    assert yaml.safe_load(saved.read_text()) == VALID
    assert saved.stat().st_mode & 0o777 == 0o600
    assert (m.state_dir / fb.DONE_MARKER).exists()
    assert "http://ginn.lab" in m.issue.read_text()


def test_plain_text_is_accepted_when_the_encoding_says_so(machine):
    m = machine({"config": yaml.safe_dump(VALID), "config.encoding": "plain"})
    assert m.run() == 0
    assert yaml.safe_load((m.config_dir / "ginnungagap.yml").read_text()) == VALID


def test_network_only_guestinfo_runs_before_key_and_password_initialization(
    machine, monkeypatch
):
    config = {
        "address": "10.20.20.185",
        "prefix": 24,
        "gateway": "10.20.20.254",
        "dns": ["1.1.1.1"],
    }
    m = machine({"network": json.dumps(config)})
    seen = []

    def apply(value):
        assert m.commands == [] and m.passwords == [] and m.keys == []
        seen.append(value)

    monkeypatch.setattr(image_network, "apply_network", apply)
    assert m.run() == 1  # normal unconfigured boot still opens the setup wizard
    assert seen == [config]
    assert m.passwords  # network-only boot keeps the normal generated password
    assert not (m.config_dir / "ginnungagap.yml").exists()


def test_invalid_network_stops_firstboot_before_access_is_initialized(
    machine, monkeypatch
):
    m = machine({"network": "{}", "config": b64(VALID)})
    monkeypatch.setattr(
        image_network,
        "apply_network",
        lambda value: image_network.validate_network(value),
    )
    assert m.run() == 1
    assert not m.passwords and not m.keys and not m.commands
    assert (m.state_dir / fb.PROBLEMS).exists()
    assert not (m.state_dir / fb.DONE_MARKER).exists()


def test_an_invalid_configuration_is_kept_aside_with_every_reason_and_the_next_boot_tries_again(
    machine,
):
    m = machine({"config": b64({"servises": {}, "appliance": {"http_port": "nope"}})})
    assert m.run() == 1
    assert not (m.config_dir / "ginnungagap.yml").exists()
    assert "servises" in (m.config_dir / fb.REJECTED).read_text()
    assert (m.config_dir / fb.REJECTED).stat().st_mode & 0o777 == 0o600
    problems = (m.state_dir / fb.PROBLEMS).read_text()
    assert "servises" in problems and "http_port" in problems
    assert "not valid" in m.issue.read_text() and "servises" in m.issue.read_text()
    assert not (m.state_dir / fb.DONE_MARKER).exists()


def test_a_value_that_is_not_base64_text_is_rejected_not_crashed_on(machine):
    m = machine({"config": "\xff\xfe-not-base64"})
    assert m.run() == 1
    assert (m.state_dir / fb.PROBLEMS).exists()


def test_without_any_configuration_the_console_says_what_to_do_and_the_next_boot_looks_again(
    machine,
):
    m = machine()
    assert m.run() == 1
    text = m.issue.read_text()
    assert "guestinfo.ginnungagap.config" in text and "ginnungagap.example.yml" in text
    assert not (m.state_dir / fb.DONE_MARKER).exists()
    m.info["config"] = b64(VALID)  # the deployer sets it and reboots
    assert m.run() == 0 and (m.config_dir / "ginnungagap.yml").exists()


def _refresh(m, address):
    return console.refresh(
        m.config_dir,
        state_dir=m.state_dir,
        issue_file=m.issue,
        ssh_dir=m.ssh_dir,
        local_address_fn=lambda: address,
        login_user_fn=lambda: "junioradmin",
        password_changed=lambda user: m.changed,
    )


def test_setup_issue_uses_the_address_received_after_firstboot(machine):
    m = machine()
    assert m.run() == 1
    assert "<this machine's address>" in m.issue.read_text()

    _refresh(m, "192.0.2.17")

    assert "http://192.0.2.17/" in m.issue.read_text()
    assert "ssh junioradmin@192.0.2.17 -p 22" in m.issue.read_text()
    assert "<this machine's address>" not in m.issue.read_text()


@pytest.mark.parametrize("config", [VALID, {"servises": {}}])
def test_a_refresh_keeps_the_guestinfo_result_on_the_console(machine, config):
    m = machine({"config": b64(config)})
    m.run()
    before = m.issue.read_text()

    _refresh(m, None)

    assert m.issue.read_text() == before


def test_a_configuration_that_is_already_there_wins_over_guestinfo(machine):
    m = machine({"config": b64(VALID)})
    (m.config_dir / "ginnungagap.yml").write_text("# mine\n")
    assert m.run() == 0
    assert (m.config_dir / "ginnungagap.yml").read_text() == "# mine\n"


def test_once_done_it_never_runs_again(machine):
    m = machine({"config": b64(VALID)})
    assert m.run() == 0
    m.info["config"] = b64(
        {"generator": VALID["generator"], "appliance": {"public_url": "http://other"}}
    )
    (m.config_dir / "ginnungagap.yml").unlink()
    assert m.run() == 0
    assert not (m.config_dir / "ginnungagap.yml").exists()  # not rewritten
    assert any("done before" in line for line in m.lines)


def test_ssh_host_keys_are_created_only_when_there_are_none(machine):
    fresh = machine({"config": b64(VALID)}, with_keys=False)
    fresh.run()
    assert ["ssh-keygen", "-A"] in fresh.commands
    existing = machine({"config": b64(VALID)}, with_keys=True)
    existing.run()
    assert ["ssh-keygen", "-A"] not in existing.commands


def test_host_name_key_and_password_hash_are_applied_when_given(machine):
    m = machine(
        {
            "config": b64(VALID),
            "hostname": "ginn-01",
            "ssh_authorized_key": "ssh-ed25519 AAAA test",
            "admin_password_hash": "$6$salt$hash",
        }
    )
    assert m.run() == 0
    assert ["hostnamectl", "set-hostname", "ginn-01"] in m.commands
    assert m.keys == [("junioradmin", "ssh-ed25519 AAAA test")]
    assert ["usermod", "-p", "$6$salt$hash", "junioradmin"] in m.commands


def test_the_neutral_host_name_of_the_image_is_replaced_by_the_guestinfo_one_and_kept_without_it(
    machine,
):
    given = machine({"config": b64(VALID), "hostname": "ginn-02"})
    assert given.run() == 0
    assert ["hostnamectl", "set-hostname", "ginn-02"] in given.commands
    none = machine({"config": b64(VALID)})
    assert none.run() == 0
    assert not any(
        c[0] == "hostnamectl" for c in none.commands
    )  # image/prepare-image.sh set the neutral default
    blank = machine({"config": b64(VALID), "hostname": ""})
    assert blank.run() == 0
    assert not any(c[0] == "hostnamectl" for c in blank.commands)


def test_a_password_that_is_not_a_hash_is_ignored_and_never_passed_on(machine):
    m = machine({"config": b64(VALID), "admin_password_hash": "plain-password"})
    m.run()
    assert not any(c[0] == "usermod" for c in m.commands)
    assert not any("plain-password" in line for line in m.lines)


def test_the_configuration_text_is_never_written_to_the_output(machine):
    secret = {
        **VALID,
        "generator": {
            "llm_servers": [
                {"name": "o", "url": "http://10.0.0.1:11434", "model": "m"}
            ],
            "hosted": {"gemini": {"api_key": "sehr-geheim"}},
        },
    }
    m = machine({"config": b64(secret)})
    m.run()
    assert not any("sehr-geheim" in line for line in m.lines)


def test_authorized_key_is_appended_once_with_the_right_mode(tmp_path, monkeypatch):
    import os
    import pwd

    entry = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(fb.pwd, "getpwnam", lambda name: entry)
    home = tmp_path / "home"
    home.mkdir()
    fb._install_ssh_key(entry.pw_name, "ssh-ed25519 AAAA one", home=home)
    fb._install_ssh_key(entry.pw_name, "ssh-ed25519 AAAA one", home=home)
    fb._install_ssh_key(entry.pw_name, "ssh-ed25519 BBBB two", home=home)
    authorized = home / ".ssh" / "authorized_keys"
    assert authorized.read_text().splitlines() == [
        "ssh-ed25519 AAAA one",
        "ssh-ed25519 BBBB two",
    ]
    assert authorized.stat().st_mode & 0o777 == 0o600
    assert (home / ".ssh").stat().st_mode & 0o777 == 0o700


def test_guestinfo_reader_returns_none_without_vmware_tools(monkeypatch):
    monkeypatch.setattr(
        fb.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("vmtoolsd")),
    )
    assert fb.vmtools_guestinfo("guestinfo.ginnungagap.config") is None


def test_the_folder_of_a_local_template_store_is_created_before_the_services_start(
    machine,
):
    config = {
        **VALID,
        "templates": {"store": {"type": "local", "local": {"path": "/srv/my-store"}}},
    }
    m = machine({"config": b64(config)})
    assert m.run() == 0
    assert m.made == [Path("/srv/my-store")]


def test_an_nfs_store_needs_no_folder(machine):
    config = {
        **VALID,
        "templates": {
            "store": {"type": "nfs", "nfs": {"server": "10.0.0.5", "export": "/t"}}
        },
    }
    m = machine({"config": b64(config)})
    assert m.run() == 0 and m.made == []


def test_passwordless_sudo_is_off_unless_guestinfo_asks_for_it(machine):
    m = machine({"config": b64(VALID)})
    m.run()
    assert not list(m.sudoers_dir.iterdir())


def test_passwordless_sudo_is_written_checked_with_visudo_and_kept(machine):
    m = machine({"config": b64(VALID), "admin_sudo_nopasswd": "true"})
    assert m.run() == 0
    entry = m.sudoers_dir / "90-ginnungagap-admin"
    assert entry.read_text() == "junioradmin ALL=(ALL) NOPASSWD:ALL\n"
    assert entry.stat().st_mode & 0o777 == 0o440
    assert any(c[:2] == ["visudo", "-cf"] for c in m.commands)


def test_a_sudoers_entry_that_visudo_refuses_is_not_kept(machine):
    m = machine({"config": b64(VALID), "admin_sudo_nopasswd": "true"})
    m.runner = lambda args: m.commands.append(args) or (1 if args[0] == "visudo" else 0)
    m.run()
    assert not list(m.sudoers_dir.iterdir())


def test_a_login_name_that_could_be_more_than_a_name_gets_no_sudoers_entry(machine):
    m = machine({"config": b64(VALID), "admin_sudo_nopasswd": "true"})
    m.run(user="x ALL=(ALL) NOPASSWD:ALL #")
    assert not list(m.sudoers_dir.iterdir())


# ---- the admin SSH access ----------------------------------------------------------------------------------------


def test_a_random_initial_password_is_set_through_stdin_and_expired(machine):
    m = machine()
    m.run()
    assert m.passwords == [("junioradmin", "Abcd1234Efgh5678")]
    assert ["chage", "-d", "0", "junioradmin"] in m.commands
    assert all("Abcd1234Efgh5678" not in " ".join(c) for c in m.commands)
    assert all("Abcd1234Efgh5678" not in line for line in m.lines)
    state = m.state_dir / console.ACCESS
    assert state.stat().st_mode & 0o777 == 0o600
    assert console.read_access(m.state_dir) == {
        "mode": "generated",
        "password": "Abcd1234Efgh5678",
    }


def test_the_default_generator_makes_16_letters_and_digits_that_differ_every_time():
    first, second = fb._random_password(), fb._random_password()
    assert len(first) == 16 and first.isalnum() and first != second


def test_the_password_is_not_generated_again_at_a_later_boot(machine):
    m = machine()
    assert m.run() == 1  # no configuration yet: the next boot runs first boot again
    m.run()
    assert len(m.passwords) == 1


def test_a_given_hash_or_key_means_nothing_is_generated(machine):
    hashed = machine({"admin_password_hash": "$6$salt$hash"})
    hashed.run()
    assert hashed.passwords == []
    assert console.read_access(hashed.state_dir) == {"mode": "deployer"}
    assert ["passwd", "-l", "junioradmin"] not in hashed.commands

    keyed = machine({"ssh_authorized_key": "ssh-ed25519 AAAA test"})
    keyed.run()
    assert keyed.passwords == []
    assert console.read_access(keyed.state_dir) == {"mode": "deployer"}
    assert [
        "passwd",
        "-l",
        "junioradmin",
    ] in keyed.commands  # the template's own password must not stay valid


def test_a_hash_that_is_not_a_hash_does_not_count_as_given(machine):
    m = machine({"admin_password_hash": "plaintext-not-a-hash"})
    m.run()
    assert len(m.passwords) == 1


def test_a_failing_chpasswd_is_reported_without_the_password_and_without_state(machine):
    m = machine()
    m.password_status = 1
    m.run()
    assert console.read_access(m.state_dir) is None
    assert any("could not set an initial password" in line for line in m.lines)
    assert all("Abcd1234Efgh5678" not in line for line in m.lines)
    assert ["chage", "-d", "0", "junioradmin"] not in m.commands


def test_no_login_user_means_no_ssh_section_and_no_password(machine):
    m = machine()
    m.run(user=None)
    assert m.passwords == []
    assert "SSH:" not in m.issue.read_text()


def test_the_console_shows_address_port_and_the_initial_password_until_it_is_changed(
    machine,
):
    m = machine()
    m.run()
    text = m.issue.read_text()
    assert "SSH: ssh junioradmin@<this machine's address> -p 22" in text
    assert "Password: Abcd1234Efgh5678 (you must change it at the first login)" in text

    m.changed = True
    _refresh(m, "192.0.2.17")
    after = m.issue.read_text()
    assert "SSH: ssh junioradmin@192.0.2.17 -p 22" in after
    assert "Password:" not in after and "Abcd1234Efgh5678" not in after


def test_the_console_says_when_the_deployer_set_the_access(machine):
    m = machine({"admin_password_hash": "$6$salt$hash"})
    m.run()
    assert "(password/key set by the deployer)" in m.issue.read_text()
    assert "Password:" not in m.issue.read_text()
