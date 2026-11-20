"""
The release source without the REST API ("web mode"): the latest stable release from the redirect of `releases/latest` and the
files from the `releases/download/...` addresses. Everything runs against the fake GitHub (which serves the web side too and
counts what is an API call); nothing here reaches the real github.com.
"""

import json

import pytest

from ginnungagap import releases as rl
from ginnungagap import update as up
from tests.helpers import fake_release
from tests.helpers.fake_github import REPO, FakeGitHub
from tests.helpers.update_world import World

TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def folders(tmp_path):
    return [
        fake_release.make_release(tmp_path / "a", appliance="1.2.1"),
        fake_release.make_release(tmp_path / "b", appliance="1.3.0-rc.1"),
        fake_release.make_release(tmp_path / "c", appliance="1.3.0"),
    ]


@pytest.fixture
def github(folders):
    with FakeGitHub(folders) as fake:
        yield fake


def source(github, tmp_path, token=None, api="auto", **kwargs):
    token_file = None
    if token:
        token_file = tmp_path / "update-token"
        rl.write_token(token_file, token)
    return rl.GitHubSource(
        REPO,
        token_file=token_file,
        api_base=github.url,
        web_base=github.url,
        cache_path=tmp_path / "etag.json",
        api=api,
        **kwargs,
    )


# ---- the latest stable release and its files, without one API call ----


def test_the_latest_stable_release_is_found_and_downloaded_without_the_api(
    github, tmp_path
):
    src = source(github, tmp_path)
    found = src.candidates("stable")
    assert [r.version for r in found] == ["1.3.0"]  # the rc is not offered
    assert found[0].tag == "v1.3.0" and found[0].web and not found[0].prerelease
    dest = tmp_path / "dl"
    for name in ("components.json", "SHA256SUMS", "Igdrasyl-1.2.1.tar.gz"):
        path = src.fetch(found[0], name, dest)
        assert path.read_bytes() == (tmp_path / "c" / name).read_bytes()
    assert (
        json.loads((dest / "components.json").read_text())["appliance"]["version"]
        == "1.3.0"
    )
    assert github.api_requests() == []
    assert github.web_requests() and github.count("/__download/") == 3
    assert all(r.authorization is None for r in github.requests)
    assert not (
        tmp_path / "etag.json"
    ).exists()  # no list was asked, so nothing to cache


def test_a_missing_file_is_not_found_and_a_size_limit_is_enforced(github, tmp_path):
    src = source(github, tmp_path)
    release = src.candidates("stable")[0]
    with pytest.raises(rl.SourceError) as raised:
        src.fetch(
            release, "components.json.sig", tmp_path / "dl"
        )  # an unsigned release
    assert raised.value.kind == "not_found"
    with pytest.raises(rl.SourceError, match="larger than") as raised:
        src.fetch(release, "components.json", tmp_path / "dl", 10)
    assert raised.value.kind == "data"
    assert not list((tmp_path / "dl").glob(".*"))  # no half-written file is left


def test_no_stable_release_is_a_clean_404_and_asks_nobody_else(tmp_path):
    only_rc = [fake_release.make_release(tmp_path / "b", appliance="1.3.0-rc.1")]
    with FakeGitHub(only_rc) as fake:
        assert source(fake, tmp_path).candidates("stable") == []
        assert fake.api_requests() == []  # a clean 404 is the answer, no fallback


# ---- a hostile or odd redirect is refused ----


@pytest.mark.parametrize(
    "location",
    [
        "https://github.com/evil/other/releases/tag/v1.3.0",  # another repository (and host)
        "{base}/someone/else/releases/tag/v1.3.0",  # another repository
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance2/releases/tag/v1.3.0",  # a longer repository name
        "http://evil.example/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/v1.3.0",
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/v1.3.0?x=1",
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/v1.3.0#frag",
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/v1.3.0/extra",
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/v1.3.0%2F..%2Fx",
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/1.3.0",  # not a v tag
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/v1.3",  # not SemVer
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/vlatest",
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/v1.3.0-rc.1",  # latest is never a pre-release
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/releases/download/v1.3.0/x",
        "{base}/HTL3R-Xeonis/Ginnungagap-Appliance/pull/1",
        "{base}@evil.example/HTL3R-Xeonis/Ginnungagap-Appliance/releases/tag/v1.3.0",
        "",
    ],
)
def test_a_hostile_latest_redirect_is_refused(github, tmp_path, location):
    github.latest_location = location.replace("{base}", github.url)
    src = source(github, tmp_path)
    with pytest.raises(rl.SourceError) as raised:
        src.latest_stable()
    assert raised.value.kind == "data"
    # with api: never there is nothing else to ask; with auto the API answers instead (and its answer is the real one)
    with pytest.raises(rl.SourceError):
        source(github, tmp_path, api="never").candidates("stable")
    assert [r.version for r in src.candidates("stable")] == ["1.3.0", "1.2.1"]
    assert github.api_requests()
    assert all(not r.path.startswith("/evil") for r in github.requests)


