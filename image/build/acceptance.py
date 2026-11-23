"""Acceptance gates for the exported image, on new, run-owned VMs only."""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import ipaddress
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import names, remote
from .inspect_image import row
from .ports import (
    Blocked,
    CommandFailed,
    PermanentError,
    RemoteTarget,
    Timeout,
    Unreachable,
)

BOOT_TIMEOUT = 600.0
CHECK_TIMEOUT = 300.0
POLL = 5.0
_MINIMAL = Path(__file__).with_name("minimal-config.yml")


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self):
        return json.loads(self.body)


class HttpClient:
    """One cookie jar per acceptance VM; credentials never appear in URLs."""

    def __init__(self, address: str):
        self.base = f"http://{address}"
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.basic: tuple[str, str] | None = None

    def request(
        self, path: str, *, method: str = "GET", form=None, headers=None, port=None
    ) -> HttpResult:
        origin = self.base if port is None else self.base + f":{port}"
        request_headers = dict(headers or {})
        if self.basic:
            request_headers["Authorization"] = (
                "Basic " + base64.b64encode(":".join(self.basic).encode()).decode()
            )
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        request = urllib.request.Request(
            origin + path, data=data, headers=request_headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, urllib.error.URLError):
            return HttpResult(0, b"", {})
        with response:
            return HttpResult(
                response.code,
                response.read(2 * 1024 * 1024),
                {key.lower(): value for key, value in response.headers.items()},
            )


