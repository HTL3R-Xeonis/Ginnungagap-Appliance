# syntax=docker/dockerfile:1
# The OVA deploy service (repository TopologyBuilderServices) as its own container. Its code is used as it is; only a
# health endpoint is added around it (ova_deploy_health.py), because the service has none.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY --from=service requirements.txt .
RUN pip install -r requirements.txt
COPY --from=service app.py deploy_ova.py progress_stream.py settings.py ./
COPY docker/ova_deploy_health.py ./

CMD ["uvicorn", "ova_deploy_health:app", "--host", "0.0.0.0", "--port", "8000"]
