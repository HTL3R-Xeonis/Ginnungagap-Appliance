"""
Loading and checking the one configuration file (ginnungagap.yml).

The file holds everything that is *not* about a host: which services run, where the template store is, the LLM servers
and their keys, the URLs of services that run elsewhere. Hosts (ESXi + GNS3 VM) stay what they are in the app: profiles
created in its GUI. Everything is checked as a whole and every problem is reported, so a bad file changes nothing.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_VERSION = 1

# Presets for hosted LLM services: the name under generator.hosted, with the OpenAI-compatible endpoint and a default model.
HOSTED_PRESETS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.8-flash",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
    },
}

DEFAULTS: dict[str, Any] = {
    "version": SUPPORTED_VERSION,
    "appliance": {
        "public_url": "http://localhost",
        "timezone": "UTC",
        "tls": {"mode": "off"},
        "auth": {"mode": "none", "user": "", "password_hash": ""},
        "legacy_ports": False,
        "http_port": 80,
        "https_port": 443,
    },
    "services": {
        "template_esxi": True,
        "template_gns3": True,
        "generator": True,
        "ova_store": True,
        "filebrowser": False,
    },
    "templates": {
        "store": {
            "type": "local",
            "manage": False,
            "local": {"path": "/srv/templates"},
            "nfs": {"server": "", "export": ""},
        },
        "esxi_refresh_minutes": 15,
        "gns3_refresh_minutes": 5,
        "esxi_url": "",
        "gns3_url": "",
        "ova_base_url": "",
        "ova_port": 8088,
    },
    "generator": {
        "url": "",
        "llm_servers": [],
        "hosted": {},
        "max_retries": 3,
        "request_timeout_seconds": 500,
    },
    "logging": {"level": "INFO", "max_lines": 100000},
    # Updating the installed components (`ginnungagap update`). The sources are public: nothing here needs a token, and a token
    # never goes into this file - `token_file` only names the (optional, 0600) file `ginnungagap update --set-token` writes.
    "update": {
        "source": "github",
        "repo": "HTL3R-Xeonis/Ginnungagap-Appliance",
        "directory": "",
        "channel": "stable",
        "token_file": "/etc/ginnungagap/update-token",
        "check": "off",
        "require_signature": "auto",
        "keep_releases": 2,
        "step_timeout_seconds": 90,
    },
}

# settings people may expect from the design but that are deliberately not part of the appliance
_EXCLUDED = {
    "services.monitoring": "monitoring is not part of the one-VM solution (it runs separately)",
    "ova_deploy": "was removed: the ESXi hosts pull the OVAs themselves over HTTP - delete this section and see "
    "services.ova_store and templates.ova_base_url",
    "services.ova_deploy": "was removed: the ESXi hosts pull the OVAs themselves over HTTP - delete this line and see "
    "services.ova_store",
    **{
        f"update.{name}": "a token is never written into this file - hand it over with `ginnungagap update --set-token` "
        "(it ends up in update.token_file, readable by root only); public sources need none"
        for name in ("token", "access_token", "github_token", "api_key")
    },
}

# the ports compose.legacy-ports.yml publishes
LEGACY_PORTS = [8123, 8000, 8001, 8002]

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")
_URL = re.compile(r"^https?://[^\s/]+(/\S*)?$")
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$")
UPDATE_SOURCES = ("github", "directory")
UPDATE_CHANNELS = ("stable", "prerelease")
UPDATE_CHECKS = ("off", "daily")


class ConfigError(ValueError):
    """The configuration is not usable; `problems` lists everything that is wrong, one line each."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value is None and isinstance(result.get(key), (dict, list)):
            continue  # a section that holds only comments is null in YAML: it means "nothing set", keep the default
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _unknown_keys(given: dict[str, Any], known: dict[str, Any], path: str) -> list[str]:
    problems = []
    for key, value in given.items():
        if key not in known:
            reason = _EXCLUDED.get(f"{path}{key}", "unknown setting (a typo?)")
            problems.append(f"{path}{key}: {reason}")
        elif (
            known[key] and isinstance(known[key], dict) and isinstance(value, dict)
        ):  # an empty default is free-form
            problems += _unknown_keys(value, known[key], f"{path}{key}.")
    return problems