# This is the pinned tooling's probe, not code read from a release. Requests
# carrying passwords travel only over RemotePort.run(stdin=...), never argv.
_GUEST_PROBE = r"""
import base64,hashlib,json,os,pathlib,re,shlex,subprocess,sys
p=json.load(sys.stdin)
def command(argv, data=None, check=True):
 r=subprocess.run(argv,input=data,capture_output=True,text=True,timeout=600,env=env,cwd=app)
 if check and r.returncode: raise RuntimeError("guest command failed")
 return r
env=dict(os.environ)
for line in pathlib.Path("/etc/ginnungagap/install.env").read_text().splitlines():
 if line and not line.startswith("#") and "=" in line:
  k,v=line.split("=",1);env[k]=shlex.split(v)[0] if v else ""
app=pathlib.Path(env["PYTHONPATH"])
for line in (app/"rendered/compose.env").read_text().splitlines():
 if line and not line.startswith("#") and "=" in line:
  k,v=line.split("=",1);env[k]=v
compose=["docker","compose","--env-file","rendered/compose.env"]
def cp(args, **kw): return command(compose+args,**kw)
def inside(code,data=None): return cp(["exec","-T","ginnungagap","python","-c",code],data=data)
def health_services(containers):
 checked=[]
 for c in containers:
  identity=c.get("ID")
  if not isinstance(identity,str) or re.fullmatch(r"[a-f0-9]{12,64}",identity) is None: raise RuntimeError("missing container identity")
  inspected=json.loads(command(["docker","inspect",identity]).stdout)
  if not isinstance(inspected,list) or len(inspected)!=1: raise RuntimeError("invalid container inspection")
  health=inspected[0]["Config"].get("Healthcheck")
  if health is None: continue
  if not isinstance(health,dict): raise RuntimeError("invalid container healthcheck")
  test=health.get("Test")
  if test==["NONE"]: continue
  if not isinstance(test,list) or not test or test[0] not in ("CMD","CMD-SHELL"): raise RuntimeError("unknown container healthcheck")
  checked.append(c["Service"])
 return sorted(checked)
try:
 action=p["action"]
 if action=="snapshot":
  expected=cp(["config","--services"]).stdout.split()
  text=cp(["ps","--all","--format","json"]).stdout.strip()
  containers=json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x]
  checked=health_services(containers)
  wanted=sorted(set(x if ":" in x.rsplit("/",1)[-1] else x+":latest" for x in cp(["--profile","*","config","--images"]).stdout.split()))
  present=sorted(set(command(["docker","image","ls","--format","{{.Repository}}:{{.Tag}}"]).stdout.split()))
  versions=json.loads(command(["ginnungagap","update","--versions","--json"]).stdout)
  installed=versions.get("installed") or {}
  banner=pathlib.Path("/etc/issue.d/ginnungagap.issue").read_text()
  access=json.loads(pathlib.Path("/var/lib/ginnungagap/ssh-access.json").read_text())
  history=json.loads(command(["ginnungagap","update","--history","--json"]).stdout)
  check=command(["ginnungagap","update","--check","--json"],check=False)
  token_files=list(pathlib.Path("/etc/ginnungagap").rglob("*token*"))
  value={"services":expected,"containers":containers,"healthchecked_services":checked,"images":present,"expected_images":wanted,
    "installed":installed,"versions_program":versions.get("program"),"history":history.get("history"),
    "status_exit":command(["ginnungagap","status","/etc/ginnungagap/ginnungagap.yml"],check=False).returncode,
    "versions_first":command(["ginnungagap","versions"]).stdout.splitlines()[0],
    "check_exit":check.returncode,"check_network_warning":any(x in (check.stdout+check.stderr).lower() for x in ("network","unreachable","timed out","could not reach","connection")),
    "banner_version_ok":("v"+p["version"]) in banner,"banner_ssh_ok":("SSH: ssh "+p["user"]+"@") in banner,
    "password_mode":access.get("mode"),"password_leaked":bool(access.get("password")),
    "token_files":len(token_files)}
 elif action=="boot_identity":
  value={"boot_id":pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
 elif action=="schedule_reboot":
  unit=p["unit"]
  if re.fullmatch(r"imgaccept-reboot-[a-f0-9]{24}",unit) is None: raise RuntimeError("invalid reboot unit")
  command(["systemd-run","--quiet","--unit="+unit,"--on-active=2s","/usr/bin/systemctl","reboot"])
  value={"scheduled":True}
 elif action=="reboot_observation":
  text=cp(["ps","--all","--format","json"]).stdout.strip()
  containers=json.loads(text) if text.startswith("[") else [json.loads(x) for x in text.splitlines() if x]
  checked=health_services(containers)
  expected=cp(["config","--services"]).stdout.split()
  running=command(["docker","ps","--format","{{.ID}}"]).stdout.split()
  journal=command(["journalctl","-b","--no-pager","--output=cat"]).stdout
  value={"boot_id":pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),"healthchecked_services":checked,
   "container_count":len(running),"compose_ready":len(containers)==9 and len(expected)==9
    and {c.get("Service") for c in containers}==set(expected)
    and all(c.get("State")=="running" and (c.get("Health")=="healthy" if c.get("Service") in checked else c.get("Health") in (None,"","healthy")) for c in containers),
   "ordering_cycles":len(re.findall("ordering"+chr(32)+"cycle",journal,re.IGNORECASE))}
 elif action=="create_state":
  inside("import json,sys,pathlib;from app.users import create_user;p=json.load(sys.stdin);create_user(p['username'],p['password'],'admin');pathlib.Path('/data/'+p['marker']).write_text(p['value'])",json.dumps(p))
  value={"ok":True}
 elif action=="check_state":
  result=inside("import json,sys,pathlib;from app.users import list_users;p=json.load(sys.stdin);u=list_users();names=[x['username'] for x in u];print(json.dumps({'marker':pathlib.Path('/data/'+p['marker']).read_text()==p['value'],'admin':p['username'] in names}))",json.dumps(p))
  value=json.loads(result.stdout)
 elif action=="apply":
  command(["ginnungagap","apply","/etc/ginnungagap/ginnungagap.yml","--no-build"])
  cp(["up","-d","--no-build","--pull","never","--force-recreate","ginnungagap"])
  value={"ok":True}
 elif action=="install_update_trust":
  data=base64.b64decode(p["contents"],validate=True)
  root=pathlib.Path("/etc/ginnungagap/update-trust")
  if root.is_symlink(): raise RuntimeError("linked trust directory")
  root.mkdir(mode=0o755,exist_ok=True)
  temporary=root/".imgaccept-allowed_signers"
  fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,"wb") as stream: stream.write(data)
  temporary.replace(root/"allowed_signers")
  value={"sha256":hashlib.sha256((root/"allowed_signers").read_bytes()).hexdigest()}
 elif action=="ova_create":
  root=pathlib.Path("/srv/templates")/p["folder"];root.mkdir(exist_ok=False)
  with (root/"probe.ova").open("xb") as f:
   f.write(b"0123456789"*524288)
  value={"size":5242880}
 elif action=="ova_remove":
  root=pathlib.Path("/srv/templates")/p["folder"]
  (root/"probe.ova").unlink(missing_ok=True);root.rmdir();value={"ok":True}
 else: raise RuntimeError("unknown probe action")
 print(json.dumps(value))
except Exception:
 print(json.dumps({"probe_error":True}))
 sys.exit(1)
"""