def test_an_answer_without_a_redirect_is_refused(github, tmp_path):
    github.web_mode = "latest_ok"
    with pytest.raises(rl.SourceError, match="did not answer") as raised:
        source(github, tmp_path).latest_stable()
    assert raised.value.kind == "data"


def test_the_repositorys_own_spelling_is_accepted(github, tmp_path):
    src = source(github, tmp_path)
    # the repository's own spelling is accepted (GitHub answers with the canonical case)
    github.latest_location = f"{github.url}/{REPO.lower()}/releases/tag/v1.2.1"
    assert src.latest_stable().tag == "v1.2.1"


def test_the_location_is_checked_by_a_plain_function_too():
    url = "https://github.com/o/r/releases/latest"
    ok = "https://github.com/o/r/releases/tag/v2.0.0-rc.1"
    assert rl.latest_tag_from_location(ok, url, "o/r") == "v2.0.0-rc.1"
    assert (
        rl.latest_tag_from_location("/o/r/releases/tag/v2.0.0", url, "o/r") == "v2.0.0"
    )
    for bad in (
        None,
        "",
        "https://github.com/o/r/releases/tag/v1.0.0\r\nX: y",
        "https://github.com/o/R2/releases/tag/v1.0.0",
        "https://api.github.com/o/r/releases/tag/v1.0.0",
        "http://github.com/o/r/releases/tag/v1.0.0",
        "https://github.com:444/o/r/releases/tag/v1.0.0",
        "https://x@github.com/o/r/releases/tag/v1.0.0",
        "https://github.com/o/r/releases/tag/v01.0.0",
        "https://github.com/o/r/releases/tag/" + "v1.0.0-" + "a" * 5000,
    ):
        with pytest.raises(rl.SourceError):
            rl.latest_tag_from_location(bad, url, "o/r")


# ---- which mode asks whom ----


def test_a_prerelease_a_listing_and_an_older_version_use_the_api(github, tmp_path):
    src = source(github, tmp_path)
    assert [r.version for r in src.candidates("prerelease")] == [
        "1.3.0",
        "1.3.0-rc.1",
        "1.2.1",
    ]
    assert github.web_requests() == []
    assert "1.3.0-rc.1" not in [
        r.version for r in src.candidates("stable", "1.3.0-rc.1")
    ]
    assert (
        github.web_requests() == []
    )  # a pre-release is asked for: not the web's business
    assert [r.version for r in src.list_releases("stable")] == ["1.3.0", "1.2.1"]
    assert github.web_requests() == []
    # an older stable version: the latest is looked up first (web), the API knows the rest
    found = src.candidates("stable", "1.2.1")
    assert "1.2.1" in [r.version for r in found] and not any(r.web for r in found)
    # the latest itself is asked for: the web answer is enough
    github.requests.clear()
    assert [r.version for r in src.candidates("stable", "1.3.0")] == ["1.3.0"]
    assert github.api_requests() == []


def test_a_token_means_the_api(github, tmp_path):
    src = source(github, tmp_path, token=TOKEN)
    found = src.candidates("stable")
    assert [r.version for r in found] == ["1.3.0", "1.2.1"] and not found[0].web
    assert github.web_requests() == []
    assert all(r.authorization == f"Bearer {TOKEN}" for r in github.api_requests())
    src.fetch(found[0], "components.json", tmp_path / "dl")
    assert github.web_requests() == []


def test_a_rate_limited_api_falls_back_to_the_web_for_the_latest_stable_release(
    github, tmp_path
):
    github.mode = "rate_limit"
    src = source(github, tmp_path, token=TOKEN)  # a token: the API goes first
    found = src.candidates("stable")
    assert [r.version for r in found] == ["1.3.0"] and found[0].web
    path = src.fetch(found[0], "components.json", tmp_path / "dl")
    assert json.loads(path.read_text())["appliance"]["version"] == "1.3.0"
    assert all(
        r.authorization is None for r in github.web_requests()
    )  # the token never goes to the web side
    for mode in ("secondary_limit", "too_many"):
        github.mode = mode
        assert source(github, tmp_path, token=TOKEN).candidates("stable")[0].web


def test_the_rate_limit_message_stays_where_the_web_cannot_help(github, tmp_path):
    github.mode = "rate_limit"
    src = source(github, tmp_path, token=TOKEN)
    for call in (
        lambda: src.candidates("prerelease"),  # pre-releases are only in the API
        lambda: src.candidates("stable", "1.2.1"),  # not the latest
        lambda: src.candidates("stable", "1.3.0-rc.1"),
        lambda: src.list_releases("stable"),  # a listing
    ):
        with pytest.raises(rl.SourceError, match="rate limit is used up") as raised:
            call()
        assert raised.value.kind == "rate_limit"
    # the web fails as well: the API's message is what is shown
    github.web_mode = "server_error"
    with pytest.raises(rl.SourceError, match="rate limit is used up"):
        src.candidates("stable")