def _is_url(value: Any) -> bool:
    return isinstance(value, str) and bool(_URL.match(value))


def _check(config: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    appliance, services, templates = (
        config["appliance"],
        config["services"],
        config["templates"],
    )
    generator, logging_cfg = config["generator"], config["logging"]

    if config["version"] != SUPPORTED_VERSION:
        problems.append(
            f"version: must be {SUPPORTED_VERSION}, not {config['version']!r}"
        )

    if not _is_url(appliance["public_url"]):
        problems.append("appliance.public_url: must be an http:// or https:// address")
    if appliance["tls"]["mode"] not in ("off", "self-signed"):
        problems.append(
            "appliance.tls.mode: must be off or self-signed (file and acme are not available yet)"
        )
    if appliance["tls"]["mode"] == "self-signed" and str(
        appliance["public_url"]
    ).startswith("http://"):
        problems.append("appliance.tls.mode: self-signed needs an https:// public_url")
    for port in ("http_port", "https_port"):
        value = appliance[port]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 65535
        ):
            problems.append(f"appliance.{port}: must be a port number")
    if not isinstance(appliance["legacy_ports"], bool):
        problems.append("appliance.legacy_ports: must be true or false")
    auth = appliance["auth"]
    if auth.get("mode") not in ("none", "basic"):
        problems.append("appliance.auth.mode: must be none or basic")
    elif auth["mode"] == "basic":
        if not auth.get("user") or not re.fullmatch(
            r"[A-Za-z0-9._-]+", str(auth.get("user", ""))
        ):
            problems.append(
                "appliance.auth.user: needed for basic auth (letters, digits, . _ -)"
            )
        if not str(auth.get("password_hash", "")).startswith("$2"):
            problems.append(
                "appliance.auth.password_hash: a bcrypt hash is needed (never the password itself)"
            )

    for name in ("template_esxi", "template_gns3", "generator", "ova_store"):
        if not isinstance(services[name], bool):
            problems.append(f"services.{name}: must be true or false")
    if services["filebrowser"] is not False:
        problems.append(
            "services.filebrowser: is not available in this version yet, leave it false"
        )

    store = templates["store"]
    if not isinstance(store.get("manage"), bool):
        problems.append("templates.store.manage: must be true or false")
    if store.get("type") not in ("local", "nfs"):
        problems.append("templates.store.type: must be local or nfs")
    elif store["type"] == "local":
        path = str(store.get("local", {}).get("path", ""))
        if not path.startswith("/"):
            problems.append("templates.store.local.path: must be an absolute path")
    else:
        nfs = store.get("nfs") or {}
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(nfs.get("server", ""))):
            problems.append(
                "templates.store.nfs.server: a host name or address is needed"
            )
        if not str(nfs.get("export", "")).startswith("/"):
            problems.append("templates.store.nfs.export: must be an absolute path")
    for name in ("esxi_refresh_minutes", "gns3_refresh_minutes"):
        value = templates[name]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= 1440
        ):
            problems.append(
                f"templates.{name}: must be a whole number of minutes (1 to 1440)"
            )
    for service, url_key, owner in (
        ("template_esxi", "esxi_url", "templates"),
        ("template_gns3", "gns3_url", "templates"),
        ("generator", "url", "generator"),
    ):
        url = config[owner][url_key]
        if services[service] is False and not _is_url(url):
            problems.append(
                f"{owner}.{url_key}: services.{service} is false, so the address of the one that runs elsewhere is needed"
            )
        elif url and not _is_url(url):
            problems.append(
                f"{owner}.{url_key}: must be an http:// or https:// address"
            )

    problems += _check_generator(generator, required=services["generator"] is True)

    problems += _check_ova_store(config)
    if logging_cfg["level"] not in _LEVELS:
        problems.append(f"logging.level: must be one of {', '.join(_LEVELS)}")
    problems += _check_update(config["update"])
    return problems