def _remote(ctx, target: RemoteTarget, action: str, *, probe_timeout=900, **payload):
    result = ctx.remote.run(
        target,
        ["sudo", "python3", "-c", _GUEST_PROBE],
        stdin=json.dumps({"action": action, **payload}).encode(),
        timeout=probe_timeout,
    )
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise PermanentError(
            f"acceptance guest probe {action} returned no valid result"
        ) from exc
    if not result.ok or not isinstance(value, dict) or value.get("probe_error"):
        raise PermanentError(f"acceptance guest probe {action} failed")
    return value


def _object_member(response: HttpResult, name: str) -> dict:
    """Absent web status is JSON null while the host has not written it yet."""
    body = response.json()
    value = body.get(name) if isinstance(body, dict) else None
    return value if isinstance(value, dict) else {}


def _web_check_done(response: HttpResult, request_id: str) -> bool:
    if response.status != 200:
        return False
    result = _object_member(response, "result")
    return result.get("id") == request_id and result.get("state") == "done"


def wait_http(
    ctx,
    client: HttpClient,
    path: str,
    predicate,
    *,
    timeout=CHECK_TIMEOUT,
    diagnostic=None,
    **options,
):
    end = ctx.clock.monotonic() + timeout
    last = HttpResult(0, b"", {})
    while True:
        last = client.request(path, **options)
        try:
            if predicate(last):
                return last
        except (ValueError, TypeError, KeyError):
            pass
        if ctx.clock.monotonic() >= end:
            detail = ": " + diagnostic(last) if diagnostic is not None else ""
            if last.status == 0:
                raise Unreachable(
                    f"acceptance HTTP endpoint did not answer: {path}{detail}"
                )
            raise PermanentError(f"acceptance HTTP check did not pass: {path}{detail}")
        ctx.clock.sleep(POLL)


def _operator_version(response: HttpResult) -> str | None:
    """Only a valid version field may enter readiness checks or diagnostics."""
    try:
        body = response.json()
        value = body.get("version") if isinstance(body, dict) else None
        return names.check_version(value)
    except (ValueError, TypeError, names.InvalidVersion):
        return None


def validate_snapshot(data: dict, manifest: dict, version: str) -> list[dict[str, Any]]:
    """Every required observation is explicit; missing fields never pass."""
    expected = data.get("services")
    containers = data.get("containers")
    checked = data.get("healthchecked_services")
    services_ok = (
        isinstance(expected, list)
        and bool(expected)
        and isinstance(containers, list)
        and isinstance(checked, list)
        and all(isinstance(name, str) for name in checked)
        and len(checked) == len(set(checked))
        and set(checked).issubset(expected)
    )
    if services_ok:
        services_ok = {c.get("Service") for c in containers} == set(expected)
        services_ok = services_ok and len(containers) == len(expected)
        services_ok = services_ok and all(
            c.get("State") == "running"
            and (
                c.get("Health") == "healthy"
                if c.get("Service") in checked
                else c.get("Health") in (None, "", "healthy")
            )
            for c in containers
        )
    installed = data.get("installed") or {}
    components = installed.get("components") or {}
    wanted_components = manifest.get("components") or {}
    versions_ok = (installed.get("appliance") or {}).get("version") == version
    versions_ok = (
        versions_ok
        and bool(wanted_components)
        and set(components) == set(wanted_components)
    )
    versions_ok = versions_ok and all(
        (components.get(name) or {}).get("version") == value.get("version")
        for name, value in wanted_components.items()
    )
    images = data.get("images")
    wanted_images = data.get("expected_images")
    image_ok = (
        isinstance(images, list)
        and isinstance(wanted_images, list)
        and bool(wanted_images)
    )
    image_ok = image_ok and set(images) == set(wanted_images)
    image_ok = image_ok and not any(
        "<none>" in image or "rollback-" in image or "ova-deploy" in image
        for image in images or []
    )
    return [
        row("all compose services running and healthy", services_ok),
        row("ginnungagap status is healthy", data.get("status_exit") == 0),
        row(
            "versions lists appliance first",
            str(data.get("versions_first", "")).startswith("Appliance release:"),
        ),
        row("exact installed release and component versions", versions_ok),
        row("appliance program version", data.get("versions_program") == version),
        row("exact compose image set", image_ok),
        row(
            "console banner version and SSH hint",
            data.get("banner_version_ok") is True and data.get("banner_ssh_ok") is True,
        ),
        row(
            "injected key mode, no initial password carried",
            data.get("password_mode") == "deployer"
            and data.get("password_leaked") is False,
        ),
        row(
            "update history contains only the current rollback seed",
            _current_history(data.get("history"), installed, version),
        ),
        row("no update token file", data.get("token_files") == 0),
        row(
            "updater check",
            data.get("check_exit") in (0, 10)
            or data.get("check_network_warning") is True,
            "a network failure is a recorded warning, not proof of a successful release check",
        ),
    ]


