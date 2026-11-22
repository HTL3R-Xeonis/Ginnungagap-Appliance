#!/usr/bin/env python3
"""
Builds the service images of ONE appliance release and pushes them to the registry (the release pipeline, stage 2).

  python release/build_images.py --repos-dir DIR --version 1.3.6 --registry ghcr.io/htl3r-xeonis --push --out dist/images.json

`--repos-dir` holds the five components unpacked as the appliance expects them (Igdrasyl, TopologyBuilder, Hugyn-and-Munyn,
Mimyr; the appliance itself is this checkout). It builds every image the way `ginnungagap build-images` does (so what runs is what
the appliance would have built itself), tags `<registry>/ginnungagap-<service>:<version>`, pushes it (only with --push; without
it nothing leaves this machine and the result is an empty list, for a dry run) and writes `images.json`: for each service the
reference BY DIGEST, which `release/build_release.py --images` puts into the signed components.json and `ginnungagap update`
pulls. The digest is read back from Docker after the push, never guessed.

The registry login is the caller's (`docker login ghcr.io` with the workflow's token).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ginnungagap import manifest as manifest_module
from ginnungagap import operate

PROJECT = "ginnungagap"  # the compose project name: the images are called ginnungagap-<service>


class ImageError(Exception):
    """An image could not be built or published; the message says which."""


Run = Callable[[list[str]], int]
Inspect = Callable[[list[str]], str | None]


def _run(args: list[str]) -> int:
    return subprocess.run(args, check=False).returncode


def _inspect(args: list[str]) -> str | None:
    done = subprocess.run(args, capture_output=True, text=True, check=False)
    return done.stdout if done.returncode == 0 else None


def digest_reference(registry: str, service: str, inspect: Inspect) -> str:
    """`<registry>/ginnungagap-<service>@sha256:...` as Docker knows it after the push. :raises ImageError:"""
    repository = f"{registry}/{PROJECT}-{service}"
    answer = inspect(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{json .RepoDigests}}",
            f"{repository}:latest",
        ]
    )
    try:
        found = [
            entry
            for entry in json.loads(answer or "[]")
            if isinstance(entry, str) and entry.startswith(repository + "@sha256:")
        ]
    except ValueError:
        found = []
    if not found:
        raise ImageError(
            f"{service}: Docker knows no digest of {repository} after the push"
        )
    return found[0]


def publish(
    version: str,
    registry: str,
    push: bool,
    run: Run = _run,
    inspect: Inspect = _inspect,
    out: Callable[[str], None] = print,
) -> dict[str, dict[str, str]]:
    """Tags (and with `push` pushes) every service image; returns service -> {ref, tag} ({} without push)."""
    images: dict[str, dict[str, str]] = {}
    for service in manifest_module.IMAGE_SERVICES:
        local = operate.image_reference(PROJECT, service)
        if operate.image_id(local, inspect) is None:
            raise ImageError(f"{service}: the image {local} was not built")
        tag = f"{registry}/{PROJECT}-{service}:{version}"
        latest = f"{registry}/{PROJECT}-{service}:latest"
        # `latest` only so that the digest can be read back from one well-known name; the appliance pulls by digest
        for target in (tag, latest):
            if run(["docker", "tag", local, target]) != 0:
                raise ImageError(f"{service}: tagging {target} failed")
        if not push:
            out(f"{service}: built, not pushed (no --push)")
            continue
        for target in (tag, latest):
            if run(["docker", "push", target]) != 0:
                raise ImageError(f"{service}: pushing {target} failed")
        images[service] = {
            "ref": digest_reference(registry, service, inspect),
            "tag": tag,
        }
        out(f"{service}: {images[service]['ref']}")
    return images


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repos-dir", type=Path, required=True)
    parser.add_argument(
        "--version", required=True, help="the appliance version, like 1.3.6"
    )
    parser.add_argument("--registry", default="ghcr.io/htl3r-xeonis")
    parser.add_argument(
        "--push",
        action="store_true",
        help="push the images (without it nothing leaves this machine)",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="where images.json is written"
    )
    args = parser.parse_args(argv)
    if not args.repos_dir.is_dir():
        print(f"build_images: {args.repos_dir} is not a folder", file=sys.stderr)
        return 1
    os.environ["REPOS_DIR"] = str(args.repos_dir.resolve())
    generator = args.repos_dir / manifest_module.CHECKOUT_NAMES["generator"] / "config"
    if operate.build_images(ROOT, generator) != 0:
        print("build_images: building the images failed", file=sys.stderr)
        return 1
    try:
        images = publish(args.version, args.registry, args.push)
    except ImageError as exc:
        print(f"build_images: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(images, indent=2) + "\n", encoding="utf-8")
    print(f"{len(images)} image(s) in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
