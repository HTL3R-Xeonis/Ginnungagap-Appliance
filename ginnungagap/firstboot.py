"""
First boot of an appliance image: the configuration comes from the hypervisor instead of from a person at a keyboard.

A VM deployed from the image has no ginnungagap.yml. At its first boot this reads what the deployer put into the VM's
guestinfo (VMware's key/value channel, read with `vmtoolsd`; on ESXi: VM options > Advanced > configuration parameters):

  guestinfo.ginnungagap.config               the configuration file, base64 (or plain text with ...config.encoding = plain)
  guestinfo.ginnungagap.hostname             optional, the VM's host name
  guestinfo.ginnungagap.ssh_authorized_key   optional, one public key for the login user
  guestinfo.ginnungagap.admin_password_hash  optional, a crypt(3) hash (`openssl passwd -6`), never the password itself
  guestinfo.ginnungagap.admin_sudo_nopasswd  optional, "true" lets the login user use sudo without a password (for
                                             automation; off by default, an image is delivered without it)

and prepares the machine: the SSH host keys (the image has none, so no two VMs share them), the configuration file after
validating it (an invalid one is kept as ginnungagap.yml.rejected and the reasons are shown at the login prompt), and a
message at the console login. A configuration that is already in place wins over guestinfo. Once a configuration is in
place the first boot is never done again; without one (nothing given, or an invalid one) the next boot tries again.

Everything outside the machine goes through arguments (the guestinfo reader, the command runner, the folders), so it can be
tested without a hypervisor.
"""

from __future__ import annotations

import base64
import binascii
import os
import pwd
import re
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

from ginnungagap import config as config_module

GuestInfo = Callable[[str], str | None]
Runner = Callable[[list[str]], int]

PREFIX = "guestinfo.ginnungagap."
DONE_MARKER = "firstboot.done"
REJECTED = "ginnungagap.yml.rejected"
PROBLEMS = "firstboot.problems"
ISSUE_FILE = "/etc/issue.d/ginnungagap.issue"
STATE_DIR = "/var/lib/ginnungagap"
SUDOERS_DIR = "/etc/sudoers.d"
LOGIN_UID = 1000  # the first ordinary account of a Debian image


