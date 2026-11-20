"""
The text a person sees at the VM's console login (its first line names the appliance release): how to reach the machine over SSH (address and port, and the initial
password while it has not been changed yet) and where its web interface is (or how to finish its setup).

There is exactly one composer (`compose`) and exactly one writer (`write_issue`, used by `refresh`) of
/etc/issue.d/ginnungagap.issue; first boot, bootstrap, apply, the setup wizard's hand-off and the boot-time unit
`ginnungagap-issue.service` all go through `refresh`, so the text always describes the machine as it is now. Everything
outside the machine (address, login user, shadow file, folders) comes in through arguments, so it can be tested without
a VM.
"""

from __future__ import annotations

import glob
import json
import re
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from ginnungagap import config as config_module
from ginnungagap import manifest as manifest_module
from ginnungagap import render as render_module

ISSUE_FILE = "/etc/issue.d/ginnungagap.issue"
STATE_DIR = "/var/lib/ginnungagap"
CONFIG_DIR = "/etc/ginnungagap"
SSH_DIR = "/etc/ssh"
SHADOW_FILE = "/etc/shadow"
REJECTED = "ginnungagap.yml.rejected"
PROBLEMS = "firstboot.problems"
ACCESS = "ssh-access.json"
LOGIN_UID = 1000  # the first ordinary account of a Debian image
NO_ADDRESS = "<this machine's address>"

MAX_INCLUDE_DEPTH = 5


def local_address() -> str | None:
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


def login_user() -> str | None:
    import pwd

    for entry in pwd.getpwall():
        if entry.pw_uid == LOGIN_UID:
            return entry.pw_name
    return None


# ---- the SSH access state ------------------------------------------------------------------------------------------


