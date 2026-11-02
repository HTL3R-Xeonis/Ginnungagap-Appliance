# syntax=docker/dockerfile:1
# The GNS3 template collector as its own service: takes its hosts (and their ESXi passwords) from the app's host profiles
# in the read-only mounted data volume, asks each host's GNS3 VM for its templates and writes gns3_templates.yml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY --from=services /gns3-reader/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
COPY --from=services /gns3-reader/read_gns3_templates.py ./read_gns3_templates.py

CMD ["sh", "-c", "while true; do python read_gns3_templates.py --config /config/gns3_config.yml --profiles-dir /host-data/settings_profiles; sleep \"${REFRESH_SECONDS:-300}\"; done"]