def test_api_always_never_asks_the_web(github, tmp_path):
    src = source(github, tmp_path, api="always")
    assert [r.version for r in src.candidates("stable")] == ["1.3.0", "1.2.1"]
    assert github.web_requests() == []
    github.mode = "rate_limit"
    with pytest.raises(rl.SourceError, match="rate limit is used up"):
        src.candidates("stable")
    assert github.web_requests() == []  # not even as a fallback


def test_api_never_asks_the_api_nothing(github, tmp_path):
    src = source(github, tmp_path, api="never", token=TOKEN)
    release = src.candidates("stable")[0]
    assert release.version == "1.3.0" and release.web
    src.fetch(release, "components.json", tmp_path / "dl")
    assert github.api_requests() == []
    assert all(r.authorization is None for r in github.requests)  # not even the token
    assert src.candidates("stable", "1.3.0")[0].version == "1.3.0"
    for call in (
        lambda: src.list_releases("stable"),
        lambda: src.candidates("prerelease"),
        lambda: src.candidates("stable", "1.3.0-rc.1"),
        lambda: src.candidates("stable", "1.2.1"),
    ):
        with pytest.raises(rl.SourceError, match="update.api is never") as raised:
            call()
        assert raised.value.kind == "config"
    assert github.api_requests() == []
    github.web_mode = "server_error"
    with pytest.raises(rl.SourceError) as raised:
        src.candidates("stable")
    assert raised.value.kind == "server" and github.api_requests() == []


def test_a_web_failure_that_is_not_a_404_falls_back_to_the_api(github, tmp_path):
    for web_mode in ("server_error", "too_many"):
        github.web_mode = web_mode
        github.requests.clear()
        found = source(github, tmp_path).candidates("stable")
        assert [r.version for r in found] == ["1.3.0", "1.2.1"]
        assert github.api_requests()
    github.web_mode = "not_found"  # a clean 404 is an answer: no release, no API call
    github.requests.clear()
    assert source(github, tmp_path).candidates("stable") == []
    assert github.api_requests() == []


def test_a_failing_download_address_is_retried_through_the_api(github, tmp_path):
    src = source(github, tmp_path)
    release = src.candidates("stable")[0]
    github.web_mode = "download_error"
    path = src.fetch(release, "components.json", tmp_path / "dl")
    assert json.loads(path.read_text())["appliance"]["version"] == "1.3.0"
    assert github.api_requests()
    never = source(github, tmp_path, api="never")
    with pytest.raises(rl.SourceError) as raised:
        never.fetch(release, "components.json", tmp_path / "dl2")
    assert raised.value.kind == "server"


def test_the_mode_comes_from_the_configuration_or_the_environment(monkeypatch):
    monkeypatch.delenv(rl.API_ENV, raising=False)
    assert rl.api_mode(None) == "auto" and rl.api_mode("never") == "never"
    monkeypatch.setenv(rl.API_ENV, "Always")
    assert rl.api_mode("never") == "always"  # the environment wins
    monkeypatch.setenv(rl.API_ENV, "sometimes")
    with pytest.raises(rl.SourceError, match=rl.API_ENV):
        rl.api_mode("auto")
    monkeypatch.delenv(rl.API_ENV)
    with pytest.raises(rl.SourceError, match="update.api"):
        rl.api_mode("maybe")
    with pytest.raises(rl.SourceError):
        rl.GitHubSource(REPO, api="maybe")


# ---- through `ginnungagap update` ----


@pytest.fixture
def web_world(tmp_path, example, monkeypatch):
    world = World(tmp_path, example, monkeypatch)
    monkeypatch.setenv(
        rl.API_ENV, "auto"
    )  # the world's config says always: the web is what is tested here
    return world


def point_at(world, fake):
    world.env.api_base = fake.url
    world.env.web_base = fake.url


def test_check_and_apply_through_the_web_make_no_api_call(web_world):
    world = web_world
    release = world.release("a")
    with FakeGitHub([release]) as fake:
        point_at(world, fake)
        assert world.update("check") == up.EXIT_AVAILABLE
        assert "1.3.0" in world.text()
        assert world.update("apply") == 0
        assert fake.api_requests() == []
        assert fake.web_requests() and all(
            r.authorization is None for r in fake.requests
        )
    assert world.installed()["appliance"]["version"] == "1.3.0"
    assert not world.layout.etag_cache.exists()


