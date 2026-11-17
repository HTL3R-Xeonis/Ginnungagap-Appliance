import base64
from pathlib import Path

import pytest
import yaml

from ginnungagap import firstboot as fb

VALID = {"generator": {"llm_servers": [{"name": "o", "url": "http://10.0.0.1:11434", "model": "m"}]}, "appliance": {"public_url": "http://ginn.lab"}, "ova_deploy": {"external_url": "http://10.0.0.2:8003"}}  # fmt: skip


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
            out=self.lines.append,
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