def _current_history(history, installed, version):
    if history == []:
        return True
    if not isinstance(history, list) or len(history) != 1:
        return False
    item = history[0]
    return (
        isinstance(item, dict)
        and isinstance(installed.get("entry"), str)
        and item.get("file") == installed["entry"]
        and item.get("current") is True
        and item.get("version") == version
    )


def _required(checks, name):
    failed = [
        c for c in checks if c.get("required", True) and c.get("passed") is not True
    ]
    if not checks or not any(c.get("required", True) for c in checks) or failed:
        raise PermanentError(f"{name} failed: " + ", ".join(c["name"] for c in failed))


def _user(ctx) -> str:
    configured = getattr(ctx.config.image, "ssh_user", None)
    prior = ctx.results.get("P5", {}).get("target", {}).get("user")
    catalog = ctx.results.get("P3", {}).get("username")
    user = configured or prior or catalog
    if (
        not isinstance(user, str)
        or not user
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in user
        )
    ):
        raise Blocked(
            "acceptance needs an explicit SSH user or the base template's login user"
        )
    return user


def _guestinfo(config: bytes, public: str, hostname: str):
    return {
        "guestinfo.ginnungagap.config": base64.b64encode(config).decode(),
        "guestinfo.ginnungagap.config.encoding": "base64",
        "guestinfo.ginnungagap.ssh_authorized_key": public,
        "guestinfo.ginnungagap.hostname": hostname,
        "guestinfo.ginnungagap.admin_sudo_nopasswd": "true",
    }


def _upgrade_channel(ctx):
    return "prerelease" if ctx.options.allow_prerelease else "stable"


def _acceptance_config(ctx, boot: str):
    config = yaml.safe_load(_MINIMAL.read_bytes())
    if boot == "c":
        # This override belongs only to the disposable upgrade VM. The daemon's
        # automatic jobs never set allow_unsigned, and no published image changes.
        config["update"] = {
            "channel": _upgrade_channel(ctx),
            "require_signature": not ctx.options.allow_unsigned,
        }
    return yaml.safe_dump(config).encode()


def _upgrade_trust(ctx):
    if ctx.options.allow_unsigned:
        return None
    try:
        contents = ctx.config.paths.allowed_signers.read_bytes()
    except OSError as exc:
        raise Blocked(
            "signed upgrade acceptance needs daemon-pinned allowed_signers"
        ) from exc
    if not any(
        line.strip() and not line.lstrip().startswith(b"#")
        for line in contents.splitlines()
    ):
        raise Blocked(
            "signed upgrade acceptance needs nonempty daemon-pinned allowed_signers"
        )
    return contents


def _reserved_address(ctx, name: str, expected: str, *, timeout=BOOT_TIMEOUT):
    """Wait for Tools to converge on the reserved hardware-NIC address."""
    deadline = ctx.clock.monotonic() + timeout
    observed = None
    while True:
        remaining = deadline - ctx.clock.monotonic()
        if remaining <= 0:
            break
        try:
            address = ctx.esxi.wait_for_ip(name, min(30, remaining))
        except Timeout:
            pass
        else:
            if address == expected:
                return address
            try:
                observed = (
                    str(ipaddress.IPv4Address(address))
                    if isinstance(address, str)
                    else "invalid address"
                )
            except ipaddress.AddressValueError:
                observed = "invalid address"
        remaining = deadline - ctx.clock.monotonic()
        if remaining > 0:
            ctx.clock.sleep(min(POLL, remaining))
    message = (
        "acceptance VM did not report its reserved static management address: "
        f"expected {expected}; last observed {observed or 'no address'}"
    )
    if observed is None:
        raise Timeout(message)
    raise PermanentError(message)


