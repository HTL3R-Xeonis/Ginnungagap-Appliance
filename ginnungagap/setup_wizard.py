"""
The setup wizard's own logic: a plain HTML form -> a `ginnungagap.yml` candidate, validated by the very same code the
command line's `validate`/`apply` use (`ginnungagap.config.normalise`, never a second copy of the rules). Nothing here
imports FastAPI: the wizard's web container (`docker/setup_app.py`) is a thin shim around this module so the form logic,
the YAML it builds and the hand-off file it writes can be tested without a web framework installed at all.

Field names are plain strings matching the <input name="..."> of the rendered page; both directions (building the page,
reading the submitted values back) go through the same name constants so a typo shows up as a failing test, not a
silently-ignored field.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from ginnungagap import config as config_module

LLM_ROWS = 5  # fixed number of blank "external Ollama server" rows the form offers (no JS to add more)
HOSTED_PRESETS = tuple(
    config_module.HOSTED_PRESETS
)  # ("gemini", "groq", "openrouter"), in a fixed order
MARKER = "apply.request"
STAGED = "ginnungagap.yml"


def _get(values: Mapping[str, str], key: str, default: str = "") -> str:
    value = values.get(key, default)
    return value if value is not None else default


def _checked(values: Mapping[str, str], key: str) -> bool:
    return _get(values, key) not in ("", "off", "0", "false", "False")


def _int(values: Mapping[str, str], key: str, default: int) -> int | str:
    """The raw text if it doesn't parse as a whole number, so `config.normalise` reports it (not this module guessing)."""
    text = _get(values, key).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return text


