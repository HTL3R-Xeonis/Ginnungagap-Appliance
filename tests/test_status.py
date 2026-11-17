import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ginnungagap import status as st


def answers(table: dict[str, tuple[int | None, dict | str, str | None]]):
    """A fake fetch: path -> (status, body, error)."""

    def fetch(url: str):
        path = "/" + url.split("/", 3)[3]
        status, body, error = table[path]
        return status, body if isinstance(body, str) else json.dumps(body), error

    return fetch


ALL_OK = {
    "/health": (200, {"warning": None}, None),
    "/templates/esxi/health": (
        200,
        {"status": "ok", "templates": 2, "problems": []},
        None,
    ),
    "/templates/gns3/health": (
        200,
        {"status": "ok", "templates": 19, "servers": 1, "problems": []},
        None,
    ),
    "/generator/health": (200, {"status": "ok", "servers": 3, "problems": []}, None),
}


def rows_by_name(rows):
    return {row.name: row for row in rows}


def test_base_url_follows_tls_mode_and_can_be_overridden(make_config):
    assert (
        st.base_url(make_config(appliance={"http_port": 8080}))
        == "http://127.0.0.1:8080"
    )
    tls = make_config(
        appliance={
            "public_url": "https://g.lab",
            "tls": {"mode": "self-signed"},
            "https_port": 8443,
        }
    )
    assert st.base_url(tls) == "https://127.0.0.1:8443"
    assert st.base_url(tls, "http://other:81/") == "http://other:81"


def test_everything_healthy(make_config):
    rows = st.check(make_config(), "http://g", answers(ALL_OK))
    assert st.healthy(rows)
    by = rows_by_name(rows)
    assert by["template-esxi"].detail == "2 template(s)"
    assert by["template-gns3"].detail == "19 template(s) from 1 server(s)"
    assert by["generator"].detail == "3 LLM server(s)"
    assert (
        by["ova-deploy"].state == "external"
        and "10.20.20.172:8003" in by["ova-deploy"].detail
    )


def test_the_apps_own_warning_is_a_note_not_a_failure(make_config):
    table = {
        **ALL_OK,
        "/health": (200, {"warning": "The ESXi host 10.1.1.1 did not answer"}, None),
    }
    by = rows_by_name(st.check(make_config(), "http://g", answers(table)))
    assert by["ginnungagap"].state == "ok"
    assert "note: The ESXi host 10.1.1.1 did not answer" in by["ginnungagap"].detail


def test_a_degraded_service_is_named_with_its_problem(make_config):
    table = {
        **ALL_OK,
        "/templates/esxi/health": (
            503,
            {"status": "degraded", "problems": ["templates.yml not found"]},
            None,
        ),
    }
    rows = st.check(make_config(), "http://g", answers(table))
    assert not st.healthy(rows)
    assert rows_by_name(rows)["template-esxi"].state == "degraded"
    assert "templates.yml not found" in rows_by_name(rows)["template-esxi"].detail


def test_a_service_the_gateway_cannot_reach_is_down(make_config):
    table = {**ALL_OK, "/generator/health": (502, "", None)}
    by = rows_by_name(st.check(make_config(), "http://g", answers(table)))
    assert by["generator"].state == "down" and "no answer" in by["generator"].detail


def test_an_unreachable_gateway_makes_everything_down_with_the_reason(make_config):
    table = {path: (None, "", "Connection refused") for path in ALL_OK}
    rows = st.check(make_config(), "http://g", answers(table))
    assert {r.state for r in rows if r.name != "ova-deploy"} == {"down"}
    assert "Connection refused" in rows[0].detail


def test_a_login_wall_is_reported_as_such(make_config):
    table = {path: (401, "", None) for path in ALL_OK}
    assert (
        rows_by_name(st.check(make_config(), "http://g", answers(table)))[
            "ginnungagap"
        ].state
        == "login"
    )


def test_services_that_run_elsewhere_or_are_off_are_not_asked(make_config):
    config = make_config(
        services={"template_esxi": False, "generator": False, "ova_deploy": "off"},
        templates={"esxi_url": "http://e:8000"},
        generator={"url": "http://g:1"},
    )
    table = {
        p: v for p, v in ALL_OK.items() if p in ("/health", "/templates/gns3/health")
    }  # asking anything else would KeyError
    rows = rows_by_name(st.check(config, "http://g", answers(table)))
    assert (
        rows["template-esxi"].state == "off"
        and rows["generator"].state == "off"
        and rows["ova-deploy"].state == "off"
    )
    assert st.healthy(list(rows.values()))


def test_the_table_lists_every_row(make_config):
    text = st.render_table(st.check(make_config(), "http://g", answers(ALL_OK)))
    for name in (
        "ginnungagap",
        "template-esxi",
        "template-gns3",
        "generator",
        "ova-deploy",
    ):
        assert name in text


# ---------- the real fetch, against a real local server ----------


@pytest.fixture
def server():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            code = 503 if self.path == "/bad" else 200
            body = json.dumps({"path": self.path}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", seen
    httpd.shutdown()


def test_fetch_returns_status_and_body_also_for_error_statuses(server):
    url, _ = server
    fetch = st.make_fetch()
    assert fetch(url + "/ok")[0] == 200
    status, body, error = fetch(url + "/bad")
    assert (status, error) == (503, None) and json.loads(body)["path"] == "/bad"


def test_fetch_sends_basic_auth_only_when_given(server):
    url, seen = server
    st.make_fetch()(url + "/x")
    assert seen["auth"] is None
    st.make_fetch("admin", "pw")(url + "/x")
    assert seen["auth"] == "Basic YWRtaW46cHc="


def test_fetch_reports_a_refused_connection_instead_of_raising():
    status, body, error = st.make_fetch(timeout=2)("http://127.0.0.1:9/x")
    assert status is None and body == "" and error


def test_ova_deploy_as_a_container_is_asked_through_the_gateway(make_config):
    config = make_config(services={"ova_deploy": True})
    table = {**ALL_OK, "/ova/health": (200, {"status": "ok", "problems": []}, None)}
    by = rows_by_name(st.check(config, "http://g", answers(table)))
    assert by["ova-deploy"].state == "ok"
    assert "ESXi login comes with each deploy request" in by["ova-deploy"].detail
    assert "password" not in by["ova-deploy"].detail


def test_a_200_without_the_services_own_data_is_not_healthy(make_config):
    """A gateway with no site for the address it is asked on answers 200 with nothing at all (found on a real machine)."""
    table = {path: (200, "", None) for path in ALL_OK}
    rows = st.check(make_config(), "http://g", answers(table))
    assert not st.healthy(rows)
    by = rows_by_name(rows)
    assert (
        by["ginnungagap"].state == "down"
        and "wrong host name" in by["ginnungagap"].detail
    )
    assert by["generator"].state == "down"
