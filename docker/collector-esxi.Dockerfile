# syntax=docker/dockerfile:1
# The ESXi template collector as its own service: reads the YAML files of the template store and writes templates.yml
# for the template API, then waits REFRESH_SECONDS and does it again.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN pip install requests pyyaml
COPY --from=services /yaml-api/collect_templates.py ./collect_templates.py

CMD ["sh", "-c", "while true; do python collect_templates.py; sleep \"${REFRESH_SECONDS:-900}\"; done"]