def build_raw_config(
    values: Mapping[str, str],
    hash_password: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """
    The submitted form as the nested dict `config.normalise` expects - not validated here, that is the one job left to
    `config.normalise` itself. `hash_password` turns a freshly typed plaintext password into the bcrypt hash the config
    needs (only called when one was actually typed); its own failures are not caught here, they surface as a 500 to the
    person submitting, same as any other server error.
    """
    auth_mode = _get(values, "auth_mode", "none")
    password_hash = ""
    if auth_mode == "basic":
        typed = _get(values, "auth_password")
        if typed and hash_password is not None:
            password_hash = hash_password(typed)
        elif typed:
            password_hash = typed  # a test double without real hashing; config.normalise will reject it, which is fine

    ova_mode = _get(values, "ova_deploy_mode", "external")
    store_type = _get(values, "store_type", "local")

    llm_servers = []
    for index in range(1, LLM_ROWS + 1):
        name = _get(values, f"llm_{index}_name").strip()
        url = _get(values, f"llm_{index}_url").strip()
        model = _get(values, f"llm_{index}_model").strip()
        if (
            name or url or model
        ):  # a half-filled row is still reported, by config.normalise, not silently dropped
            llm_servers.append({"name": name, "url": url, "model": model})

    hosted: dict[str, Any] = {}
    for name in HOSTED_PRESETS:
        if not _checked(values, f"hosted_{name}_enabled"):
            continue
        entry: dict[str, Any] = {"api_key": _get(values, f"hosted_{name}_api_key")}
        base_url = _get(values, f"hosted_{name}_base_url").strip()
        model = _get(values, f"hosted_{name}_model").strip()
        if base_url:
            entry["base_url"] = base_url
        if model:
            entry["model"] = model
        hosted[name] = entry
    custom_name = _get(values, "hosted_custom_name").strip()
    if custom_name:
        hosted[custom_name] = {
            "api_key": _get(values, "hosted_custom_api_key"),
            "base_url": _get(values, "hosted_custom_base_url").strip(),
            "model": _get(values, "hosted_custom_model").strip(),
        }

    return {
        "version": config_module.SUPPORTED_VERSION,
        "appliance": {
            "public_url": _get(values, "public_url", "http://localhost"),
            "timezone": _get(values, "timezone", "UTC"),
            "tls": {"mode": _get(values, "tls_mode", "off")},
            "auth": {
                "mode": auth_mode,
                "user": _get(values, "auth_user"),
                "password_hash": password_hash,
            },
            "legacy_ports": _checked(values, "legacy_ports"),
            "http_port": _int(values, "http_port", 80),
            "https_port": _int(values, "https_port", 443),
        },
        "services": {
            "template_esxi": _get(values, "esxi_mode", "bundled") == "bundled",
            "template_gns3": _get(values, "gns3_mode", "bundled") == "bundled",
            "generator": _get(values, "generator_mode", "bundled") == "bundled",
            "ova_deploy": {"true": True, "external": "external", "off": "off"}.get(
                ova_mode, ova_mode
            ),
            "filebrowser": False,
        },
        "templates": {
            "store": {
                "type": store_type,
                "local": {"path": _get(values, "store_local_path", "/srv/templates")},
                "nfs": {
                    "server": _get(values, "store_nfs_server"),
                    "export": _get(values, "store_nfs_export"),
                },
            },
            "esxi_refresh_minutes": _int(values, "esxi_refresh_minutes", 15),
            "gns3_refresh_minutes": _int(values, "gns3_refresh_minutes", 5),
            "esxi_url": _get(values, "esxi_url"),
            "gns3_url": _get(values, "gns3_url"),
        },
        "generator": {
            "url": _get(values, "generator_url"),
            "llm_servers": llm_servers,
            "hosted": hosted,
            "max_retries": _int(values, "generator_max_retries", 3),
            "request_timeout_seconds": _int(values, "generator_timeout_seconds", 500),
        },
        "ova_deploy": {
            "external_url": _get(values, "ova_external_url"),
            "esxi_password": _get(values, "ova_esxi_password"),
            "timeout_seconds": _int(values, "ova_timeout_seconds", 600),
        },
        "logging": {
            "level": _get(values, "logging_level", "INFO"),
            "max_lines": _int(values, "logging_max_lines", 100000),
        },
    }


def keep_existing_secrets(
    raw: dict[str, Any], existing: dict[str, Any] | None
) -> dict[str, Any]:
    """
    The form never shows a stored secret back, and says "leave blank to keep the current one" - this is what makes that
    true: a secret left blank in the submitted form takes the value of the configuration already applied (if it had
    one), instead of being validated as missing (password hash, API key) or silently wiped (ESXi fallback password).
    """
    if existing is None:
        return raw
    auth, old_auth = raw["appliance"]["auth"], existing["appliance"]["auth"]
    if (
        auth["mode"] == "basic"
        and not auth["password_hash"]
        and old_auth.get("mode") == "basic"
    ):
        auth["password_hash"] = old_auth.get("password_hash", "")
    old_hosted = existing["generator"]["hosted"]
    for name, entry in raw["generator"]["hosted"].items():
        if not entry["api_key"] and name in old_hosted:
            entry["api_key"] = old_hosted[name].get("api_key", "")
    if not raw["ova_deploy"]["esxi_password"]:
        raw["ova_deploy"]["esxi_password"] = existing["ova_deploy"]["esxi_password"]
    return raw


def render_yaml(config: dict[str, Any]) -> str:
    return yaml.safe_dump(config, sort_keys=False)


def write_staged(staging_dir: Path, yaml_text: str) -> None:
    """
    Writes the candidate config into the folder only this container and the host's watcher share, then signals it with a
    marker file - written last, so the watcher (ginnungagap.setup_watcher) never sees a marker before the file it points
    at is completely there. The container has no other way to reach the host: no Docker socket, no sudo, nothing else
    mounted writable.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / STAGED
    tmp = staging_dir / (STAGED + ".new")
    tmp.write_text(yaml_text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(staged)
    (staging_dir / MARKER).write_text("", encoding="utf-8")


def existing_config(config_dir: Path) -> dict[str, Any] | None:
    """The configuration already applied, normalised - None when there is none yet, or it doesn't even parse."""
    path = config_dir / "ginnungagap.yml"
    if not path.is_file():
        return None
    try:
        return config_module.load(path)
    except config_module.ConfigError:
        return None  # an invalid file on disk is treated the same as none: the form starts from defaults either way


def prefill_from(config: dict[str, Any] | None) -> dict[str, str]:
    """
    The current (redacted) values as form field strings, for the GET page - a secret is never shown back, only the fact
    that one is already set (the field is left for "leave blank to keep the current value", enforced by `keep_existing_secrets`, not
    guessed at here).
    """
    if config is None:
        config = config_module.DEFAULTS
    appliance, services, templates = (
        config["appliance"],
        config["services"],
        config["templates"],
    )
    generator, ova, logging_cfg = (
        config["generator"],
        config["ova_deploy"],
        config["logging"],
    )
    store = templates["store"]
    values: dict[str, str] = {
        "public_url": appliance["public_url"],
        "timezone": appliance["timezone"],
        "tls_mode": appliance["tls"]["mode"],
        "auth_mode": appliance["auth"].get("mode", "none"),
        "auth_user": appliance["auth"].get("user", ""),
        "legacy_ports": "on" if appliance["legacy_ports"] else "",
        "http_port": str(appliance["http_port"]),
        "https_port": str(appliance["https_port"]),
        "esxi_mode": "bundled" if services["template_esxi"] else "external",
        "gns3_mode": "bundled" if services["template_gns3"] else "external",
        "generator_mode": "bundled" if services["generator"] else "external",
        "esxi_url": templates["esxi_url"],
        "gns3_url": templates["gns3_url"],
        "generator_url": generator["url"],
        "ova_deploy_mode": {True: "true", "external": "external", "off": "off"}.get(
            services["ova_deploy"], "external"
        ),
        "ova_external_url": ova["external_url"],
        "ova_timeout_seconds": str(ova["timeout_seconds"]),
        "store_type": store["type"],
        "store_local_path": store.get("local", {}).get("path", ""),
        "store_nfs_server": store.get("nfs", {}).get("server", ""),
        "store_nfs_export": store.get("nfs", {}).get("export", ""),
        "esxi_refresh_minutes": str(templates["esxi_refresh_minutes"]),
        "gns3_refresh_minutes": str(templates["gns3_refresh_minutes"]),
        "generator_max_retries": str(generator["max_retries"]),
        "generator_timeout_seconds": str(generator["request_timeout_seconds"]),
        "logging_level": logging_cfg["level"],
        "logging_max_lines": str(logging_cfg["max_lines"]),
    }
    for index, server in enumerate(generator["llm_servers"][:LLM_ROWS], start=1):
        values[f"llm_{index}_name"] = server.get("name", "")
        values[f"llm_{index}_url"] = server.get("url", "")
        values[f"llm_{index}_model"] = server.get("model", "")
    for name in HOSTED_PRESETS:
        if name in generator["hosted"]:
            values[f"hosted_{name}_enabled"] = "on"
    return values


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _field(
    values: Mapping[str, str], name: str, *, kind: str = "text", placeholder: str = ""
) -> str:
    value = _esc(_get(values, name))
    ph = f' placeholder="{_esc(placeholder)}"' if placeholder else ""
    return f'<input type="{kind}" name="{name}" value="{value}"{ph}>'


def render_page(
    values: Mapping[str, str],
    problems: list[str] | None = None,
    message: str | None = None,
    has_existing: bool = False,
) -> str:
    """The whole wizard page: no JavaScript, conditional fields shown/hidden with a plain CSS sibling-selector trick
    driven by the radio buttons themselves (":checked ~ .reveal"), so the browser needs nothing beyond CSS."""
    problems = problems or []
    problem_html = ""
    if problems:
        items = "".join(f"<li>{_esc(p)}</li>" for p in problems)
        problem_html = f'<div class="problems"><strong>The configuration was not accepted:</strong><ul>{items}</ul></div>'
    message_html = f'<div class="message">{_esc(message)}</div>' if message else ""
    existing_note = (
        '<p class="note">A configuration is already applied. Secret fields are left blank here - leave them blank to '
        "keep the current value, or type a new one to replace it.</p>"
        if has_existing
        else ""
    )

    def radio(group: str, value: str, label: str, reveal_id: str | None = None) -> str:
        checked = (
            " checked"
            if _get(values, group) == value
            or (
                not _get(values, group)
                and value in ("bundled", "none", "off", "local", "true")
            )
            else ""
        )
        peer = f' class="toggle" data-reveal="{reveal_id}"' if reveal_id else ""
        return f'<label><input type="radio" name="{group}" value="{value}"{checked}{peer}> {label}</label>'

    def service_block(key: str, label: str, url_field: str) -> str:
        return f"""
        <fieldset class="service">
          <legend>{label}</legend>
          {radio(f"{key}_mode", "bundled", "Runs on this appliance")}
          {radio(f"{key}_mode", "external", "Runs elsewhere", f"{key}-external")}
          <div class="reveal" data-shows-when='{key}_mode=external'>
            <label>Address of the one that runs elsewhere {_field(values, url_field, placeholder="http://10.0.0.1:8000")}</label>
          </div>
        </fieldset>"""

    llm_rows = "".join(
        f"""
        <div class="llm-row">
          <input type="text" name="llm_{i}_name" value="{_esc(_get(values, f"llm_{i}_name"))}" placeholder="name">
          <input type="text" name="llm_{i}_url" value="{_esc(_get(values, f"llm_{i}_url"))}" placeholder="http://10.0.0.1:11434">
          <input type="text" name="llm_{i}_model" value="{_esc(_get(values, f"llm_{i}_model"))}" placeholder="model">
        </div>"""
        for i in range(1, LLM_ROWS + 1)
    )

    hosted_rows = "".join(
        f"""
        <div class="hosted-row">
          <label><input type="checkbox" name="hosted_{name}_enabled" {"checked" if _checked(values, f"hosted_{name}_enabled") else ""}> {name}</label>
          <input type="password" name="hosted_{name}_api_key" placeholder="API key (leave blank to keep the current one)">
          <input type="text" name="hosted_{name}_base_url" value="{_esc(_get(values, f"hosted_{name}_base_url"))}" placeholder="base URL (optional, preset default otherwise)">
          <input type="text" name="hosted_{name}_model" value="{_esc(_get(values, f"hosted_{name}_model"))}" placeholder="model (optional, preset default otherwise)">
        </div>"""
        for name in HOSTED_PRESETS
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ginnungagap - Setup</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #1c1c1c; color: #e8e8e8; margin: 0; padding: 24px; }}
  main {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 20px; }}
  fieldset {{ border: 1px solid #444; border-radius: 8px; margin-bottom: 18px; padding: 14px 16px; }}
  legend {{ color: #b6d98a; font-weight: 600; }}
  label {{ display: block; margin: 8px 0 4px; font-size: 13px; color: #c8c8c8; }}
  input[type=text], input[type=password], input[type=number], select {{
    width: 100%; box-sizing: border-box; background: #262626; border: 1px solid #444;
    color: #eee; border-radius: 6px; padding: 7px 9px; margin-top: 2px;
  }}
  .llm-row, .hosted-row {{ display: flex; gap: 8px; margin-bottom: 6px; align-items: center; }}
  .llm-row input, .hosted-row input[type=text], .hosted-row input[type=password] {{ flex: 1; }}
  .reveal {{ display: none; margin-top: 8px; }}
  input.toggle {{ display: inline-block; width: auto; margin-right: 6px; }}
  .service label:has(> input.toggle:checked) ~ .reveal {{ display: block; }}
  .problems {{ background: #3a2020; border: 1px solid #a04040; border-radius: 6px; padding: 10px 14px; margin-bottom: 16px; }}
  .message {{ background: #20331f; border: 1px solid #4b7a3f; border-radius: 6px; padding: 10px 14px; margin-bottom: 16px; }}
  .note {{ color: #9a9; font-size: 13px; }}
  button {{ padding: 9px 16px; border-radius: 6px; border: none; cursor: pointer; font-size: 14px; margin-right: 8px; }}
  button[name=action][value=apply] {{ background: #7cb342; color: #1c1c1c; font-weight: 600; }}
  button[name=action][value=download] {{ background: #333; color: #eee; border: 1px solid #555; }}
</style>
</head>
<body>
<main>
  <h1>Ginnungagap appliance - setup</h1>
  {existing_note}
  {message_html}
  {problem_html}
  <form method="post" action="">
    <fieldset>
      <legend>Appliance</legend>
      <label>Public URL {_field(values, "public_url", placeholder="https://ginnungagap.lab.example")}</label>
      <label>Timezone {_field(values, "timezone", placeholder="Europe/Vienna")}</label>
      <label>TLS
        <select name="tls_mode">
          <option value="off" {"selected" if _get(values, "tls_mode", "off") == "off" else ""}>off</option>
          <option value="self-signed" {"selected" if _get(values, "tls_mode") == "self-signed" else ""}>self-signed</option>
        </select>
      </label>
      <label><input type="checkbox" name="legacy_ports" {"checked" if _checked(values, "legacy_ports") else ""}> also publish the legacy ports (8123, 8000-8002)</label>
      <label>HTTP port {_field(values, "http_port", kind="number")}</label>
      <label>HTTPS port {_field(values, "https_port", kind="number")}</label>
    </fieldset>

    <fieldset class="service">
      <legend>Login</legend>
      {radio("auth_mode", "none", "No login at the gateway")}
      {radio("auth_mode", "basic", "Basic auth at the gateway", "auth")}
      <div class="reveal" data-shows-when="auth_mode=basic">
        <label>User {_field(values, "auth_user", placeholder="admin")}</label>
        <label>Password <input type="password" name="auth_password" placeholder="leave blank to keep the current one"></label>
      </div>
    </fieldset>

    {service_block("esxi", "ESXi template API", "esxi_url")}
    {service_block("gns3", "GNS3 template API", "gns3_url")}
    {service_block("generator", "Topology generator", "generator_url")}

    <fieldset class="service">
      <legend>OVA deploy</legend>
      {radio("ova_deploy_mode", "true", "Runs on this appliance")}
      {radio("ova_deploy_mode", "external", "Runs elsewhere", "ova-ext")}
      {radio("ova_deploy_mode", "off", "Switched off")}
      <div class="reveal" data-shows-when="ova_deploy_mode=external">
        <label>Address of the one that runs elsewhere {_field(values, "ova_external_url", placeholder="http://10.20.20.172:8003")}</label>
      </div>
      <label>Fallback ESXi password <input type="password" name="ova_esxi_password" placeholder="leave blank to keep the current one"></label>
      <label>Timeout (seconds) {_field(values, "ova_timeout_seconds", kind="number")}</label>
    </fieldset>

    <fieldset class="service">
      <legend>Template store</legend>
      {radio("store_type", "local", "Local folder")}
      {radio("store_type", "nfs", "NFS export", "nfs")}
      <label>Local path {_field(values, "store_local_path", placeholder="/srv/templates")}</label>
      <div class="reveal" data-shows-when="store_type=nfs">
        <label>NFS server {_field(values, "store_nfs_server", placeholder="10.20.20.180")}</label>
        <label>NFS export {_field(values, "store_nfs_export", placeholder="/mnt/pool/templates")}</label>
      </div>
      <label>ESXi refresh (minutes) {_field(values, "esxi_refresh_minutes", kind="number")}</label>
      <label>GNS3 refresh (minutes) {_field(values, "gns3_refresh_minutes", kind="number")}</label>
    </fieldset>

    <fieldset>
      <legend>Generator - external Ollama servers (leave a row blank to skip it)</legend>
      {llm_rows}
    </fieldset>

    <fieldset>
      <legend>Generator - hosted LLM services</legend>
      {hosted_rows}
      <div class="hosted-row">
        <input type="text" name="hosted_custom_name" placeholder="other service name">
        <input type="text" name="hosted_custom_base_url" placeholder="base URL">
        <input type="text" name="hosted_custom_model" placeholder="model">
        <input type="password" name="hosted_custom_api_key" placeholder="API key">
      </div>
      <label>Max retries {_field(values, "generator_max_retries", kind="number")}</label>
      <label>Request timeout (seconds) {_field(values, "generator_timeout_seconds", kind="number")}</label>
    </fieldset>

    <fieldset>
      <legend>Logging</legend>
      <label>Level
        <select name="logging_level">
          {"".join(f'<option value="{lvl}" {"selected" if _get(values, "logging_level", "INFO") == lvl else ""}>{lvl}</option>' for lvl in ("DEBUG", "INFO", "WARNING", "ERROR"))}
        </select>
      </label>
      <label>Max lines kept {_field(values, "logging_max_lines", kind="number")}</label>
    </fieldset>

    <button type="submit" name="action" value="apply">Apply</button>
    <button type="submit" name="action" value="download">Download the YAML instead</button>
  </form>
</main>
</body>
</html>"""