def _whole(value: Any, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def _check_update(update: dict[str, Any]) -> list[str]:
    """The update settings. Nothing here is a secret and nothing needs a token: the sources are public."""
    problems: list[str] = []
    if update["source"] not in UPDATE_SOURCES:
        problems.append("update.source: must be github or directory")
    elif update["source"] == "github":
        if not isinstance(update["repo"], str) or not _REPO.match(update["repo"]):
            problems.append("update.repo: must look like owner/repository")
    elif not str(update["directory"]).startswith("/"):
        problems.append(
            "update.directory: update.source is directory, so the absolute path of the folder with the release files is needed"
        )
    if update["directory"] and not str(update["directory"]).startswith("/"):
        problems.append("update.directory: must be an absolute path")
    if update["channel"] not in UPDATE_CHANNELS:
        problems.append("update.channel: must be stable or prerelease")
    token_file = update["token_file"]
    if not isinstance(token_file, str) or (
        token_file and not token_file.startswith("/")
    ):
        problems.append(
            "update.token_file: must be an absolute path (or empty for no token file); it is only read when it exists"
        )
    if update["check"] not in UPDATE_CHECKS:
        problems.append("update.check: must be off or daily")
    signature = update["require_signature"]
    if signature != "auto" and not isinstance(signature, bool):
        problems.append("update.require_signature: must be auto, true or false")
    if not _whole(update["keep_releases"], 2, 20):
        problems.append(
            "update.keep_releases: must be a whole number from 2 (the current and the previous release) to 20"
        )
    if not _whole(update["step_timeout_seconds"], 10, 3600):
        problems.append(
            "update.step_timeout_seconds: must be a whole number of seconds (10 to 3600)"
        )
    return problems


def _check_ova_store(config: dict[str, Any]) -> list[str]:
    """The OVA server the ESXi hosts pull from: its port (when it runs here) and the address they reach it at."""
    appliance, services, templates = (
        config["appliance"],
        config["services"],
        config["templates"],
    )
    problems: list[str] = []
    port, base_url = templates["ova_port"], templates["ova_base_url"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        problems.append("templates.ova_port: must be a port number")
    elif services["ova_store"] is True:
        taken = [appliance["http_port"], appliance["https_port"]]
        if appliance["legacy_ports"] is True:
            taken += LEGACY_PORTS
        if port in taken:
            problems.append(
                f"templates.ova_port: {port} is already used by the gateway or a legacy port, choose another one"
            )
    if base_url and not _is_url(base_url):
        problems.append(
            "templates.ova_base_url: must be an http:// or https:// address"
        )
    elif services["ova_store"] is False and not base_url:
        problems.append(
            "templates.ova_base_url: services.ova_store is false, so the address the ESXi hosts reach the OVAs at is needed"
        )
    return problems


def _check_generator(generator: dict[str, Any], required: bool) -> list[str]:
    problems: list[str] = []
    names: list[str] = []
    servers = generator["llm_servers"]
    if not isinstance(servers, list):
        return ["generator.llm_servers: must be a list"]
    for index, server in enumerate(servers):
        where = f"generator.llm_servers[{index}]"
        if not isinstance(server, dict):
            problems.append(f"{where}: must be a mapping with name, url and model")
            continue
        unknown = set(server) - {"name", "url", "model"}
        if unknown:
            problems.append(f"{where}: unknown setting {', '.join(sorted(unknown))}")
        if not _NAME.match(str(server.get("name", ""))):
            problems.append(f"{where}.name: needed (letters, digits, - _)")
        if not _is_url(server.get("url")):
            problems.append(f"{where}.url: must be an http:// or https:// address")
        if not server.get("model"):
            problems.append(f"{where}.model: needed")
        names.append(str(server.get("name", "")))
    hosted = generator["hosted"]
    if not isinstance(hosted, dict):
        return [
            *problems,
            "generator.hosted: must be a mapping of service name to its settings",
        ]
    for name, settings in hosted.items():
        where = f"generator.hosted.{name}"
        if not _NAME.match(str(name)):
            problems.append(
                f"{where}: the name may only contain letters, digits, - and _"
            )
        if not isinstance(settings, dict) or not settings.get("api_key"):
            problems.append(f"{where}.api_key: needed")
            continue
        if not re.fullmatch(r"\S+", str(settings["api_key"])):
            problems.append(
                f"{where}.api_key: must be one word without spaces or line breaks"
            )
        unknown = set(settings) - {"api_key", "base_url", "model"}
        if unknown:
            problems.append(f"{where}: unknown setting {', '.join(sorted(unknown))}")
        preset = HOSTED_PRESETS.get(str(name), {})
        if not (settings.get("base_url") or preset.get("base_url")):
            problems.append(f"{where}.base_url: needed (no preset for this name)")
        if not (settings.get("model") or preset.get("model")):
            problems.append(f"{where}.model: needed (no preset for this name)")
        names.append(str(name))
    problems += [
        f"generator: the name {n!r} is used twice"
        for n in sorted({n for n in names if names.count(n) > 1})
    ]
    if required and not servers and not hosted:
        problems.append(
            "generator: at least one LLM server (llm_servers or hosted) is needed while the generator runs"
        )
    if not isinstance(generator["max_retries"], int) or generator["max_retries"] < 1:
        problems.append("generator.max_retries: must be a positive number")
    if (
        not isinstance(generator["request_timeout_seconds"], int)
        or generator["request_timeout_seconds"] < 1
    ):
        problems.append("generator.request_timeout_seconds: must be a positive number")
    return problems


def normalise(raw: Any) -> dict[str, Any]:
    """The given settings on top of the defaults, checked. :raises ConfigError: with every problem found."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(["the file must contain a mapping at its top level"])
    problems = _unknown_keys(raw, DEFAULTS, "")
    config = _merge(DEFAULTS, raw)
    for section in (
        "appliance",
        "services",
        "templates",
        "generator",
        "logging",
        "update",
    ):
        if not isinstance(config[section], dict):
            problems.append(f"{section}: must be a mapping")
    if any("must be a mapping" in problem for problem in problems):
        raise ConfigError(problems)
    # YAML 1.1 reads an unquoted `off` as false (and `on` as true): `mode: off` is what people write
    if (
        isinstance(config["appliance"]["tls"], dict)
        and config["appliance"]["tls"].get("mode") is False
    ):
        config["appliance"]["tls"]["mode"] = "off"
    _read_update_words(config["update"])
    try:
        problems += _check(config)
    except (TypeError, KeyError, AttributeError) as exc:
        problems.append(
            f"a setting has the wrong kind of value ({type(exc).__name__}) - compare it with ginnungagap.example.yml"
        )
    if problems:
        raise ConfigError(problems)
    return config


def _read_update_words(update: dict[str, Any]) -> None:
    """YAML 1.1 turns an unquoted `off` into false and `on` into true; `check: off` and `require_signature: true` are what people write."""
    if update.get("check") is False:
        update["check"] = "off"
    signature = update.get("require_signature")
    if isinstance(signature, str) and signature.lower() in (
        "true",
        "false",
        "yes",
        "no",
    ):
        update["require_signature"] = signature.lower() in ("true", "yes")


def load(path: str | Path) -> dict[str, Any]:
    """Reads and checks a configuration file. :raises ConfigError: it cannot be read or is not valid."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError([f"{path}: can't be read ({exc.strerror})"]) from exc
    except yaml.YAMLError as exc:
        raise ConfigError([f"{path}: isn't valid YAML ({exc})"]) from exc
    return normalise(raw)


def redacted(config: dict[str, Any]) -> dict[str, Any]:
    """A copy that is safe to print: keys and hashes replaced."""
    shown = copy.deepcopy(config)
    for settings in shown["generator"]["hosted"].values():
        if isinstance(settings, dict) and "api_key" in settings:
            settings["api_key"] = "<hidden>"
    if "password_hash" in shown["appliance"]["auth"]:
        shown["appliance"]["auth"]["password_hash"] = "<hidden>"
    return shown


def update_check_is_daily(path: str | Path) -> bool:
    """True when the file is valid and asks for the daily update check (`update.check: daily`). Never raises."""
    try:
        return load(path)["update"]["check"] == "daily"
    except ConfigError:
        return False


def main(argv: list[str] | None = None) -> int:
    """`python3 -m ginnungagap.config update-check-daily FILE`: exit 0 when the daily check is switched on, 1 otherwise.

    The condition of ginnungagap-update-check.service: the timer is always armed, and this is what keeps it silent
    until the configuration says `update.check: daily` (so changing the setting needs no systemctl and no reboot).
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "update-check-daily":
        return 0 if update_check_is_daily(args[1]) else 1
    print(
        "usage: python3 -m ginnungagap.config update-check-daily FILE", file=sys.stderr
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
