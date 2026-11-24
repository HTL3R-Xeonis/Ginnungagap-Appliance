"""The optional read-only status page: contract of the document, the two paths, the refusals and the config keys."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest
import yaml

from image import build_image as cli
from image.build import status, status_server
from image.build.config import ConfigError, parse_config

ROOT = Path(__file__).resolve().parents[1]
SECRET = "hunter2-very-secret-value"


def make_config(**extra):
    return parse_config({"esxi": {"datastore": "ds", "network": "net"}, **extra})


@pytest.fixture
def state(tmp_path):
    folder = tmp_path / "state"
    folder.mkdir()
    return folder


@pytest.fixture
def server(state):
    instance = status_server.make_server(state, "127.0.0.1", 0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(5)


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=5
    )
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response, response.read()
    finally:
        connection.close()


def get_document(server):
    code, _response, body = request(server, "GET", "/status.json")
    assert code == 200
    return json.loads(body)


def add_summary(state, version, day, *, result="published", reason="", checks=None):
    summary = {
        "run_id": f"{version}-202611{day}T100000Z",
        "version": version,
        "result": result,
        "reason": reason,
        "started_at": f"2026-11-{day}T09:00:00Z",
        "finished_at": f"2026-11-{day}T10:00:00Z",
        "published": result == "published",
        "acceptance": {"passed": True, "checks": checks} if checks is not None else {},
        # None of the following may ever be returned.
        "facts": {"password": SECRET, "esxi_host": "10.20.20.205"},
        "base": {"path": "/etc/ginnungagap-image-build/secrets/esxi-205.password"},
    }
    status.write_json(state / "builds" / version / "summary.json", summary)
    return summary


def add_run(state, run_id, run_status="running", phases=None):
    status.write_json(
        state / "runs" / run_id / "state.json",
        {
            "schema": 1,
            "run_id": run_id,
            "version": run_id.split("-20")[0],
            "status": run_status,
            "started_at": "2026-11-24T10:00:00Z",
            "finished_at": None if run_status == "running" else "2026-11-24T11:00:00Z",
            "phases": phases or {},
            "resources": [{"ref": "10.20.20.205", "kind": "vm"}],
        },
    )


# ---------- paths, methods, headers ----------


def test_healthz_and_status_answer_with_the_required_headers(server):
    for path in ("/healthz", "/status.json", "/status.json?x=1&token=abc"):
        code, response, body = request(server, "GET", path)
        assert code == 200
        assert response.getheader("Content-Type") == "application/json; charset=utf-8"
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("Content-Length") == str(len(body))
        assert not [
            h
            for h, _ in response.getheaders()
            if h.lower().startswith("access-control")
        ]
        assert "Python" not in (response.getheader("Server") or "")
    assert json.loads(request(server, "GET", "/healthz")[2]) == {"ok": True}


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/index.html",
        "/status",
        "/status.json/",
        "/healthz/x",
        "/runs/",
        "/latest.json",
        "/builds/1.3.0/summary.json",
        "/../etc/passwd",
        "/status.json/../../../etc/passwd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/..%2f..%2fetc/passwd",
        "/etc/passwd",
        "/%00",
    ],
)
def test_every_other_path_is_a_404_without_a_listing(server, path):
    code, response, body = request(server, "GET", path)
    assert code == 404
    assert json.loads(body) == {"error": "not found"}
    assert response.getheader("Cache-Control") == "no-store"
    head_code, _response, head_body = request(server, "HEAD", path)
    assert head_code == 404 and head_body == b""


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "BREW"]
)
def test_every_other_method_is_a_405(server, method):
    code, response, body = request(server, method, "/status.json", body=b"x=1")
    assert code == 405
    assert response.getheader("Allow") == "GET, HEAD"
    assert json.loads(body) == {"error": "method not allowed"}
    assert not [
        h for h, _ in response.getheaders() if h.lower().startswith("access-control")
    ]


def test_head_has_the_headers_of_get_but_no_body(server):
    code, response, body = request(server, "HEAD", "/status.json")
    assert code == 200 and body == b""
    assert int(response.getheader("Content-Length")) > 0
    assert response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    assert request(server, "HEAD", "/healthz")[2] == b""


def test_an_oversized_request_line_is_refused_by_the_stdlib_with_json(server):
    code, _response, body = request(server, "GET", "/" + "a" * 70000)
    assert code == 414
    assert json.loads(body) == {"error": "request-uri too long"}


def test_the_log_never_contains_the_query_string(server, capsys):
    request(server, "GET", "/status.json?token=" + SECRET)
    request(server, "GET", "/healthz?password=" + SECRET)
    # The server thread writes after the response is sent.
    server.shutdown()
    err = capsys.readouterr().err
    assert "/status.json" in err and "/healthz" in err
    assert SECRET not in err and "token=" not in err and "password=" not in err


# ---------- the document ----------


def test_empty_state_directory_is_idle_with_no_latest(server):
    document = get_document(server)
    assert set(document) == {
        "schema",
        "generated_at",
        "worker",
        "state",
        "paused",
        "active",
        "latest",
        "builds",
    }
    assert document["schema"] == 1
    assert document["generated_at"].endswith("Z") and "T" in document["generated_at"]
    assert document["worker"]
    assert document["state"] == "idle" and document["paused"] is False
    assert document["active"] is None and document["latest"] is None
    assert document["builds"] == []


def test_a_missing_state_directory_still_answers(tmp_path):
    document = status_server.build_document(tmp_path / "nowhere")
    assert document["state"] == "idle" and document["builds"] == []


def test_pause_file_means_paused(server, state):
    (state / "PAUSE").write_text("hold\n")
    document = get_document(server)
    assert document["state"] == "paused" and document["paused"] is True
    assert document["active"] is None


def test_unfinished_run_means_building_even_when_paused(server, state):
    add_run(state, "1.3.6-20261123T100000Z", "published")
    add_run(
        state,
        "1.3.7-20261124T100000Z",
        phases={
            "P0": {"name": "preflight", "status": "ok"},
            "P1": {"name": "catalog", "status": "ok"},
            "P2": {"name": "release", "status": "running"},
            "P3": {"name": "base", "status": "pending"},
        },
    )
    assert get_document(server)["state"] == "building"
    (state / "PAUSE").write_text("")
    document = get_document(server)
    assert document["state"] == "building" and document["paused"] is True
    assert document["active"] == {
        "run_id": "1.3.7-20261124T100000Z",
        "release": "1.3.7",
        "phase": "P2",
        "started_at": "2026-11-24T10:00:00Z",
    }
    assert "10.20.20.205" not in json.dumps(document)


def test_finished_runs_do_not_make_the_worker_busy(server, state):
    add_run(state, "1.3.6-20261123T100000Z", "failed")
    assert get_document(server)["state"] == "idle"


def test_latest_and_builds_use_only_the_whitelisted_fields(server, state):
    checks = [{"name": "a", "passed": True}, {"name": "b", "passed": False}]
    published = add_summary(state, "1.3.6", "20", checks=[{"passed": True}] * 3)
    failed = add_summary(
        state,
        "1.3.7",
        "21",
        result="failed",
        reason="boom",
        checks=checks,
    )
    status.write_json(state / "latest.json", failed)
    document = get_document(server)
    assert document["latest"] == {
        "version": "1.3.7",
        "result": "failed",
        "started_at": "2026-11-21T09:00:00Z",
        "finished_at": "2026-11-21T10:00:00Z",
        "template": None,
        "reason": "boom",
        "acceptance": {"passed": 1, "total": 2},
    }
    assert [b["version"] for b in document["builds"]] == ["1.3.7", "1.3.6"]
    assert document["builds"][1] == {
        "version": "1.3.6",
        "result": "published",
        "started_at": "2026-11-20T09:00:00Z",
        "finished_at": "2026-11-20T10:00:00Z",
        "template": "ginnungagap-appliance-v1.3.6",
        "reason": None,
        "acceptance": {"passed": 3, "total": 3},
    }
    assert published["facts"]["password"] == SECRET  # the fixture does hold it
    raw = json.dumps(document)
    for forbidden in (SECRET, "10.20.20.205", "/etc/", "password", "esxi"):
        assert forbidden not in raw


@pytest.mark.parametrize(
    "content", ["{not json", "[]", '"text"', "", "\x00\x01", '{"version": 5}']
)
def test_a_corrupt_latest_file_never_breaks_the_page(server, state, content):
    (state / "latest.json").write_text(content)
    add_summary(state, "1.3.6", "20")
    document = get_document(server)
    assert document["latest"] is None
    assert [b["version"] for b in document["builds"]] == ["1.3.6"]


def test_corrupt_summaries_and_run_states_are_skipped(server, state):
    (state / "builds" / "1.3.5").mkdir(parents=True)
    (state / "builds" / "1.3.5" / "summary.json").write_text("{broken")
    add_summary(state, "1.3.6", "20")
    (state / "runs" / "1.3.7-20261124T100000Z").mkdir(parents=True)
    (state / "runs" / "1.3.7-20261124T100000Z" / "state.json").write_text("nope")
    document = get_document(server)
    assert document["state"] == "idle"
    assert [b["version"] for b in document["builds"]] == ["1.3.6"]


def test_a_symlinked_or_oversized_latest_is_not_read(server, state, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"version": "9.9.9", "result": "published"}))
    (state / "latest.json").symlink_to(target)
    assert get_document(server)["latest"] is None
    (state / "latest.json").unlink()
    status.write_json(
        state / "latest.json",
        {"version": "1.3.6", "result": "failed", "reason": "x" * (2 * 1024 * 1024)},
    )
    assert get_document(server)["latest"] is None


def test_unknown_values_become_null_and_an_unknown_result_is_never_a_success(
    server, state
):
    status.write_json(
        state / "builds" / "1.3.6" / "summary.json",
        {
            "version": "1.3.6",
            "result": "weird",
            "started_at": "yesterday",
            "finished_at": ["2026-11-20T10:00:00Z"],
            "reason": 42,
            "acceptance": {"checks": "none"},
        },
    )
    status.write_json(
        state / "builds" / "bad" / "summary.json",
        {"version": "../../x", "result": "published"},
    )
    document = get_document(server)
    assert document["builds"] == [
        {
            "version": "1.3.6",
            "result": "failed",
            "started_at": None,
            "finished_at": None,
            "template": None,
            "reason": None,
            "acceptance": None,
        }
    ]


def test_at_most_twenty_builds_newest_first(server, state):
    for index in range(25):
        add_summary(state, f"1.3.{index}", f"{index + 1:02d}")
    document = get_document(server)
    versions = [b["version"] for b in document["builds"]]
    assert versions == [f"1.3.{i}" for i in range(24, 4, -1)]
    assert len(versions) == 20


def test_secret_looking_fields_are_not_echoed_and_free_text_is_cut_and_scrubbed(
    server, state
):
    reason = (
        "ESXi 10.20.20.205 refused root password=" + SECRET + "\nwhile reading "
        "/etc/ginnungagap-image-build/secrets/esxi-205.password via https://u:p@10.20.20.205/ui\x1b[31m "
        + "very long "
        * 60
    )
    summary = add_summary(state, "1.3.6", "20", result="failed", reason=reason)
    summary["token"] = SECRET
    summary["traceback"] = "Traceback (most recent call last):\n  File secret.py"
    status.write_json(state / "latest.json", summary)
    document = get_document(server)
    raw = json.dumps(document)
    for forbidden in (
        SECRET,
        "10.20.20.205",
        "/etc/",
        "https://",
        "Traceback",
        "\\u001b",
        "\\n",
    ):
        assert forbidden not in raw, forbidden
    shown = document["latest"]["reason"]
    assert len(shown) <= 200 and shown.endswith("...")
    assert shown.startswith("ESXi <address> refused root password=<hidden>")
    assert document["builds"][0]["reason"] == shown


def test_clean_text_rules():
    assert status_server.clean_text("a\x00b\x07c\nd\te") == "abc d e"
    assert status_server.clean_text("\x00\x01") is None
    assert status_server.clean_text(5) is None
    assert len(status_server.clean_text("x" * 500)) == 200
    assert (
        status_server.clean_reason("tag v1.3.6 and/or retry")
        == "tag v1.3.6 and/or retry"
    )


def test_sample_document(state, capsys):
    add_run(
        state,
        "1.3.7-20261124T100000Z",
        phases={"P0": {"status": "ok"}, "P13": {"status": "running"}},
    )
    add_summary(state, "1.3.6", "20", checks=[{"passed": True}] * 42)
    add_summary(state, "1.3.5", "10", result="blocked", reason="catalog changed")
    status.write_json(
        state / "latest.json",
        json.loads((state / "builds/1.3.6/summary.json").read_text()),
    )
    document = status_server.build_document(state, hostname="imgbuild")
    document["generated_at"] = "2026-11-24T10:15:00Z"
    print(json.dumps(document, indent=1))
    assert document["state"] == "building"
    assert document["active"]["phase"] == "P13"
    assert [b["result"] for b in document["builds"]] == ["published", "blocked"]


# ---------- the command ----------


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "build-host.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "esxi": {"datastore": "build-datastore", "network": "build-network"},
                "paths": {"state_dir": str(tmp_path / "state")},
            }
        )
    )
    return path


def test_serve_status_refuses_to_start_while_status_http_is_false(
    config_file, monkeypatch, capsys
):
    monkeypatch.setattr(
        status_server, "serve", lambda *a: pytest.fail("the server must not start")
    )
    assert cli.main(["--config", str(config_file), "serve-status"]) == 2
    assert "status_http: true" in capsys.readouterr().err


def test_serve_status_passes_the_configured_address_and_state_dir(
    config_file, tmp_path, monkeypatch
):
    config_file.write_text(
        config_file.read_text()
        + "status_http: true\nstatus_bind: 127.0.0.1\nstatus_port: 18183\n"
    )
    calls = []
    monkeypatch.setattr(status_server, "serve", lambda *a: calls.append(a))
    assert cli.main(["--config", str(config_file), "serve-status"]) == 0
    assert calls == [(tmp_path / "state", "127.0.0.1", 18183)]


def test_serve_status_needs_no_secrets_or_esxi(config_file, monkeypatch):
    config_file.write_text(config_file.read_text() + "status_http: true\n")
    monkeypatch.setattr(cli, "runtime", lambda *a: pytest.fail("contacted ESXi"))
    monkeypatch.setattr(status_server, "serve", lambda *a: None)
    assert cli.main(["--config", str(config_file), "serve-status"]) == 0


def test_serve_stops_on_sigterm(state, monkeypatch):
    import os
    import signal
    import time

    original = status_server.make_server
    ready = threading.Event()

    def make_and_signal(*args):
        instance = original(*args)
        ready.set()
        return instance

    def stop_later():
        ready.wait(5)
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(status_server, "make_server", make_and_signal)
    threading.Thread(target=stop_later, daemon=True).start()
    before = signal.getsignal(signal.SIGTERM)
    status_server.serve(state, "127.0.0.1", 0)  # returns after SIGTERM
    assert signal.getsignal(signal.SIGTERM) == before


# ---------- configuration ----------


def test_status_keys_have_the_documented_defaults():
    config = make_config()
    assert config.status_http is False
    assert config.status_bind == "0.0.0.0"
    assert config.status_port == 8183


def test_status_keys_are_read():
    config = make_config(status_http=True, status_bind="10.0.0.5", status_port=1024)
    assert (config.status_http, config.status_bind, config.status_port) == (
        True,
        "10.0.0.5",
        1024,
    )
    assert make_config(status_bind="::1", status_port=65535).status_port == 65535


@pytest.mark.parametrize("port", [0, 80, 1023, 65536, 100000, True, "8183", 81.5, -1])
def test_status_port_must_be_1024_to_65535(port):
    with pytest.raises(ConfigError) as error:
        make_config(status_port=port)
    assert any("status_port" in p for p in error.value.problems)


@pytest.mark.parametrize(
    "bind", ["localhost", "10.0.0", "999.1.1.1", "", 5, "0.0.0.0/0"]
)
def test_status_bind_must_be_an_ip_address(bind):
    with pytest.raises(ConfigError) as error:
        make_config(status_bind=bind)
    assert any("status_bind" in p for p in error.value.problems)


def test_example_file_documents_the_keys_and_stays_valid():
    text = (ROOT / "image/build/build-host.example.yml").read_text()
    assert "# status_bind: 0.0.0.0" in text and "# status_port: 8183" in text
    data = yaml.safe_load(text)
    data["esxi"].update(datastore="ds", network="net")
    assert parse_config(data).status_http is False


# ---------- the unit ----------


def test_status_unit_is_hardened_and_runs_the_installed_command():
    unit = (
        ROOT / "image/build/systemd/ginnungagap-image-build-status.service"
    ).read_text()
    for line in (
        "User=imgbuild",
        "Group=imgbuild",
        "ExecStart=/usr/local/bin/ginnungagap-image-build serve-status",
        "Restart=on-failure",
        "RestartPreventExitStatus=2",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    ):
        assert line in unit.splitlines(), line
    assert "ReadWritePaths" not in unit and "LoadCredential" not in unit
