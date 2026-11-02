"""Prints the app's pinned dependencies except TopologyBuilder (installed from the sibling checkout, not from GitHub)."""

import sys
import tomllib


def main(pyproject: str) -> None:
    with open(pyproject, "rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    print("\n".join(d for d in dependencies if not d.startswith("topologybuilder")))


if __name__ == "__main__":
    main(sys.argv[1])