def test_check_says_there_is_no_release_on_a_clean_404(web_world):
    world = web_world
    with FakeGitHub([]) as fake:
        point_at(world, fake)
        assert world.update("check") == up.EXIT_ERROR
    assert "offers no release on the stable channel" in world.text()


def test_a_rate_limited_api_still_checks_the_latest_release_and_listing_says_why_not(
    web_world,
):
    world = web_world
    rl.write_token(world.token_file, TOKEN)
    release = world.release("a")
    with FakeGitHub([release], token=TOKEN) as fake:
        # a private repository: the web side says 404, so the clear API message is all there is
        fake.mode = "rate_limit"
        point_at(world, fake)
        assert world.update("check") == up.EXIT_ERROR
        assert "rate limit is used up" in world.text()
    with FakeGitHub([release]) as fake:
        fake.mode = "rate_limit"
        point_at(world, fake)
        assert (
            world.update("check") == up.EXIT_AVAILABLE
        )  # a token, a public repo: the web side answers
        world.out.clear()
        world.err.clear()
        assert world.update("list") == up.EXIT_ERROR
        assert "rate limit is used up" in world.text()
        world.err.clear()
        assert world.update("check", channel="prerelease") == up.EXIT_ERROR
        assert "rate limit is used up" in world.text()


def test_version_flag_uses_the_web_for_the_latest_and_the_api_for_others(web_world):
    world = web_world
    old, new = (
        world.release("a", appliance="1.2.1"),
        world.release("b", appliance="1.3.0"),
    )
    with FakeGitHub([old, new]) as fake:
        point_at(world, fake)
        assert world.update("check", version="1.3.0") == up.EXIT_AVAILABLE
        assert fake.api_requests() == []
        assert fake.api_requests() == []
        world.out.clear()
        world.update("check", version="1.2.1")
        assert fake.api_requests()  # not the latest: the API lists the releases


def test_api_never_and_always_through_the_config_or_the_environment(
    web_world, monkeypatch
):
    world = web_world
    release = world.release("a")
    with FakeGitHub([release]) as fake:
        point_at(world, fake)
        monkeypatch.setenv(rl.API_ENV, "never")
        assert world.update("list") == up.EXIT_ERROR
        assert "update.api is never" in world.text()
        assert world.update("check") == up.EXIT_AVAILABLE
        monkeypatch.setenv(rl.API_ENV, "always")
        fake.requests.clear()
        assert world.update("check") == up.EXIT_AVAILABLE
        assert fake.web_requests() == [] and fake.api_requests()
        monkeypatch.delenv(rl.API_ENV)  # the config file says always
        fake.requests.clear()
        assert world.update("check") == up.EXIT_AVAILABLE
        assert fake.web_requests() == []
        monkeypatch.setenv(rl.API_ENV, "nonsense")
        world.err.clear()
        assert world.update("check") == up.EXIT_ERROR
        assert rl.API_ENV in world.text()


def test_a_tampered_tarball_from_the_web_is_still_refused(web_world):
    world = web_world
    release = world.release("a")
    tarball = release / "TopologyBuilder-1.0.0.tar.gz"
    data = bytearray(tarball.read_bytes())
    data[-1] ^= 0xFF  # same size, other content
    tarball.write_bytes(bytes(data))
    with FakeGitHub([release]) as fake:
        point_at(world, fake)
        assert world.update("apply") == up.EXIT_REFUSED
        assert fake.api_requests() == []
    assert "does not match the checksum" in world.text()
    assert not world.layout.transaction.exists()


def test_a_tampered_manifest_from_the_web_is_refused_by_the_signature(web_world):
    import shutil

    if shutil.which("ssh-keygen") is None:
        pytest.skip("no ssh-keygen")
    world = web_world
    release = world.release("a", sign=True)
    world.layout.trust_file.parent.mkdir(parents=True, exist_ok=True)
    world.layout.trust_file.write_text(fake_release.keys_of(release).read_text())
    fake_release.rewrite_manifest(
        release, lambda d: d["appliance"].update(notes="tampered")
    )
    with FakeGitHub([release]) as fake:
        point_at(world, fake)
        assert world.update("apply") == up.EXIT_REFUSED
        assert fake.api_requests() == []
    assert "signature of components.json is not valid" in world.text()


def test_a_valid_signature_is_checked_with_a_release_from_the_web(web_world):
    import shutil

    if shutil.which("ssh-keygen") is None:
        pytest.skip("no ssh-keygen")
    world = web_world
    release = world.release("a", sign=True)
    world.layout.trust_file.parent.mkdir(parents=True, exist_ok=True)
    world.layout.trust_file.write_text(fake_release.keys_of(release).read_text())
    with FakeGitHub([release]) as fake:
        point_at(world, fake)
        assert world.update("check") == up.EXIT_AVAILABLE
        assert fake.api_requests() == []
    assert "signature: valid" in world.text()