def _new_vm(ctx, boot: str, url: str, *, configured: bool):
    name = names.accept_vm_name(ctx.run_id, boot)
    ctx.track_vm(name)
    ctx.track_datastore_folder(ctx.config.esxi.datastore, name)
    ctx.esxi.import_ova(url, name, ctx.config.esxi.datastore, [ctx.config.esxi.network])
    ctx.esxi.set_annotation(name, names.annotation(ctx.run_id))
    target = None
    key = None
    guestinfo = {}
    network = getattr(ctx.config.image, "management_network", None)
    if configured:
        directory = ctx.subdir("accept-" + boot)
        ctx.track_path(directory)
        key = ctx.remote.generate_key(directory / "key", "imgaccept-" + boot)
        ctx.secret(key.public)
        config = _acceptance_config(ctx, boot)
        guestinfo = _guestinfo(config, key.public, "imgaccept-" + boot)
    elif ctx.esxi.get_extra_config(name):
        # The OVA must carry no deployment guestinfo; do not silently clear leaked data.
        leaked = [
            k
            for k, v in ctx.esxi.get_extra_config(name).items()
            if k.startswith("guestinfo.ginnungagap.") and v
        ]
        if leaked:
            raise PermanentError("unconfigured acceptance OVA carries guestinfo data")
    if network is not None:
        guestinfo["guestinfo.ginnungagap.network"] = json.dumps(
            {
                "address": network.acceptance_address,
                "prefix": network.prefix,
                "gateway": network.gateway,
                "dns": list(network.dns),
            }
        )
    if guestinfo:
        ctx.esxi.set_extra_config(name, guestinfo)
    ctx.esxi.power_on(name)
    if network is not None and boot == "c":
        # The previous published image does not yet understand network guestinfo.
        # Bootstrap only that configured upgrade VM over its hardware NIC's
        # link-local address; A/B must prove the new native first-boot path.
        target = remote.configure_static_guest(
            ctx,
            name,
            _user(ctx),
            key.private,
            key.private.parent / "known_hosts",
            network.acceptance_address,
        )
    ip = (
        _reserved_address(ctx, name, network.acceptance_address)
        if network is not None
        else ctx.esxi.wait_for_ip(name, BOOT_TIMEOUT)
    )
    if key and target is None:
        target = RemoteTarget(
            ip, _user(ctx), key.private, key.private.parent / "known_hosts"
        )
        ctx.remote.wait_ssh(target, BOOT_TIMEOUT)
    return name, ip, target


def _remove_vm(ctx, name):
    ctx.esxi.delete_vm(name)
    ctx.esxi.delete_datastore_folder(ctx.config.esxi.datastore, name)


def boot_a(ctx, url: str, client_factory=HttpClient) -> list[dict[str, Any]]:
    name, ip, _ = _new_vm(ctx, "a", url, configured=False)
    try:
        client = client_factory(ip)
        wait_http(
            ctx,
            client,
            "/",
            lambda r: r.status == 200 and b"Ginnungagap - Setup" in r.body,
        )
        wait_http(
            ctx,
            client,
            "/appliance/health",
            lambda r: r.status == 503 and r.json().get("status") == "unconfigured",
        )
        screenshot = ctx.esxi.screenshot(name)
        valid_png = screenshot.startswith(b"\x89PNG\r\n\x1a\n")
        if valid_png:
            (ctx.run_dir / "accept-a-console.png").write_bytes(screenshot)
        checks = [
            row("unconfigured wizard reachable", True),
            row("unconfigured health state", True),
            row("unconfigured console screenshot", valid_png),
        ]
        password = secrets.token_urlsafe(32)
        ctx.secret(password)
        fields = {
            "public_url": "http://" + ip,
            "timezone": "UTC",
            "tls_mode": "off",
            "auth_mode": "basic",
            "auth_user": "imgaccept",
            "auth_password": password,
            "store_type": "local",
            "store_local_path": "/srv/templates",
            "ova_mode": "bundled",
            "esxi_mode": "bundled",
            "gns3_mode": "bundled",
            "generator_mode": "bundled",
            "llm_1_name": "acceptance-only",
            "llm_1_url": "http://192.0.2.1:11434",
            "llm_1_model": "acceptance-only",
            "action": "apply",
        }
        # The initial setup is intentionally unauthenticated. The 401 guard is
        # checked after applying a Basic Auth configuration, not before it.
        response = client.request("/", method="POST", form=fields)
        checks.append(
            row("initial setup accepts valid configuration", response.status == 200)
        )
        client.basic = ("imgaccept", password)
        wait_http(
            ctx,
            client,
            "/appliance/health",
            lambda r: r.status == 200 and r.json().get("status") == "ok",
            timeout=BOOT_TIMEOUT,
        )
        wait_http(ctx, client, "/setup/", lambda r: r.status == 200)
        client.basic = None
        response = client.request("/setup/", method="POST", form=fields)
        checks.append(
            row(
                "configured wizard rejects unauthenticated submit",
                response.status == 401,
            )
        )
        client.basic = ("imgaccept", password)
        response = client.request(
            "/setup/", method="POST", form={**fields, "action": "download"}
        )
        checks.append(
            row(
                "configured wizard accepts authenticated submit", response.status == 200
            )
        )
        _required(checks, "unconfigured boot")
        return checks
    finally:
        _remove_vm(ctx, name)


