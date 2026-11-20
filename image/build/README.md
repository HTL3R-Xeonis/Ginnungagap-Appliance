# image/build - the automated image build

The contracts and the phase runner of `PLAN_automated_image_builds.md` (handoff repo). WP1 delivered the four modules below;
WP2-WP7 add their files next to them and plug into `pipeline.py` without editing it. Plain Python 3.12 + PyYAML.
The modules import each other relatively: use `from image.build import pipeline` (tests) or `from . import pipeline` (inside the package).

| module | owns |
|---|---|
| `ports.py` | the Protocols `EsxiPort`, `RemotePort`, `ShareApiPort`, `TemplateApiPort`, `Clock`; the data they return (`VmInfo`, `CommandResult`, `RemoteTarget`, `ExportResult`, `HeadResult`, `TemplateEntry`); every error class; the read-only/mutating classification of each method |
| `names.py` | template/folder/stem, run id, `imgbuild-`/`imgaccept-` VM names, the annotation, `is_ours_*`, never-overwrite (`check_free`), `ova_url` |
| `config.py` | `build-host.yml` -> `BuildHostConfig` (`parse_config`, `load_config`), `read_secret` / `check_secret_file` |
| `pipeline.py` | the phase registry, `Pipeline.run`, `PhaseContext`, state.json, retry, cleanup, `Janitor`, `BuildLock`, dry run, stop-after, resume, `attempts_in_window` |
| `tests/helpers/fake_esxi.py` | `FakeEsxi` (an `EsxiPort`), `FakeClock` |

## Register a phase (WP2-WP7)

```python
from . import pipeline, names
from .ports import TransientError, PermanentError


@pipeline.phase(
    "P4",
    describe=lambda ctx: f"import the base OVA as {names.build_vm_name(ctx.run_id)}",
)
def deploy_base(ctx: pipeline.PhaseContext) -> dict | None:
    vm = names.build_vm_name(ctx.run_id)
    ctx.track_vm(vm)  # FIRST: cleanup by name, then create
    ctx.esxi.import_ova(
        ctx.results["P3"]["url"],
        vm,
        ctx.config.esxi.datastore,
        [ctx.config.esxi.network],
    )
    ctx.esxi.set_annotation(vm, names.annotation(ctx.run_id))
    return {"vm": vm}  # JSON facts -> state.json, later ctx.results["P4"]
```

* Ids are exactly `P0`..`P16` (`pipeline.PHASES`). `@pipeline.phase` registers in `pipeline.DEFAULT_REGISTRY`; a test (or the CLI)
  may build its own `PhaseRegistry` and give it to `Pipeline(registry=...)`. P15 (cleanup) and P16 (summary) have built-in defaults.
* `describe(ctx) -> str` is what a dry run prints for the phase (the command it would run); P0-P3 really run in a dry run, with
  READ-ONLY views of the ports (a mutating call raises `DryRunViolation`, `ctx.track_*` too).
* Return a mapping of JSON-able facts (paths and times are stored as text; resume restores them as text) or `None`. Secrets never.
* Raise by meaning: `TransientError` (retry once from P4), `PermanentError` (never), `Blocked` (loud, no retry), `Deferred`
  (not now, nothing created). Wrap library errors yourself; any other exception counts as a bug (permanent, traceback logged);
  only `TimeoutError`/`ConnectionError` are treated as transient as a last resort.
* Log only through `ctx.logger` (it redacts the ESXi password, registered secrets via `ctx.secret(v)`, token shapes and private
  keys); write only below `ctx.run_dir` (`ctx.subdir("release")`).
* Track every created thing BEFORE the call that creates it: `ctx.track_vm`, `track_datastore_folder`, `track_share_folder`
  (removed only when the run does NOT publish; the marker `.imgbuild-run` = run id proves it is ours), `track_path` (below the
  state dir only), `on_cleanup(kind, ref, fn)`. Entries are written to state.json, so the janitor finishes them after a crash.
  `ctx.add_diagnostic(label, fn(dir))` saves evidence into `runs/<id>/diag/` before a failed attempt is cleaned.
