"""Install the already verified release, prepare it and validate exported OVAs."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from ginnungagap.image_network import restore_script

from . import pipeline, remote
from .ports import Blocked, CommandFailed, PermanentError, TransientError

_PREBUILT_SERVICES = frozenset(
    (
        "ginnungagap",
        "template-esxi",
        "collector-esxi",
        "template-gns3",
        "collector-gns3",
        "generator",
        "setup",
    )
)

# Executed as root in a throw-away guest, never on the build host. The release
# was verified with the pinned tooling before it crossed the SSH connection.
_CHECKOUTS = """import json,pathlib,shutil,sys,tarfile,tempfile
release=pathlib.Path(sys.argv[1]);root=pathlib.Path('/srv/repos');root.mkdir(parents=True,exist_ok=True)
manifest=json.loads((release/'components.json').read_text())
for role,c in manifest['components'].items():
 repo=c['repo'].split('/')[-1];asset=c['asset'];top=asset.removesuffix('.tar.gz')
 if '/' in asset or '\\\\' in asset or not asset.endswith('.tar.gz') or repo in ('.','..'):
  raise RuntimeError('invalid verified archive name')
 scratch=pathlib.Path(tempfile.mkdtemp(prefix='.imgbuild-',dir=root))
 try:
  with tarfile.open(release/asset,'r:gz') as archive:
   members=archive.getmembers()
   for m in members:
    p=pathlib.PurePosixPath(m.name)
    if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!=top:
     raise RuntimeError('archive escaped its verified component folder')
    if m.isdev() or m.isfifo() or m.islnk(): raise RuntimeError('unsupported archive member')
   archive.extractall(scratch,filter='data')
  dest=root/repo
  if dest.is_symlink(): dest.unlink()
  elif dest.exists(): shutil.rmtree(dest)
  (scratch/top).rename(dest)
 finally: shutil.rmtree(scratch,ignore_errors=True)
"""

_COMPOSE_IMAGES = """import json,os,pathlib,shutil,subprocess
from ginnungagap import config,operate,render
bundle=operate.bundle_dir();folder=bundle/'rendered'
try:
 render.render(config.load('/etc/ginnungagap/ginnungagap.yml'),folder,os.environ.get('GINNUNGAGAP_GENERATOR_CONFIG'))
 result=subprocess.run(operate.compose_args(bundle,'--profile','*','config','--images'),cwd=bundle,capture_output=True,text=True,check=True)
 print(json.dumps(sorted(set(operate._image_reference(line.strip()) for line in result.stdout.splitlines() if line.strip()))))
finally: shutil.rmtree(folder,ignore_errors=True)
"""

_RECORD_CHECKOUTS = """import datetime,os,pathlib,shlex,shutil,sys
from ginnungagap import manifest as mf
release=pathlib.Path(sys.argv[1]);m=mf.load(release/'components.json')
root=pathlib.Path(os.environ.get('GINNUNGAGAP_UPDATE_DIR','/var/lib/ginnungagap/update')).parent
links=root/'components';links.mkdir(parents=True,exist_ok=True);dirs={}
for name,c in m.components.items():
 source=pathlib.Path('/srv/repos')/c.repo_name
 folder=c.version+'.imgbuild';dest=root/'releases'/c.checkout/folder
 if dest.exists(): raise RuntimeError('checkout release destination exists')
 dest.parent.mkdir(parents=True,exist_ok=True);source.rename(dest)
 link=links/c.checkout
 if link.is_symlink(): link.unlink()
 elif link.exists(): raise RuntimeError('component link is not a symlink')
 link.symlink_to('../releases/'+c.checkout+'/'+folder);dirs[name]=folder
