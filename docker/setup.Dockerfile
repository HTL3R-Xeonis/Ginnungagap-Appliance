# syntax=docker/dockerfile:1
# The setup wizard: a plain server-rendered HTML form (docker/setup_app.py, built on ginnungagap/setup_wizard.py) that
# builds and validates a ginnungagap.yml for a fresh appliance. `caddy hash-password` is copied from the gateway's own
# image unchanged (compose.yml's additional_contexts.caddy) so the wizard can hash a typed password server-side without
# a second copy of Caddy's hashing code. This container never gets the Docker socket or sudo: see ginnungagap/bootstrap.py
# (how it is reached before any configuration exists) and ginnungagap/setup_watcher.py (how a submitted configuration
# reaches the host).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install fastapi "uvicorn[standard]" python-multipart pyyaml bcrypt
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
COPY ginnungagap ./ginnungagap
COPY docker/setup_app.py ./setup_app.py

CMD ["uvicorn", "setup_app:app", "--host", "0.0.0.0", "--port", "8000"]
