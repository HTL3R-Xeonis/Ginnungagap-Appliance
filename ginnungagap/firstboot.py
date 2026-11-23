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

and prepares the machine: the login user's SSH password (a random one shown on the console until it is changed, unless the
deployer gave a hash or a key - see ginnungagap/console.py for the text), the SSH host keys (the image has none, so no two VMs share them), the configuration file after
validating it (an invalid one is kept as ginnungagap.yml.rejected and the reasons are shown at the login prompt), and a
message at the console login. A configuration that is already in place wins over guestinfo. Once a configuration is in
place the first boot is never done again; without one (nothing given, or an invalid one) the next boot tries again.

Everything outside the machine goes through arguments (the guestinfo reader, the command runner, the folders), so it can be
tested without a hypervisor.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import pwd
import re
import secrets
import string
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

from ginnungagap import config as config_module
from ginnungagap import console
from ginnungagap.console import ISSUE_FILE, PROBLEMS, REJECTED, STATE_DIR

GuestInfo = Callable[[str], str | None]
Runner = Callable[[list[str]], int]
PasswordSetter = Callable[[str, str], int]

PREFIX = "guestinfo.ginnungagap."
DONE_MARKER = "firstboot.done"
SUDOERS_DIR = "/etc/sudoers.d"
PASSWORD_LENGTH = 16


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


_local_address = console.local_address


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


_login_user = console.login_user


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))


def _set_password(user: str, password: str) -> int:
    """`chpasswd` with the password on its standard input: it never appears in an argument list or a process listing."""
    try:
        return subprocess.run(
            ["chpasswd"],
            input=f"{user}:{password}\n",
            text=True,
            check=False,
            timeout=30,
        ).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


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
    set_password: PasswordSetter | None = None,
    new_password: Callable[[], str] | None = None,
    password_changed: Callable[[str], bool] | None = None,
    network_apply: Callable[[dict], object] | None = None,
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

    def info(name: str) -> str | None:
        return guestinfo(PREFIX + name)

    network = info("network")
    if network is not None:
        try:
            if len(network) > 16384:
                raise ValueError("network input is too large")
            from ginnungagap.image_network import apply_network

            (network_apply or apply_network)(json.loads(network))
        except (
            ValueError,
            TypeError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
        ):
            _write_private(
                state_dir / PROBLEMS,
                "The guest management network could not be configured.\n",
            )
            out("the guest management network could not be configured")
            return 1

    # SSH host keys: an image carries none (copies would share them); an existing machine keeps the ones it has
    if not list(ssh_dir.glob("ssh_host_*_key")):
        out("creating the SSH host keys")
        if runner(["ssh-keygen", "-A"]) != 0:
            out("ssh-keygen -A failed - the machine will not accept SSH logins")

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

    state_dir.mkdir(parents=True, exist_ok=True)
    if user and console.read_access(state_dir) is None:
        deployer_hash = bool(password_hash) and password_hash.startswith("$")
        if key or deployer_hash:
            console.write_access(state_dir, "deployer")
            if key and not deployer_hash:
                # key only: the Debian password of the template must not stay valid now that password login is on
                out(f"locking the password of {user} (only the given SSH key opens it)")
                runner(["passwd", "-l", user])
        else:
            password = (new_password or _random_password)()
            if (set_password or _set_password)(user, password) == 0:
                runner(["chage", "-d", "0", user])
                console.write_access(state_dir, "generated", password)
                out(
                    f"set a random initial password for {user}; it is shown on the console until it is changed"
                )
            else:
                out(
                    f"could not set an initial password for {user} - SSH password login will not work until one is set"
                )

    result = 0
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

    console.refresh(
        config_dir,
        state_dir=state_dir,
        issue_file=issue_file,
        ssh_dir=ssh_dir,
        local_address_fn=local_address,
        login_user_fn=lambda: user,
        password_changed=password_changed,
    )
    if (
        result == 0
    ):  # without a usable configuration the next boot looks at guestinfo again
        (state_dir / DONE_MARKER).write_text("done\n", encoding="utf-8")
    return result
