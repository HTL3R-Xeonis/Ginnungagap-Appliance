import json
import os
import urllib.error
import urllib.request

import pytest

from ginnungagap import releases as rl
from tests.helpers import fake_release
from tests.helpers.fake_github import FakeGitHub

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


def source(github, tmp_path, token=None, cache=True, **kwargs):
    token_file = None
    if token:
        token_file = tmp_path / "update-token"
        rl.write_token(token_file, token)
    return rl.GitHubSource(
        "HTL3R-Xeonis/Ginnungagap-Appliance",
        token_file=token_file,
        api_base=github.url,
        cache_path=tmp_path / "etag.json" if cache else None,
        **kwargs,
    )


def test_it_works_anonymously_and_never_sends_an_authorization_header(github, tmp_path):
    src = source(github, tmp_path)
    found = src.list_releases("stable")
    assert [r.version for r in found] == ["1.3.0", "1.2.1"]
    dest = tmp_path / "dl"
    path = src.fetch(found[0], "components.json", dest)
    assert json.loads(path.read_text())["appliance"]["version"] == "1.3.0"
    assert github.requests and all(r.authorization is None for r in github.requests)
    assert src.describe() == "github HTL3R-Xeonis/Ginnungagap-Appliance"


def test_the_prerelease_channel_shows_everything_newest_first(github, tmp_path):
    found = source(github, tmp_path).list_releases("prerelease")
    assert [r.version for r in found] == ["1.3.0", "1.3.0-rc.1", "1.2.1"]
    assert [r.prerelease for r in found] == [False, True, False]
    assert found[0].tag == "v1.3.0" and found[0].date == "2026-12-01T10:00:00Z"


def test_drafts_and_odd_tags_are_not_offered(github, folders, tmp_path):
    github.drafts.append(folders[2])
    assert [r.version for r in source(github, tmp_path).list_releases("stable")] == [
        "1.2.1"
    ]


def test_the_release_list_is_cached_by_etag(github, tmp_path):
    src = source(github, tmp_path)
    first = src.list_releases("stable")
    again = rl.GitHubSource(
        "HTL3R-Xeonis/Ginnungagap-Appliance",
        api_base=github.url,
        cache_path=tmp_path / "etag.json",
    )
    second = again.list_releases("stable")
    assert [r.version for r in first] == [r.version for r in second]
    listings = [
        r
        for r in github.requests
        if r.path.startswith("/repos/") and r.path.endswith("per_page=100")
    ]
    assert "if-none-match" not in listings[0].headers
    assert listings[1].headers["if-none-match"].startswith('"')
    assert oct((tmp_path / "etag.json").stat().st_mode & 0o777) == "0o600"


def test_a_304_without_a_stored_copy_is_an_error_not_a_crash(github, tmp_path):
    cache = tmp_path / "etag.json"
    src = source(github, tmp_path)
    src.list_releases("stable")
    data = json.loads(cache.read_text())
    key = next(iter(data))
    data[key]["body"] = (
        5  # damaged: no usable stored copy, so no If-None-Match is sent and the full list comes
    )
    cache.write_text(json.dumps(data))
    assert [r.version for r in source(github, tmp_path).list_releases("stable")] == [
        "1.3.0",
        "1.2.1",
    ]


def test_a_token_goes_to_the_api_but_never_to_the_download_address(github, tmp_path):
    src = source(github, tmp_path, token=TOKEN)
    release = src.list_releases("stable")[0]
    path = src.fetch(release, "SHA256SUMS", tmp_path / "dl")
    assert path.read_text().startswith(
        ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "c", "d", "e", "f")
    )
    api = [r for r in github.requests if not r.path.startswith("/__download/")]
    download = [r for r in github.requests if r.path.startswith("/__download/")]
    assert api and all(r.authorization == f"Bearer {TOKEN}" for r in api)
    assert download and all(r.authorization is None for r in download)


def test_the_redirect_handler_drops_credentials_and_refuses_https_to_http():
    handler = rl._SafeRedirect()
    request = urllib.request.Request(
        "https://api.example/x", headers={"Authorization": "Bearer t"}
    )
    request.add_unredirected_header("Authorization", "Bearer t")
    new = handler.redirect_request(
        request, None, 302, "Found", {}, "https://store.example/y"
    )
    assert new.get_header("Authorization") is None and not new.unredirected_hdrs.get(
        "Authorization"
    )
    with pytest.raises(urllib.error.HTTPError, match="plain http"):
        handler.redirect_request(
            request, None, 302, "Found", {}, "http://store.example/y"
        )


def test_a_private_repository_needs_the_token(folders, tmp_path):
    with FakeGitHub(folders, token=TOKEN) as fake:
        with pytest.raises(rl.SourceError) as caught:
            source(fake, tmp_path).list_releases("stable")
        assert (
            caught.value.kind == "not_found" and "--set-token" in caught.value.message
        )
        assert [
            r.version
            for r in source(fake, tmp_path, token=TOKEN).list_releases("stable")
        ] == ["1.3.0", "1.2.1"]