state=mf.state_from_manifest(m,dirs,datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),None)
mf.save_installed(root/'update'/'installed.json',state)
env=pathlib.Path('/etc/ginnungagap/install.env')
values={'REPOS_DIR':str(links),'GINNUNGAGAP_GENERATOR_CONFIG':str(links/mf.CHECKOUT_NAMES['generator']/'config')}
lines=[line for line in env.read_text().splitlines() if not any(line.startswith(k+'=') for k in values)]
lines.extend(k+'='+shlex.quote(v) for k,v in values.items());env.write_text('\\n'.join(lines)+'\\n')
"""

PREPARED_MARKER = "IMGBUILD_PREPARE_VERIFIED"
PREBUILT_AUDIT_MARKER = "IMGBUILD_PREBUILT_AUDIT="
_LEGACY_SOURCE_CLEANUP = """def cleanup_legacy_sources(root=pathlib.Path('/'),owner_uid=0):
 legacy=root/'srv/repos'
 if not legacy.exists() and not legacy.is_symlink(): return
 def plain_parents(path):
  current=root
  for part in path.relative_to(root).parts:
   current=current/part
   if current.is_symlink(): raise RuntimeError('linked legacy cleanup boundary')
   if current.lstat().st_uid!=owner_uid: raise RuntimeError('foreign-owned legacy cleanup boundary')
 def plain_file(path):
  plain_parents(path)
  if not path.is_file(): raise RuntimeError('missing legacy cleanup identity file')
 plain_parents(legacy)
 if not legacy.is_dir() or legacy.is_mount(): raise RuntimeError('unsafe legacy source directory')
 links=root/'var/lib/ginnungagap/components'
 releases=root/'var/lib/ginnungagap/releases'
 plain_parents(links);plain_parents(releases)
 if not links.is_dir() or not releases.is_dir(): raise RuntimeError('missing managed components')
 env=root/'etc/ginnungagap/install.env';plain_file(env)
 values={}
 for line in env.read_text().splitlines():
  if line.startswith('REPOS_DIR='):
   fields=shlex.split(line.split('=',1)[1])
   if len(fields)!=1 or 'REPOS_DIR' in values: raise RuntimeError('ambiguous active source directory')
   values['REPOS_DIR']=fields[0]
 if values.get('REPOS_DIR')!=str(links): raise RuntimeError('legacy sources may still be active')
 state=root/'var/lib/ginnungagap/update/installed.json';plain_file(state)
 roles={'appliance':'Ginnungagap-Appliance','operator':'Igdrasyl','builder':'TopologyBuilder','template-services':'Hugyn-and-Munyn','generator':'Mimyr'}
 if set(json.loads(state.read_text()).get('components',{}))!=set(roles): raise RuntimeError('incomplete installed component identity')
 for repo in roles.values():
  link=links/repo;repo_root=releases/repo
  plain_parents(repo_root)
  if not link.is_symlink() or not repo_root.is_dir(): raise RuntimeError('unmanaged active component')
  active=link.resolve(strict=True)
  if not active.is_dir() or not active.is_relative_to(repo_root) or active.is_relative_to(legacy):
   raise RuntimeError('legacy sources are an active component')
 allowed=set(roles.values())|{'components.json'}
 for child in legacy.iterdir():
  if child.name not in allowed or child.is_symlink() or child.is_mount(): raise RuntimeError('unexpected legacy source entry')
  if child.lstat().st_uid!=owner_uid: raise RuntimeError('foreign-owned legacy source entry')
  if child.name=='components.json':
   if not child.is_file(): raise RuntimeError('invalid legacy manifest')
  elif not child.is_dir(): raise RuntimeError('invalid legacy component directory')
 shutil.rmtree(legacy)
"""
_PREPARE = (
    "import json,pathlib,shlex,shutil,subprocess,sys\n"
    + "subprocess.run([sys.executable,'-c',"
    + repr(restore_script())
    + "],check=True)\n"
    + _LEGACY_SOURCE_CLEANUP
    + """