* A phase must be repeatable from its start after the cleanup of the failed attempt (a transient failure restarts at P4 with a
  new base VM; before P4 only the failed phase repeats). A failure after P14 is only a warning (the image stays published).
* `ctx.note(key, value)` adds a fact to the summary; `ctx.options` carries every CLI flag; `ctx.names`, `ctx.config`, `ctx.clock`
  (use `ctx.clock.sleep`, never `time.sleep`).

## The ports (method list)

* `EsxiPort`: `host`, `check_login`, `list_vms`, `get_vm`, `list_datastores`, `list_networks`, `datastore_free_bytes`,
  `list_datastore_folders`, `get_extra_config`, `wait_powered_off`, `wait_for_ip` (reads); `import_ova`, `set_annotation`,
  `set_extra_config`, `clear_guestinfo`, `power_on`, `power_off`, `shutdown_guest`, `screenshot`, `delete_vm`,
  `delete_datastore_folder`, `export_ova`, `close`.
* `RemotePort`: `generate_key`, `wait_ssh`, `run`, `run_detached`, `put`, `get`, `forget`.
* `ShareApiPort`: `free_bytes`, `list_folders`, `folder_files`, `read_text`, `size`, `sha256`, `url_for`, `head` (reads);
  `make_folder`, `put_file`, `put_text`, `remove_folder(marker_name, marker_text)`. (The share AND the `.181` web server.)
* `TemplateApiPort`: `check`, `list_templates` (reads); `refresh`. `Clock`: `now`, `monotonic`, `sleep` (`SystemClock`).

Everything is addressed by name, idempotent where cheap (`delete_vm`, `delete_datastore_folder`, `power_off`, `forget`), and
secret-free in arguments: the ESXi password lives in the adapter (it hands it to children only as `ESXI_PASSWORD`), guestinfo values
and command output are never logged, `RemotePort.run` argv is logged so secrets go through `stdin`/`env`. A new port method must be
added to exactly one of the `*_READ_ONLY` / `*_MUTATING` sets of `ports.py` (a test enforces it) and to the fakes.

## Write a fake

Implement the Protocol with plain Python in `tests/helpers/fake_<name>.py` and follow `fake_esxi.py`: in-memory state, a call
log (`Call(name, args, outcome)` that never holds a secret value), `inject(method, exc, skip=, times=, match=, side_effect=)` for
transient/permanent faults, helpers to seed the world (`add_vm`, `add_folder`), and `FakeClock` for waits (advance it instead of
sleeping). Keep the method names and parameter names identical to the Protocol (`test_fake_esxi_has_every_method_of_the_port...`
shows the check). For a quick stand-in `unittest.mock.create_autospec(RemotePort, instance=True)` works too.

## State on disk (`config.paths.state_dir`, default `/var/lib/ginnungagap-image-build`)

`lock` (flock), `PAUSE` (a file defers every real run), `runs/<run id>/{state.json, summary.json, run.log, NN-name.log, diag/, ...}`.
`state.json` schema 1 is documented in the `pipeline.py` docstring. Results: `published 0 | dry-run 0 | stopped 0 | failed 1 |
blocked 2 | deferred 3 | interrupted 4` (`RunOutcome.exit_code`). A deferral before a run directory exists (lock held, PAUSE, lab busy,
host not ready) creates no run directory; P16 runs once for every outcome that has one.

## Decisions to know

* A transient failure restarts at **P4**, not P5: P4 makes the base VM, and the retry needs a new one (the plan's "from P5" meant "after the checks").
* Lab busy = a powered-on VM that is neither on `esxi.allowed_running_vms` nor one of ours; replaceable through `PipelineHooks.lab_busy(esxi, config)`.
* The janitor runs at the start of every real run and in `Pipeline.run_janitor()` (both hold the lock). It touches only `imgbuild-`/`imgaccept-`
  VMs and datastore folders, and what a crashed run recorded in its own state.json (a share folder only with its marker, paths only below `runs/`).
* The config adds a few keys to plan section 10 that the phases need: `paths.state_dir`, `paths.allowed_signers`, `share.ssh_*`,
  `image.os_type`, `status_http`, `limits.*`.