def test_a_wrong_or_expired_token_is_named_plainly_and_not_printed(folders, tmp_path):
    with (
        FakeGitHub(folders, token="ghp_" + "z" * 36) as fake,
        pytest.raises(rl.SourceError) as caught,
    ):
        source(fake, tmp_path, token=TOKEN).list_releases("stable")
    assert caught.value.kind == "auth"
    assert (
        "invalid or has expired" in caught.value.message
        and "--set-token" in caught.value.message
    )
    assert TOKEN not in caught.value.message


@pytest.mark.parametrize(
    "mode, kind",
    [("unauthorized", "auth"), ("not_found", "not_found"), ("server_error", "server")],
)
def test_http_failures_are_sorted_into_kinds(github, tmp_path, mode, kind):
    github.mode = mode
    with pytest.raises(rl.SourceError) as caught:
        source(github, tmp_path).list_releases("stable")
    assert caught.value.kind == kind and caught.value.message


def test_the_anonymous_rate_limit_is_explained(github, tmp_path):
    github.mode = "rate_limit"
    with pytest.raises(rl.SourceError) as caught:
        source(github, tmp_path).list_releases("stable")
    message = caught.value.message
    assert caught.value.kind == "rate_limit"
    assert (
        "60 per hour" in message
        and "Try again after" in message
        and "UTC" in message
        and "--set-token" in message
    )
    assert len(github.requests) == 1  # no retry loop


def test_the_rate_limit_with_a_token_does_not_advertise_a_token(github, tmp_path):
    github.mode = "rate_limit"
    with pytest.raises(rl.SourceError) as caught:
        source(github, tmp_path, token=TOKEN).list_releases("stable")
    assert caught.value.kind == "rate_limit"
    assert (
        "stored token" in caught.value.message
        and "--set-token" not in caught.value.message
    )
    assert TOKEN not in caught.value.message


@pytest.mark.parametrize("mode", ["secondary_limit", "too_many"])
def test_secondary_limits_use_retry_after(github, tmp_path, mode):
    github.mode = mode
    with pytest.raises(rl.SourceError) as caught:
        source(github, tmp_path).list_releases("stable")
    assert (
        caught.value.kind == "rate_limit" and "Try again after" in caught.value.message
    )


def test_no_network_is_a_plain_message(tmp_path):
    with FakeGitHub([]) as fake:
        url = fake.url
    src = rl.GitHubSource("o/r", api_base=url)
    with pytest.raises(rl.SourceError) as caught:
        src.list_releases("stable")
    assert (
        caught.value.kind == "network"
        and "could not reach 127.0.0.1" in caught.value.message
    )
    assert "--offline-dir" in caught.value.message


def test_an_asset_that_is_not_there_and_unsafe_names(github, tmp_path):
    src = source(github, tmp_path)
    release = src.list_releases("stable")[0]
    with pytest.raises(rl.SourceError, match="has no file nope.txt"):
        src.fetch(release, "nope.txt", tmp_path / "dl")
    for bad in ("../x", "/etc/passwd", "a/b", ".hidden", ""):
        with pytest.raises(rl.SourceError, match="not a plain file name"):
            src.fetch(release, bad, tmp_path / "dl")


def test_a_download_is_cut_off_at_the_size_the_manifest_gave(github, tmp_path):
    src = source(github, tmp_path)
    release = src.list_releases("stable")[0]
    with pytest.raises(rl.SourceError, match="larger than the 10 bytes"):
        src.fetch(
            release, "Ginnungagap-Appliance-1.3.0.tar.gz", tmp_path / "dl", max_bytes=10
        )
    assert list((tmp_path / "dl").iterdir()) == []  # no half file left


def test_a_failure_in_the_middle_of_a_download_leaves_nothing(github, tmp_path):
    class Broken:
        def read(self, n=-1):
            raise TimeoutError("timed out")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Opener:
        def open(self, request, timeout=None):
            return Broken()

    src = rl.GitHubSource("o/r", opener=Opener())
    release = rl.ReleaseInfo(
        "1.0.0", "v1.0.0", False, "", assets={"a.tar.gz": "http://x/a"}
    )
    with pytest.raises(rl.SourceError) as caught:
        src.fetch(release, "a.tar.gz", tmp_path / "dl")
    assert caught.value.kind == "network"
    assert list((tmp_path / "dl").iterdir()) == []


def test_a_redirect_downgrade_is_refused_end_to_end(tmp_path):
    class Opener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 302, "refusing a redirect", {}, None
            )

    src = rl.GitHubSource("o/r", opener=Opener())
    with pytest.raises(rl.SourceError):
        src.list_releases("stable")


# ---- the token file ----


def test_no_token_file_means_anonymous(tmp_path):
    assert rl.read_token(tmp_path / "none") is None
    assert rl.read_token(None) is None and rl.read_token("") is None


