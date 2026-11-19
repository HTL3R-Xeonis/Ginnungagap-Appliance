# Third-party notices

This project is licensed under the GNU GPL v3 (see `LICENSE`). No third-party source code is copied into the repository.
The appliance builds and runs container images; they are pulled or built by the installing user and are not part of this
repository. Licenses were read from the image and package metadata (November 2026).

| Component | Used for | License |
| --- | --- | --- |
| `caddy:2` | the single web entry point (and `caddy hash-password`) | Apache-2.0 |
| `python:3.12-slim` | base of every service image | PSF license plus the Debian packages' own licenses |
| PyYAML | the `ginnungagap` tool | MIT |
| FastAPI, uvicorn, python-multipart | the setup container | MIT / BSD-3-Clause / Apache-2.0 |
| bcrypt | the setup container | Apache-2.0 |
| Docker Engine and Compose (installed from the Debian archive by `install.sh`) | running the containers | Apache-2.0 |

The other components (Igdrasyl, Mjonyr, Mimyr, Hugyn and Munyn: the code names; the Builder repository is still called TopologyBuilder) are separate
repositories, all under GPL-3.0; their own `THIRD_PARTY_NOTICES.md` files list what they use.