def _session(ctx, target, client):
    username = "imgaccept-" + secrets.token_hex(5)
    password = secrets.token_urlsafe(32)
    marker = "imgaccept-" + secrets.token_hex(8)
    value = secrets.token_urlsafe(32)
    for secret in (password, value):
        ctx.secret(secret)
    payload = {
        "username": username,
        "password": password,
        "marker": marker,
        "value": value,
    }
    _remote(ctx, target, "create_state", **payload)
    result = client.request(
        "/login", method="POST", form={"username": username, "password": password}
    )
    if result.status not in (200, 302, 303):
        raise PermanentError("acceptance administrator login failed")
    wait_http(
        ctx,
        client,
        "/me",
        lambda r: r.status == 200 and r.json().get("username") == username,
    )
    return payload


def _check_state(ctx, target, client, payload) -> bool:
    saved = _remote(ctx, target, "check_state", **payload)
    response = wait_http(
        ctx,
        client,
        "/me",
        lambda r: r.status == 200 and r.json().get("username") == payload["username"],
    )
    return (
        saved.get("marker") is True
        and saved.get("admin") is True
        and response.status == 200
    )


def _boot_id(observation):
    try:
        value = observation["boot_id"]
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
        return value
    except (KeyError, ValueError, TypeError, AttributeError):
        raise PermanentError(
            "configured reboot returned no valid boot identity"
        ) from None


def configured_reboots(ctx, target, *, count=4, timeout=BOOT_TIMEOUT, checks=None):
    """Prove four different kernel boots, including the complete startup journal."""
    seen = {_boot_id(_remote(ctx, target, "boot_identity", probe_timeout=30))}
    checks = [] if checks is None else checks
    for number in range(1, count + 1):
        scheduled = _remote(
            ctx,
            target,
            "schedule_reboot",
            unit="imgaccept-reboot-" + secrets.token_hex(12),
            probe_timeout=30,
        )
        if scheduled.get("scheduled") is not True:
            raise PermanentError("configured acceptance reboot was not scheduled")
        # Reconnect after the scheduled reboot instead of reusing a connection
        # that may still belong to the previous kernel boot. Keep the key.
        try:
            ctx.remote.close_master(target)
        except (Unreachable, Timeout):
            pass
        end = ctx.clock.monotonic() + timeout
        observation = None
        while True:
            remaining = end - ctx.clock.monotonic()
            if remaining <= 0:
                if observation is None:
                    raise Unreachable(
                        f"configured reboot {number} did not regain SSH before its deadline"
                    )
                raise PermanentError(
                    f"configured reboot {number} did not prove a new healthy boot with nine containers"
                )
            try:
                ctx.remote.wait_ssh(target, min(30, remaining))
                remaining = end - ctx.clock.monotonic()
                if remaining <= 0:
                    continue
                observation = _remote(
                    ctx,
                    target,
                    "reboot_observation",
                    probe_timeout=min(30, remaining),
                )
            except (Unreachable, Timeout, CommandFailed, PermanentError):
                ctx.clock.sleep(min(POLL, max(0, end - ctx.clock.monotonic())))
                continue
            boot_id = _boot_id(observation)
            if boot_id not in seen:
                cycles = observation.get("ordering_cycles")
                if type(cycles) is int and cycles > 0:
                    raise PermanentError(
                        f"configured reboot {number} has ordering cycle messages in its startup journal"
                    )
                if (
                    type(observation.get("container_count")) is int
                    and observation["container_count"] == 9
                    and observation.get("compose_ready") is True
                    and type(cycles) is int
                    and cycles == 0
                ):
                    seen.add(boot_id)
                    checks.append(
                        row(
                            f"configured reboot {number}: new boot, nine healthy containers, no ordering cycle",
                            True,
                            boot_id,
                        )
                    )
                    break
            ctx.clock.sleep(min(POLL, max(0, end - ctx.clock.monotonic())))
    return checks