def read_access(state_dir: Path) -> dict[str, str] | None:
    """What first boot decided about the login user's password: {"mode": "generated", "password": ...} or {"mode":
    "deployer"}. None when it has not decided yet (or the file is not readable)."""
    try:
        data = json.loads((state_dir / ACCESS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("mode") not in ("generated", "deployer"):
        return None
    return {k: v for k, v in data.items() if isinstance(v, str)}


def write_access(state_dir: Path, mode: str, password: str | None = None) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {"mode": mode, **({"password": password} if password else {})}
    path = state_dir / ACCESS
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def shadow_password_changed(user: str, shadow_file: str | Path = SHADOW_FILE) -> bool:
    """False only while the account's last-change field in the shadow file is 0 (`chage -d 0`: the password must be
    changed at the next login). Anything else - changed, no such account, a file that can not be read - is True, so the
    initial password is never shown when it can not be told that it is still the initial one."""
    try:
        lines = Path(shadow_file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    for line in lines:
        fields = line.split(":")
        if fields[0] == user and len(fields) > 2:
            return fields[2].strip() != "0"
    return True


# ---- the sshd port -------------------------------------------------------------------------------------------------


def _resolve(pattern: str, ssh_dir: Path) -> list[Path]:
    if pattern.startswith(SSH_DIR + "/"):
        pattern = str(ssh_dir / pattern[len(SSH_DIR) + 1 :])
    elif not pattern.startswith("/"):
        pattern = str(ssh_dir / pattern)
    return [Path(p) for p in sorted(glob.glob(pattern))]


def _ports_in(path: Path, ssh_dir: Path, depth: int = 0) -> list[int]:
    if depth > MAX_INCLUDE_DEPTH:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    ports: list[int] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = re.match(r"^(\w+)\s*[=\s]\s*(.*)$", line)
        if parsed is None:
            continue
        key, value = parsed.group(1).lower(), parsed.group(2).strip()
        if key == "match":
            break  # what follows is conditional; Port is not allowed there
        if key == "port":
            first = value.split()[0] if value.split() else ""
            if first.isdigit() and 0 < int(first) < 65536:
                ports.append(int(first))
        elif key == "include":
            for pattern in value.split():
                for included in _resolve(pattern, ssh_dir):
                    ports.extend(_ports_in(included, ssh_dir, depth + 1))
    return ports


def ssh_port(ssh_dir: str | Path = SSH_DIR) -> int:
    """The port sshd listens on: the first `Port` of its configuration (the main file's `Include`d drop-ins are read where
    the Include stands, as sshd does), 22 when none is set."""
    ssh_dir = Path(ssh_dir)
    ports = _ports_in(ssh_dir / "sshd_config", ssh_dir)
    return ports[0] if ports else 22


# ---- the text ------------------------------------------------------------------------------------------------------


def ssh_lines(
    user: str | None,
    address: str | None,
    port: int,
    access: dict[str, str] | None,
    password_changed: Callable[[str], bool],
) -> list[str]:
    if not user:
        return []
    lines = [f"SSH: ssh {user}@{address or NO_ADDRESS} -p {port}"]
    if access and access.get("mode") == "generated":
        password = access.get("password")
        if password and not password_changed(user):
            lines.append(
                f"Password: {password} (you must change it at the first login)"
            )
    elif access and access.get("mode") == "deployer":
        lines.append("(password/key set by the deployer)")
    return lines


def web_lines(config_dir: Path, state_dir: Path, address: str | None) -> list[str]:
    config_file = config_dir / "ginnungagap.yml"
    if config_file.exists():
        try:
            config = config_module.load(config_file)
            url = config["appliance"]["public_url"]
        except config_module.ConfigError:
            return [
                f"A configuration exists in {config_file} but is not valid - check it with:",
                f"  ginnungagap validate {config_file}",
            ]
        lines = [f"Web interface: {url}"]
        if config["services"]["ova_store"]:
            base = render_module.ova_base_url(config)
            port = urlparse(base).port or 80
            lines.append(
                f"OVA server for the ESXi hosts: {base} (no login: allow TCP {port} from the ESXi hosts only)"
            )
        return [
            *lines,
            "First login: admin / admin - change it once you are in (Admin panel > User accounts).",
        ]
    problems_file = state_dir / PROBLEMS
    if problems_file.exists():
        problems = [
            p.strip()
            for p in problems_file.read_text(encoding="utf-8").splitlines()
            if p.strip()
        ]
        return [
            "The configuration given in guestinfo is not valid:",
            *[f"  - {p}" for p in problems[:6]],
            f"It is kept as {config_dir / REJECTED}. Fix it, save it as",
            f"{config_file} and run: ginnungagap apply {config_file}",
        ]
    return [
        f"No configuration yet. Open http://{address or NO_ADDRESS}/ in a browser to",
        "finish setup (the setup wizard) - or set",
        "guestinfo.ginnungagap.config (base64 of ginnungagap.yml) and reboot,",
        "or, as root:",
        f"  cp {config_dir}/ginnungagap.example.yml {config_file}",
        f"  edit {config_file}; ginnungagap apply {config_file}",
    ]


def compose(
    config_dir: Path,
    state_dir: Path,
    ssh_dir: Path,
    address: str | None,
    user: str | None,
    password_changed: Callable[[str], bool],
) -> list[str]:
    ssh = ssh_lines(
        user, address, ssh_port(ssh_dir), read_access(state_dir), password_changed
    )
    web = web_lines(config_dir, state_dir, address)
    return [*ssh, "", *web] if ssh else web


def title(release: str | None) -> str:
    """The first line of the console text: the product and, when it is known, the appliance release it runs."""
    return f"Ginnungagap appliance v{release}" if release else "Ginnungagap appliance"


def write_issue(issue_file: Path, lines: list[str], release: str | None = None) -> None:
    """The text shown at the console login (Debian reads /etc/issue.d/*.issue). A missing message must never stop a boot."""
    try:
        issue_file.parent.mkdir(parents=True, exist_ok=True)
        issue_file.write_text(
            "\n".join(["", title(release), *lines, "", ""]), encoding="utf-8"
        )
    except OSError:
        pass


def refresh(
    config_dir: str | Path = CONFIG_DIR,
    *,
    state_dir: str | Path = STATE_DIR,
    issue_file: str | Path = ISSUE_FILE,
    ssh_dir: str | Path = SSH_DIR,
    local_address_fn: Callable[[], str | None] = local_address,
    login_user_fn: Callable[[], str | None] = login_user,
    password_changed: Callable[[str], bool] | None = None,
    release_fn: Callable[[], str | None] = manifest_module.release_version,
) -> list[str]:
    """Rewrite the console text for the machine's current state. :return: the lines written (for tests and `issue`)."""
    config_dir, state_dir, ssh_dir = Path(config_dir), Path(state_dir), Path(ssh_dir)
    lines = compose(
        config_dir,
        state_dir,
        ssh_dir,
        local_address_fn(),
        login_user_fn(),
        password_changed or shadow_password_changed,
    )
    write_issue(Path(issue_file), lines, release_fn())
    return lines


LOGIN_TERMINALS = tuple(f"tty{number}" for number in range(1, 7))
CLEAR_SCREEN = "\033[H\033[2J\033[3J"  # home, clear, and the scroll-back: an old text must not stay readable


def idle_login_terminals(
    who_output: str, terminals: tuple[str, ...] = LOGIN_TERMINALS
) -> list[str]:
    """The text terminals nobody is logged in on (`who` lists the busy ones as the second column)."""
    busy = {
        line.split()[1] for line in who_output.splitlines() if len(line.split()) > 1
    }
    return [terminal for terminal in terminals if terminal not in busy]


def clear_idle_terminals(device_dir: str | Path = "/dev") -> None:
    """Wipes the screens that wait at a login prompt, so the text of an earlier state (the initial password) is gone."""
    try:
        who = subprocess.run(
            ["who"], check=False, capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for terminal in idle_login_terminals(who):
        try:
            with open(Path(device_dir) / terminal, "w", encoding="utf-8") as screen:
                screen.write(CLEAR_SCREEN)
        except OSError:
            continue


def reload_login_prompt(clear_screens: bool = False) -> None:
    """Ask a running login prompt to read the text again (agetty --reload); not being able to is fine."""
    if clear_screens:
        clear_idle_terminals()
    try:
        subprocess.run(["agetty", "--reload"], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