def vmtools_guestinfo(key: str) -> str | None:
    """The value of a guestinfo key, None when it is not set or there are no VMware tools."""
    try:
        result = subprocess.run(
            ["vmtoolsd", "--cmd", f"info-get {key}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def default_runner(args: list[str]) -> int:
    return subprocess.run(args, check=False).returncode


def _local_address() -> str | None:
    """This machine's own address on its default route, None when there isn't one (no network yet). Never sends
    anything: a UDP "connect" only asks the kernel which local address a packet to that address would leave from."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(
                ("198.51.100.1", 80)
            )  # TEST-NET-2 (RFC 5737): reserved, never actually reached
            return probe.getsockname()[0]
    except OSError:
        return None


def decode_config(value: str, encoding: str | None) -> str:
    """The configuration text from its guestinfo value. :raises ValueError: the value is not valid base64 / not text."""
    if (encoding or "base64").strip().lower() == "plain":
        return value
    try:
        return base64.b64decode(value, validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"the configuration is not valid base64 text ({exc})") from exc


def check_config_text(text: str) -> tuple[dict | None, list[str]]:
    """(normalised configuration, []) or (None, every problem)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"isn't valid YAML ({exc})"]
    try:
        return config_module.normalise(raw), []
    except config_module.ConfigError as exc:
        return None, list(exc.problems)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _write_issue(issue_file: Path, lines: list[str]) -> None:
    """The text shown at the console login (Debian reads /etc/issue.d/*.issue)."""
    try:
        issue_file.parent.mkdir(parents=True, exist_ok=True)
        issue_file.write_text(
            "\n".join(["", "Ginnungagap appliance", *lines, "", ""]), encoding="utf-8"
        )
    except OSError:
        pass  # a missing message must never stop the boot


def _write_setup_issue(config_dir: Path, issue_file: Path, address: str | None) -> None:
    address = address or "<this machine's address>"
    config_file = config_dir / "ginnungagap.yml"
    _write_issue(
        issue_file,
        [
            f"No configuration yet. Open http://{address}/ in a browser to",
            "finish setup (the setup wizard) - or set",
            "guestinfo.ginnungagap.config (base64 of ginnungagap.yml) and reboot,",
            "or, as root:",
            f"  cp {config_dir}/ginnungagap.example.yml {config_file}",
            f"  edit {config_file}; ginnungagap apply {config_file}",
        ],
    )


def refresh_setup_issue(
    config_dir: Path,
    *,
    state_dir: Path = Path(STATE_DIR),
    issue_file: Path = Path(ISSUE_FILE),
    local_address: Callable[[], str | None] = _local_address,
) -> None:
    """Show the address now that the wizard is running. Keep any guestinfo diagnostics."""
    if (config_dir / "ginnungagap.yml").exists() or (state_dir / PROBLEMS).exists():
        return
    _write_setup_issue(config_dir, issue_file, local_address())


def _login_user() -> str | None:
    for entry in pwd.getpwall():
        if entry.pw_uid == LOGIN_UID:
            return entry.pw_name
    return None


def _install_ssh_key(user: str, key: str, home: Path | None = None) -> None:
    entry = pwd.getpwnam(user)
    ssh_dir = (home or Path(entry.pw_dir)) / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    authorized = ssh_dir / "authorized_keys"
    existing = (
        authorized.read_text(encoding="utf-8").splitlines()
        if authorized.exists()
        else []
    )
    if key not in existing:
        authorized.write_text("\n".join([*existing, key]) + "\n", encoding="utf-8")
    authorized.chmod(0o600)
    if os.geteuid() == 0:
        for path in (ssh_dir, authorized):
            os.chown(path, entry.pw_uid, entry.pw_gid)


def _allow_sudo(user: str, sudoers_dir: Path, runner: Runner) -> bool:
    """A sudoers drop-in for the login user; it is checked with visudo before it stays."""
    if not re.fullmatch(r"[a-z_][a-z0-9_.-]*", user):
        return False  # never write a name into a sudoers file that could be more than a name
    target = sudoers_dir / "90-ginnungagap-admin"
    candidate = sudoers_dir / ".90-ginnungagap-admin.new"
    candidate.write_text(f"{user} ALL=(ALL) NOPASSWD:ALL\n", encoding="utf-8")
    candidate.chmod(0o440)
    if runner(["visudo", "-cf", str(candidate)]) != 0:
        candidate.unlink(missing_ok=True)
        return False
    candidate.replace(target)
    return True


def _make_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def run(
    config_dir: str | Path = "/etc/ginnungagap",
    state_dir: str | Path = STATE_DIR,
    issue_file: str | Path = ISSUE_FILE,
    ssh_dir: str | Path = "/etc/ssh",
    guestinfo: GuestInfo = vmtools_guestinfo,
    runner: Runner = default_runner,
    login_user: Callable[[], str | None] = _login_user,
    install_key: Callable[[str, str], None] = _install_ssh_key,
    make_dir: Callable[[Path], None] = _make_dir,
    sudoers_dir: str | Path = SUDOERS_DIR,
    local_address: Callable[[], str | None] = _local_address,
    out: Callable[[str], None] = print,
) -> int:
    """
    The whole first boot. Returns 0 when the machine is ready to have its configuration applied (or when there is nothing
    to do), 1 when it had to be left without one; both are normal boots - the caller must not retry in a loop.
    """
    config_dir, state_dir = Path(config_dir), Path(state_dir)
    issue_file, ssh_dir = Path(issue_file), Path(ssh_dir)
    config_file = config_dir / "ginnungagap.yml"
    if (state_dir / DONE_MARKER).exists():
        out("first boot was done before - nothing to do")
        return 0

    # SSH host keys: an image carries none (copies would share them); an existing machine keeps the ones it has
    if not list(ssh_dir.glob("ssh_host_*_key")):
        out("creating the SSH host keys")
        if runner(["ssh-keygen", "-A"]) != 0:
            out("ssh-keygen -A failed - the machine will not accept SSH logins")

    def info(name: str) -> str | None:
        return guestinfo(PREFIX + name)

    hostname = info("hostname")
    if hostname:
        out(f"setting the host name to {hostname}")
        runner(["hostnamectl", "set-hostname", hostname])
    user = login_user()
    key = info("ssh_authorized_key")
    if key and user:
        out(f"authorizing the given SSH key for {user}")
        install_key(user, key)
    password_hash = info("admin_password_hash")
    if password_hash and user:
        if password_hash.startswith("$"):
            out(f"setting the password of {user} from the given hash")
            runner(["usermod", "-p", password_hash, user])
        else:
            out(
                "admin_password_hash is not a crypt hash (it must start with $) - ignored"
            )

    if info("admin_sudo_nopasswd") == "true" and user:
        if _allow_sudo(user, Path(sudoers_dir), runner):
            out(f"{user} may use sudo without a password (guestinfo asked for it)")
        else:
            out(
                "admin_sudo_nopasswd was refused: the sudoers entry did not pass visudo"
            )

    result = 0
    state_dir.mkdir(parents=True, exist_ok=True)
    problems_file = state_dir / PROBLEMS
    problems_file.unlink(missing_ok=True)
    if config_file.exists():
        out(f"keeping the configuration that is already in {config_file}")
    else:
        value = info("config")
        if value is None:
            out(
                "no configuration in guestinfo.ginnungagap.config and none in "
                f"{config_file}"
            )
            _write_setup_issue(config_dir, issue_file, local_address())
            result = 1
        else:
            try:
                text = decode_config(value, info("config.encoding"))
                parsed, problems = check_config_text(text)
            except ValueError as exc:
                text, parsed, problems = "", None, [str(exc)]
            if parsed is None:
                out("the configuration from guestinfo is not valid:")
                for problem in problems:
                    out(f"  - {problem}")
                _write_private(config_dir / REJECTED, text)
                _write_private(problems_file, "\n".join(problems) + "\n")
                _write_issue(
                    issue_file,
                    [
                        "The configuration given in guestinfo is not valid:",
                        *[f"  - {p}" for p in problems[:6]],
                        f"It is kept as {config_dir / REJECTED}. Fix it, save it as",
                        f"{config_file} and run: ginnungagap apply {config_file}",
                    ],
                )
                result = 1
            else:
                _write_private(config_file, text)
                store = parsed["templates"]["store"]
                if store["type"] == "local":
                    # the services mount it read-only and Docker will not start them for a folder that is not there
                    try:
                        make_dir(Path(store["local"]["path"]))
                    except OSError as exc:
                        out(
                            f"could not create the template folder {store['local']['path']}: {exc}"
                        )
                url = parsed["appliance"]["public_url"]
                out(
                    f"configuration accepted, {url} will answer once the services are up"
                )
                _write_issue(
                    issue_file,
                    [
                        f"Web interface: {url}",
                        (
                            "First login: admin / admin - change it once you are in "
                            "(Admin panel > User accounts)."
                        ),
                    ],
                )

    if (
        result == 0
    ):  # without a usable configuration the next boot looks at guestinfo again
        (state_dir / DONE_MARKER).write_text("done\n", encoding="utf-8")
    return result