cleanup_legacy_sources()
release=pathlib.Path(sys.argv[3])
if release.name!='rel' or release.parent.parent!=pathlib.Path('/home'): raise RuntimeError('invalid build upload directory')
if release.is_symlink(): raise RuntimeError('build upload directory is a symlink')
shutil.rmtree(release)
strict='--prebuilt-only' in sys.argv[4:]
prepared=subprocess.run([sys.argv[1],'--yes','--zero-free-space',*sys.argv[4:]],check=True,**({'stdout':subprocess.PIPE,'text':True} if strict else {}))
audits=[]
if strict:
 for line in prepared.stdout.splitlines():
  if line.startswith('IMGBUILD_PREBUILT_AUDIT='): audits.append(line)
  else: print(line,flush=True)
 if len(audits)!=1: raise RuntimeError('prebuilt preparation did not emit one audit')
expected=set(json.loads(sys.argv[2]))
found=set(subprocess.check_output(['docker','image','ls','--format','{{.Repository}}:{{.Tag}}'],text=True).splitlines())
if not expected or found!=expected: raise RuntimeError('prepared Docker image set does not match compose')
if strict: print(audits[0],flush=True)
print('IMGBUILD_PREPARE_VERIFIED',flush=True)
"""
)


def _manifest(ctx):
    manifest = ctx.result_of("P2").get("manifest")
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("components"), dict
    ):
        raise PermanentError("the verified phase did not supply its component manifest")
    return manifest


def _image_policy(manifest: dict) -> tuple[str, dict[str, str]]:
    images = manifest.get("images", {})
    if not isinstance(images, dict):
        raise Blocked("verified release has malformed prebuilt image claims")
    if not images:
        return "source", {}
    if set(images) != _PREBUILT_SERVICES:
        raise Blocked("prebuilt release requires all seven signed service image refs")
    refs = {}
    for service, entry in sorted(images.items()):
        ref = entry.get("ref") if isinstance(entry, dict) else None
        if (
            not isinstance(ref, str)
            or re.fullmatch(
                rf"ghcr\.io/htl3r-xeonis/ginnungagap-{re.escape(service)}@sha256:[0-9a-f]{{64}}",
                ref,
            )
            is None
        ):
            raise Blocked("prebuilt release requires seven pinned GHCR service digests")
        refs[service] = ref
    return "prebuilt-only", refs


def _advertises_prebuilt_only(help_text: str) -> bool:
    return re.search(r"(?<![\w-])--prebuilt-only(?![\w-])", help_text) is not None


def _manifest_sha(ctx, manifest: dict) -> str:
    phase = ctx.result_of("P2")
    directory = phase.get("release_dir") or phase.get("directory")
    try:
        contents = (Path(directory) / "components.json").read_bytes()
        if json.loads(contents) != manifest:
            raise ValueError()
        digest = hashlib.sha256(contents).hexdigest()
        if phase.get("manifest_sha256", digest) != digest:
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise Blocked(
            "prebuilt image claims differ from the verified release manifest"
        ) from None
    return digest


def _prebuilt_audit(
    output: str, version: str, digest: str, refs: dict[str, str]
) -> dict:
    lines = [
        line[len(PREBUILT_AUDIT_MARKER) :]
        for line in output.splitlines()
        if line.startswith(PREBUILT_AUDIT_MARKER)
    ]
    try:
        if len(lines) != 1:
            raise ValueError()
        audit = json.loads(lines[0])
        if (
            not isinstance(audit, dict)
            or set(audit)
            != {
                "schema",
                "policy",
                "appliance_version",
                "manifest_sha256",
                "services",
                "registry_aliases_removed",
                "upstream_pulled",
            }
            or type(audit.get("schema")) is not int
            or audit["schema"] != 1
            or audit.get("policy") != "prebuilt-only"
            or audit.get("appliance_version") != version
            or audit.get("manifest_sha256") != digest
            or audit.get("registry_aliases_removed") is not True
            or audit.get("upstream_pulled") is not True
            or not isinstance(audit.get("services"), dict)
            or set(audit["services"]) != set(refs)
        ):
            raise ValueError()
        for service, ref in refs.items():
            entry = audit["services"][service]
            if (
                not isinstance(entry, dict)
                or set(entry) != {"ref", "image_id", "compose_tag"}
                or entry.get("ref") != ref
                or entry.get("compose_tag") != f"ginnungagap-{service}:latest"
                or not isinstance(entry.get("image_id"), str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", entry["image_id"]) is None
            ):
                raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise PermanentError(
            "prebuilt image audit does not prove the verified manifest and all seven service images"
        ) from None
    return audit


def verify_installed(raw: str, manifest: dict, version: str) -> dict:
    try:
        body = json.loads(raw)
        installed = body["installed"]
        if installed["appliance"]["version"] != version or installed.get("partial"):
            raise ValueError("wrong appliance release")
        expected = manifest["components"]
        actual = installed["components"]
        if set(actual) != set(expected):
            raise ValueError("component set differs")
        for name, component in expected.items():
            if (
                actual[name]["version"] != component["version"]
                or actual[name].get("commit") != component["commit"]
            ):
                raise ValueError("component version or commit differs")
    except (ValueError, TypeError, KeyError):
        raise PermanentError(
            "the installed release does not match the verified manifest"
        ) from None
    return {
        name: {"version": c["version"], "commit": c["commit"]}
        for name, c in actual.items()
    }


@pipeline.phase(
    "P7",
    describe=lambda ctx: (
        f"install verified release {ctx.version} with {ctx.config.image.install_method}, without starting services"
    ),
)
def install_release(ctx):
    manifest = _manifest(ctx)
    method = ctx.config.image.install_method
    image_policy, image_refs = _image_policy(manifest)
    if image_refs and method != "checkouts":
        raise Blocked(
            "prebuilt-only factory installation requires explicit image.install_method=checkouts"
        )
    manifest_sha = _manifest_sha(ctx, manifest) if image_refs else None
    prebuilt_audit = None
    target = remote.target_from_result(ctx.result_of("P5"))
    release_dir = ctx.result_of("P6")["release_dir"]
    # Copy the daemon's trust, never a key supplied by the release being tested.
    if not ctx.options.allow_unsigned:
        trusted = ctx.config.paths.allowed_signers
        if not trusted.is_file():
            raise PermanentError("the build host's pinned signing trust is missing")
        ctx.remote.put(target, trusted, f"{release_dir}/build-allowed-signers")
        ctx.remote.run(
            target,
            [
                "sudo",
                "-n",
                "install",
                "-d",
                "-m",
                "0755",
                "/etc/ginnungagap/update-trust",
            ],
        )
        ctx.remote.run(
            target,
            [
                "sudo",
                "-n",
                "install",
                "-m",
                "0644",
                f"{release_dir}/build-allowed-signers",
                "/etc/ginnungagap/update-trust/allowed_signers",
            ],
        )
    try:
        if method == "update":
            # Newer updaters can pull prebuilt registry images. This factory
            # deliberately rebuilds the verified source during prepare, so do
            # not leave unrelated digest references behind from an earlier pull.
            help_text = ctx.remote.run(
                target, ["sudo", "-n", "ginnungagap", "update", "--help"]
            ).stdout
            command = [
                "sudo",
                "-n",
                "env",
                "GINNUNGAGAP_INSTALL_ARGS=--image",
                "ginnungagap",
                "update",
                "--apply",
                "--offline-dir",
                release_dir,
                "--version",
                ctx.version,
                "--no-start",
                "--yes",
            ]
            if ctx.options.allow_prerelease:
                command += ["--channel", "prerelease"]
            if re.search(r"(?<![\w-])--build-local(?![\w-])", help_text):
                command.append("--build-local")
            ctx.remote.run(target, command, timeout=3600)
            appliance_dir = "/var/lib/ginnungagap/components/Ginnungagap-Appliance"
        elif method == "checkouts":
            ctx.remote.run(
                target,
                ["sudo", "-n", "python3", "-c", _CHECKOUTS, release_dir],
                timeout=600,
            )
            appliance_dir = "/srv/repos/Ginnungagap-Appliance"
            ctx.remote.run(
                target,
                [
                    "sudo",
                    "-n",
                    f"{appliance_dir}/install.sh",
                    "--repos-dir",
                    "/srv/repos",
                    "--image",
                    "--no-start",
                    "--manifest",
                    f"{release_dir}/components.json",
                ],
                timeout=1200,
            )
            command = ["sudo", "-n", "ginnungagap", "build-images"]
            if image_refs:
                help_text = ctx.remote.run(target, [*command, "--help"]).stdout
                if not _advertises_prebuilt_only(help_text):
                    raise Blocked(
                        "installed release lacks prebuilt-only image pull capability"
                    )
                command.append("--prebuilt-only")
            built = ctx.remote.run(target, command, timeout=3600)
            if image_refs:
                prebuilt_audit = _prebuilt_audit(
                    built.stdout, ctx.version, manifest_sha, image_refs
                )
            record = (
                "set -a; . /etc/ginnungagap/install.env; set +a; exec python3 -c "
                + shlex.quote(_RECORD_CHECKOUTS)
                + " "
                + shlex.quote(release_dir)
            )
            ctx.remote.run(target, ["sudo", "-n", "sh", "-c", record], timeout=600)
            appliance_dir = "/var/lib/ginnungagap/components/Ginnungagap-Appliance"
        else:
            raise PermanentError("unsupported image installation method")
    except CommandFailed as exc:
        # Transport failures already have their own classification. Treat only
        # actual, recognizable download/build-network failures as retryable.
        if any(
            word in (exc.stderr + exc.stdout).lower()
            for word in (
                "temporary failure in name resolution",
                "connection reset",
                "network is unreachable",
            )
        ):
            raise TransientError(
                "release installation lost a required network service"
            ) from None
        raise
    result = ctx.remote.run(
        target, ["sudo", "-n", "ginnungagap", "update", "--versions", "--json"]
    )
    components = verify_installed(result.stdout, manifest, ctx.version)
    query = (
        "set -a; . /etc/ginnungagap/install.env; set +a; exec python3 -c "
        + shlex.quote(_COMPOSE_IMAGES)
    )
    response = ctx.remote.run(target, ["sudo", "-n", "sh", "-c", query])
    try:
        images = json.loads(response.stdout)
        if (
            not isinstance(images, list)
            or not images
            or not all(isinstance(x, str) and x and "<none>" not in x for x in images)
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise PermanentError(
            "could not establish the expected compose image set"
        ) from None
    if prebuilt_audit and not {
        entry["compose_tag"] for entry in prebuilt_audit["services"].values()
    }.issubset(images):
        raise PermanentError(
            "prebuilt image audit differs from the expected compose image set"
        )
    return {
        "installed": ctx.version,
        "components": components,
        "method": method,
        "image_policy": image_policy,
        "image_refs": image_refs,
        "prebuilt_audit": prebuilt_audit,
        "appliance_dir": appliance_dir,
        "images": images,
    }


@pipeline.phase(
    "P8",
    describe=lambda ctx: (
        "run the installed release's prepare-image script as a detached root unit"
    ),
)
def prepare_image(ctx):
    manifest = _manifest(ctx)
    image_policy, image_refs = _image_policy(manifest)
    manifest_sha = _manifest_sha(ctx, manifest) if image_refs else None
    installed = ctx.result_of("P7")
    if image_refs and (
        installed.get("method") != "checkouts"
        or installed.get("image_policy") != image_policy
        or installed.get("image_refs") != image_refs
    ):
        raise Blocked(
            "prebuilt preparation requires the recorded strict checkout installation"
        )
    if image_refs:
        recorded_audit = _prebuilt_audit(
            PREBUILT_AUDIT_MARKER + json.dumps(installed.get("prebuilt_audit")),
            ctx.version,
            manifest_sha,
            image_refs,
        )
    target = remote.target_from_result(ctx.result_of("P5"))
    appliance = installed["appliance_dir"]
    unit = f"imgprep-{ctx.run_id.replace('.', '-')}".lower()
    log = "/run/imgprep.log"
    images = installed["images"]
    arguments = []
    if image_refs:
        help_text = ctx.remote.run(
            target, ["sudo", "-n", f"{appliance}/image/prepare-image.sh", "--help"]
        ).stdout
        if not _advertises_prebuilt_only(help_text):
            raise Blocked("installed prepare script lacks prebuilt-only capability")
        arguments.append("--prebuilt-only")
    result = ctx.remote.run_detached(
        target,
        unit,
        [
            "python3",
            "-c",
            _PREPARE,
            f"{appliance}/image/prepare-image.sh",
            json.dumps(images),
            ctx.result_of("P6")["release_dir"],
            *arguments,
        ],
        log_path=log,
        timeout=5400,
        success_marker=PREPARED_MARKER,
        poll_seconds=10,
    )
    if not result.ok or PREPARED_MARKER not in result.stdout:
        raise PermanentError("image preparation did not complete successfully")
    prebuilt_audit = None
    if image_refs:
        prebuilt_audit = _prebuilt_audit(
            result.stdout, ctx.version, manifest_sha, image_refs
        )
        if prebuilt_audit != recorded_audit:
            raise PermanentError(
                "prebuilt service image identity changed during preparation"
            )
    return {
        "prepared": True,
        "log_path": log,
        "unit": unit,
        "images": images,
        "image_policy": image_policy,
        "image_refs": image_refs,
        "prebuilt_audit": prebuilt_audit,
    }


def verify_ova(
    path: Path,
    *,
    minimum_bytes: int = 1_500_000_000,
    maximum_bytes: int = 4_500_000_000,
) -> None:
    """Re-read every archive member and its SHA-256 manifest without extraction."""
    path = Path(path)
    if not minimum_bytes <= path.stat().st_size <= maximum_bytes:
        raise PermanentError("the exported OVA has an implausible size")
    expected = {}
    actual = {}
    descriptor = None
    try:
        with tarfile.open(path, "r|*") as archive:
            for index, member in enumerate(archive):
                name = PurePosixPath(member.name)
                if (
                    not member.isfile()
                    or len(name.parts) != 1
                    or "\\" in member.name
                    or member.name in actual
                ):
                    raise ValueError("unsafe archive member")
                if index == 0 and name.suffix != ".ovf":
                    raise ValueError("OVF descriptor is not first")
                stream = archive.extractfile(member)
                if name.suffix == ".mf":
                    if member.size > 1024 * 1024 or expected:
                        raise ValueError("invalid OVA manifest")
                    for line in stream.read().decode("ascii").splitlines():
                        match = re.fullmatch(
                            r"SHA256\(([^()/\\]+)\)\s*=\s*([0-9a-fA-F]{64})", line
                        )
                        if match is None or match[1] in expected:
                            raise ValueError("invalid manifest hash")
                        expected[match[1]] = match[2].lower()
                    continue
                digest = hashlib.sha256()
                body = [] if index == 0 else None
                for block in iter(lambda stream=stream: stream.read(1024 * 1024), b""):
                    digest.update(block)
                    if body is not None:
                        if member.size > 4 * 1024 * 1024:
                            raise ValueError("OVF descriptor too large")
                        body.append(block)
                actual[member.name] = digest.hexdigest()
                if body is not None:
                    descriptor = b"".join(body).decode("utf-8")
        if (
            not expected
            or expected != actual
            or not any(name.endswith(".vmdk") for name in actual)
        ):
            raise ValueError(
                "OVA manifest does not describe exactly the archived files"
            )
        root = ET.fromstring(descriptor or "")
        references = {
            element.attrib.get("{http://schemas.dmtf.org/ovf/envelope/1}href")
            for element in root.iter()
            if element.tag.endswith("}File")
        }
        if not references or any(ref not in actual for ref in references):
            raise ValueError("OVF references missing files")
    except (tarfile.TarError, ValueError, UnicodeError, ET.ParseError, OSError) as exc:
        raise PermanentError(
            "exported OVA validation failed", detail=type(exc).__name__
        ) from None


def register_phases(registry):
    remote.register_phases(registry)
    for pid in ("P7", "P8"):
        registered = pipeline.DEFAULT_REGISTRY.get(pid)
        registry.register(
            pid, registered.fn, describe=registered.describe, replace=True
        )