def boot_b(
    ctx, url: str, manifest: dict, client_factory=HttpClient
) -> list[dict[str, Any]]:
    name, ip, target = _new_vm(ctx, "b", url, configured=True)
    checks = []
    try:
        client = client_factory(ip)
        wait_http(
            ctx,
            client,
            "/appliance/health",
            lambda r: r.status == 200 and r.json().get("status") == "ok",
            timeout=BOOT_TIMEOUT,
        )
        status = _remote(ctx, target, "snapshot", version=ctx.version, user=target.user)
        checks.extend(validate_snapshot(status, manifest, ctx.version))
        if status.get("check_network_warning"):
            ctx.summary.warnings.append(
                "acceptance updater check could not reach the release source"
            )
        operator_version = names.check_version(
            (manifest.get("components", {}).get("operator") or {}).get("version")
        )

        def operator_diagnostic(response):
            observed = _operator_version(response)
            return (
                f"expected Operator version={operator_version!r}; "
                f"observed HTTP {response.status}, version={observed!r}"
            )

        try:
            result = wait_http(
                ctx,
                client,
                "/health",
                lambda response: (
                    response.status == 200
                    and _operator_version(response) == operator_version
                ),
                diagnostic=operator_diagnostic,
            )
        except (Unreachable, PermanentError) as exc:
            checks.append(row("Operator version", False, str(exc)))
            raise
        checks.append(row("Operator version", True, operator_diagnostic(result)))
        folder = "imgaccept-range-" + ctx.run_id
        _remote(ctx, target, "ova_create", folder=folder)
        try:
            absent = client.request("/" + folder + "/absent.ova", port=8088)
            head = client.request("/" + folder + "/probe.ova", method="HEAD", port=8088)
            ranged = client.request(
                "/" + folder + "/probe.ova", headers={"Range": "bytes=0-9"}, port=8088
            )
            checks.extend(
                [
                    row("OVA store missing file returns 404", absent.status == 404),
                    row(
                        "OVA store HEAD supports pull import",
                        head.status == 200
                        and head.headers.get("content-length") == "5242880"
                        and head.headers.get("accept-ranges", "").lower() == "bytes",
                    ),
                    row(
                        "OVA store Range returns exact bytes",
                        ranged.status == 206 and ranged.body == b"0123456789",
                    ),
                ]
            )
        finally:
            _remote(ctx, target, "ova_remove", folder=folder)
        session = _session(ctx, target, client)
        panel = wait_http(
            ctx,
            client,
            "/admin/updates",
            lambda response: (
                response.status == 200
                and _object_member(response, "release").get("version") == ctx.version
            ),
        )
        checks.append(
            row(
                "Admin Updates shows installed release",
                panel.status == 200
                and _object_member(panel, "release").get("version") == ctx.version,
            )
        )
        requested = client.request("/admin/updates/check", method="POST", form={})
        checks.append(
            row("Admin Updates accepts check request", requested.status == 202)
        )
        if requested.status == 202:
            request_id = requested.json().get("id")
            if not request_id:
                raise PermanentError("update check returned no request id")
            wait_http(
                ctx,
                client,
                "/admin/updates",
                lambda response: _web_check_done(response, request_id),
            )
            checks.append(row("host updater completes the web request", True))
        _remote(ctx, target, "apply")
        checks.append(
            row(
                "marker administrator and cookie survive apply",
                _check_state(ctx, target, client, session),
            )
        )
        configured_reboots(ctx, target, checks=checks)
        _required(checks, "configured boot")
        return checks
    except Exception as exc:
        exc.acceptance_checks = checks + getattr(exc, "acceptance_checks", [])
        raise
    finally:
        _remove_vm(ctx, name)