def test_a_token_file_is_written_private_and_read_back(tmp_path):
    path = tmp_path / "etc" / "update-token"
    rl.write_token(path, TOKEN)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert rl.read_token(path) == TOKEN
    rl.write_token(path, TOKEN + "x")  # replaced
    assert rl.read_token(path) == TOKEN + "x"
    assert [p.name for p in path.parent.iterdir()] == ["update-token"]


def test_a_token_file_readable_by_others_is_refused(tmp_path):
    path = tmp_path / "update-token"
    path.write_text(TOKEN)
    path.chmod(0o644)
    with pytest.raises(rl.SourceError, match="readable by other users") as caught:
        rl.read_token(path)
    assert TOKEN not in caught.value.message
    # the source refuses to go on rather than quietly going without
    with pytest.raises(rl.SourceError, match="readable by other users"):
        rl.GitHubSource("o/r", token_file=path, opener=object()).list_releases("stable")


def test_a_symlinked_empty_or_odd_token_file_is_refused(tmp_path):
    real = tmp_path / "real"
    real.write_text(TOKEN)
    real.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(rl.SourceError, match="not a plain file"):
        rl.read_token(link)
    empty = tmp_path / "empty"
    empty.write_text("\n")
    empty.chmod(0o600)
    with pytest.raises(rl.SourceError, match="is empty"):
        rl.read_token(empty)
    odd = tmp_path / "odd"
    odd.write_text("two words\n")
    odd.chmod(0o600)
    with pytest.raises(rl.SourceError, match="single token"):
        rl.read_token(odd)
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(rl.SourceError, match="not a plain file"):
        rl.read_token(folder)


def test_token_text_is_checked_without_quoting_it():
    assert rl.check_token_text(f"  {TOKEN}\n") == TOKEN
    for bad in ("", "  ", "two words", "short", "x" * 300, "a\nb"):
        with pytest.raises(ValueError) as caught:
            rl.check_token_text(bad)
        assert bad.strip() == "" or bad.strip() not in str(caught.value)


def test_redact_hides_tokens():
    assert rl.redact(f"x {TOKEN} y", TOKEN) == "x *** y"
    assert "github_pat_" not in rl.redact("github_pat_" + "A" * 40)
    assert rl.redact("plain") == "plain"


# ---- a folder ----


def test_a_folder_with_one_release(folders, tmp_path):
    src = rl.DirectorySource(folders[2])
    found = src.list_releases("stable")
    assert [r.version for r in found] == ["1.3.0"] and found[
        0
    ].date == "2026-12-01T10:00:00Z"
    path = src.fetch(found[0], "components.json", tmp_path / "dl")
    assert path.read_bytes() == (folders[2] / "components.json").read_bytes()
    assert src.describe() == f"directory {folders[2]}"


def test_a_folder_of_releases_lists_each_subfolder(folders, tmp_path):
    src = rl.DirectorySource(tmp_path)
    assert [r.version for r in src.list_releases("prerelease")] == [
        "1.3.0",
        "1.3.0-rc.1",
        "1.2.1",
    ]
    assert [r.version for r in src.list_releases("stable")] == ["1.3.0", "1.2.1"]
    old = src.list_releases("stable")[1]
    path = src.fetch(old, "components.json", tmp_path / "dl")
    assert json.loads(path.read_text())["appliance"]["version"] == "1.2.1"


def test_a_folder_that_is_not_a_release(tmp_path):
    with pytest.raises(rl.SourceError, match="is not a folder"):
        rl.DirectorySource(tmp_path / "nothing").list_releases("stable")
    (tmp_path / "empty").mkdir()
    with pytest.raises(rl.SourceError, match="holds no release"):
        rl.DirectorySource(tmp_path / "empty").list_releases("stable")


def test_a_missing_file_oversized_file_and_odd_names_in_a_folder(folders, tmp_path):
    src = rl.DirectorySource(folders[2])
    release = src.list_releases("stable")[0]
    (folders[2] / "TopologyOperator-1.2.1.tar.gz").unlink()
    with pytest.raises(
        rl.SourceError, match="has no file TopologyOperator-1.2.1.tar.gz"
    ):
        src.fetch(release, "TopologyOperator-1.2.1.tar.gz", tmp_path / "dl")
    with pytest.raises(rl.SourceError, match="larger than the 5 bytes"):
        src.fetch(release, "components.json", tmp_path / "dl", max_bytes=5)
    with pytest.raises(rl.SourceError, match="not a plain file name"):
        src.fetch(release, "../a/components.json", tmp_path / "dl")
    assert not list((tmp_path / "dl").glob(".*"))


def test_a_folder_release_with_a_broken_manifest_is_skipped(tmp_path):
    good = fake_release.make_release(tmp_path / "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "components.json").write_text("{nope")
    versions = [r.version for r in rl.DirectorySource(tmp_path).list_releases("stable")]
    assert versions == ["1.3.0"] and good.exists()
    assert os.path.isdir(bad)
