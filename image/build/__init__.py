"""
The automated image build (plan: PLAN_automated_image_builds.md in the handoff repo). See README.md in this folder.

    ports.py      the contracts: Protocols for every outside world, the error classes (transient / permanent / blocked / deferred)
    names.py      image, template, folder, VM, run and annotation names; the never-overwrite checks
    config.py     build-host.yml loader and validator, the secret-file rule
    pipeline.py   the phase runner P0-P16: registry, run state, retry, cleanup, janitor, lock, dry run, stop-after

The modules import each other relatively, so the package works as `image.build` (tests) and as `build` (the CLI in image/).
"""