def boot_c(ctx, manifest: dict, client_factory=HttpClient) -> list[dict[str, Any]]:
    base = ctx.result_of("P3")
    version = base.get("version")
    if not version:
        raise PermanentError(
            "upgrade acceptance needs the base image's release version"
        )
    trust = _upgrade_trust(ctx)
    name, ip, target = _new_vm(ctx, "c", base["url"], configured=True)
    checks = []
    try:
        client = client_factory(ip)
        wait_http(
            ctx,
            client,
            "/appliance/health",
            lambda r: r.status == 200 and r.json().get("status") == "ok",
            timeout=BOOT_TIMEOUT,
        )
        checks.extend(
            [
                row("upgrade channel is " + _upgrade_channel(ctx), True),
                row(
                    "upgrade signature policy: "
                    + (
                        "explicit manual unsigned override"
                        if ctx.options.allow_unsigned
                        else "signature required"
                    ),
                    True,
                ),
            ]
        )
        if trust is not None:
            installed_trust = _remote(
                ctx,
                target,
                "install_update_trust",
                contents=base64.b64encode(trust).decode(),
            )
            checks.append(
                row(
                    "upgrade uses daemon-pinned signing trust",
                    installed_trust.get("sha256") == hashlib.sha256(trust).hexdigest(),
                )
            )
            _required(checks, "upgrade trust")
        session = _session(ctx, target, client)
        baseline = _remote(ctx, target, "snapshot", version=version, user=target.user)
        original = baseline.get("installed", {})
        components = original.get("components", {})
        checks.append(
            row(
                "upgrade captures the complete base identity",
                bool(original.get("appliance", {}).get("version"))
                and set(components) == set(manifest.get("components", {}))
                and bool(components)
                and all(
                    isinstance(c, dict) and bool(c.get("version"))
                    for c in components.values()
                )
                and bool(baseline.get("versions_program")),
            )
        )
        _required(checks, "upgrade baseline")
        directory = Path(ctx.result_of("P2")["release_dir"])
        destination = f"/home/{target.user}/imgaccept-release"
        ctx.remote.put(target, directory, destination, recursive=True)
        ctx.remote.run(
            target,
            [
                "sudo",
                "env",
                "GINNUNGAGAP_INSTALL_ARGS=--image",
                "ginnungagap",
                "update",
                "--apply",
                "--offline-dir",
                destination,
                "--version",
                ctx.version,
                "--channel",
                _upgrade_channel(ctx),
                "--yes",
            ],
            timeout=1800,
        )
        wait_http(
            ctx,
            client,
            "/appliance/health",
            lambda r: r.status == 200 and r.json().get("status") == "ok",
            timeout=BOOT_TIMEOUT,
        )
        updated = _remote(
            ctx, target, "snapshot", version=ctx.version, user=target.user
        )
        checks.extend(
            [
                row(
                    "upgrade records the requested release",
                    updated.get("installed", {}).get("appliance", {}).get("version")
                    == ctx.version,
                ),
                row(
                    "upgrade preserves marker administrator and cookie",
                    _check_state(ctx, target, client, session),
                ),
            ]
        )
        installed = updated.get("installed", {}).get("components", {})
        checks.append(
            row(
                "upgrade records every component version",
                bool(installed)
                and set(installed) == set(manifest.get("components", {}))
                and all(
                    installed[key].get("version") == value.get("version")
                    for key, value in manifest.get("components", {}).items()
                ),
            )
        )
        ctx.remote.run(
            target,
            ["sudo", "ginnungagap", "update", "--rollback", "--yes"],
            timeout=1800,
        )
        wait_http(
            ctx,
            client,
            "/appliance/health",
            lambda r: r.status == 200 and r.json().get("status") == "ok",
            timeout=BOOT_TIMEOUT,
        )
        restored = _remote(ctx, target, "snapshot", version=version, user=target.user)
        checks.append(
            row(
                "rollback restores the exact captured base identity",
                restored.get("installed", {}).get("appliance")
                == original.get("appliance")
                and restored.get("installed", {}).get("components") == components
                and restored.get("versions_program")
                == baseline.get("versions_program"),
            )
        )
        checks.append(
            row(
                "rollback preserves marker administrator and cookie",
                _check_state(ctx, target, client, session),
            )
        )
        _required(checks, "upgrade boot")
        return checks
    except Exception as exc:
        exc.acceptance_checks = checks + getattr(exc, "acceptance_checks", [])
        raise
    finally:
        _remove_vm(ctx, name)


def acceptance_phase(ctx):
    stage = ctx.result_of("P12")
    manifest = ctx.result_of("P2").get("manifest")
    if not isinstance(manifest, dict):
        raise PermanentError("acceptance needs the verified release manifest")
    if ctx.config.acceptance.extra_hosts:
        raise Blocked(
            "extra-host acceptance needs separately configured ESXi adapters; no image is published"
        )
    checks = []
    report = {"passed": False, "checks": checks}
    try:
        boots = [
            ("a", lambda: boot_a(ctx, stage["url"])),
            ("b", lambda: boot_b(ctx, stage["url"], manifest)),
        ]
        if ctx.config.acceptance.upgrade_test:
            boots.append(("c", lambda: boot_c(ctx, manifest)))
        else:
            ctx.summary.warnings.append("upgrade acceptance disabled explicitly")
        for boot, function in boots:
            try:
                observed = function()
            except Exception as exc:
                checks.extend(
                    {**c, "name": boot + ": " + c["name"]}
                    for c in getattr(exc, "acceptance_checks", [])
                )
                raise
            checks.extend({**c, "name": boot + ": " + c["name"]} for c in observed)
        _required(checks, "acceptance")
        report["passed"] = True
        ctx.note("acceptance", report)
        return report
    except Exception as exc:
        checks.append(row("acceptance completed", False, type(exc).__name__))
        raise
    finally:
        (ctx.run_dir / "acceptance.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


def register_phases(registry=None):
    from . import pipeline

    chosen = registry if registry is not None else pipeline.DEFAULT_REGISTRY
    chosen.register(
        "P13",
        acceptance_phase,
        describe=lambda ctx: (
            "boot new unconfigured, configured and upgrade acceptance VMs"
        ),
    )
    return chosen
