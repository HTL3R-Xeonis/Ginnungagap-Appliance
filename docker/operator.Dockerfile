# syntax=docker/dockerfile:1
# Ginnungagap (the app, Igdrasyl) with Mjonyr (the builder library; its Python distribution is still called topologybuilder),
# built from the two sibling repositories (compose.yml gives them as the named build contexts "operator" and "builder"). Run from /srv/ginnungagap because the app serves its static
# files and the licence from there.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /srv/topologybuilder
COPY --from=builder /pyproject.toml /README.md ./
COPY --from=builder /main.py ./main.py
COPY --from=builder /src ./src
RUN pip install .

WORKDIR /srv/ginnungagap
COPY --from=operator /pyproject.toml ./pyproject.toml
COPY docker/operator_requirements.py /tmp/operator_requirements.py
# the app's own pinned dependencies, minus Mjonyr, i.e. topologybuilder (installed above from this checkout, not from GitHub)
RUN python /tmp/operator_requirements.py pyproject.toml > requirements.txt && pip install -r requirements.txt
COPY --from=operator /app ./app
COPY --from=operator /LICENSE ./LICENSE

# The app log (./logs/app.log, relative to the working directory) goes to the data volume like everything else the app keeps, so a
# recreated container does not lose it; the folder is made at start because the volume is mounted over /data at run time.
RUN ln -s /data/logs /srv/ginnungagap/logs

EXPOSE 8123
CMD ["sh", "-c", "mkdir -p /data/logs && exec uvicorn app.main:app --host 0.0.0.0 --port 8123"]
