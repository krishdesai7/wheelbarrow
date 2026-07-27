"""Fetching release assets: what gets picked, what gets trusted, what lands.

Every test routes through `fetch._open`, the single choke point for HTTP, and
the `fake_http` fixture replaces it. Nothing here may reach the network.
"""

import email.message
import hashlib
import io
import json
import re
import tarfile
import urllib.error
import urllib.request
import zipfile
from typing import TYPE_CHECKING, Any, Final

import pytest

from wheelforge import discover, fetch
from wheelforge.errors import FetchError
from wheelforge.fetch import Asset, Release

if TYPE_CHECKING:
    from pathlib import Path

PAYLOAD: Final[bytes] = b"#!/bin/sh\nexec echo hello\n"
#: sha256 of PAYLOAD, computed by the test rather than asserted from the code.
DIGEST: Final[str] = hashlib.sha256(PAYLOAD).hexdigest()


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def asset(name: str, *, digest: str | None = None, size: int = len(PAYLOAD)) -> Asset:
    return Asset(
        name=name, url=f"https://example.test/{name}", size=size, digest=digest
    )


def release(*assets: Asset, tag: str = "v1.0.0") -> Release:
    return Release(owner="acme", repo="tool", tag=tag, assets=assets)


def make_tarball(path: Path, members: dict[str, tuple[bytes, int]]) -> Path:
    """Write a gzipped tar. Member names are used verbatim, traversal included."""
    with tarfile.open(path, "w:gz") as tar:
        for name, (data, mode) in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return path


def make_zip(path: Path, members: dict[str, tuple[bytes, int]]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, (data, mode) in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            zf.writestr(info, data)
    return path


# --------------------------------------------------------------------------
# Naming the release
# --------------------------------------------------------------------------


class TestParseSource:
    """Everything a user might reasonably paste as a release reference."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("starship/starship", ("starship", "starship", None)),
            (
                "https://github.com/starship/starship/releases/tag/v1.26.0",
                ("starship", "starship", "v1.26.0"),
            ),
            (
                "github.com/starship/starship/releases/tag/v1.26.0",
                ("starship", "starship", "v1.26.0"),
            ),
            (
                "https://github.com/starship/starship/releases/latest",
                ("starship", "starship", None),
            ),
            (
                "https://github.com/starship/starship",
                ("starship", "starship", None),
            ),
            (
                "https://github.com/starship/starship.git",
                ("starship", "starship", None),
            ),
        ],
    )
    def test_recognised_forms(self, source, expected) -> None:
        assert fetch.parse_source(source) == expected

    def test_an_asset_url_yields_the_tag_not_the_asset(self) -> None:
        """People paste the download link; the tag sits between it and the marker."""
        assert fetch.parse_source(
            "https://github.com/cli/cli/releases/download/v2.40.0/gh_linux.tar.gz"
        ) == ("cli", "cli", "v2.40.0")

    def test_a_tag_containing_slashes_survives(self) -> None:
        """Git tags may contain `/`, so the tag is everything after the marker."""
        assert fetch.parse_source(
            "https://github.com/acme/tool/releases/tag/release/2024/01"
        ) == ("acme", "tool", "release/2024/01")

    def test_an_api_url_is_understood_too(self) -> None:
        assert fetch.parse_source(
            "https://api.github.com/repos/acme/tool/releases/tags/v1.0.0"
        ) == ("acme", "tool", "v1.0.0")

    def test_trailing_slash_is_ignored(self) -> None:
        assert fetch.parse_source("https://github.com/acme/tool/") == (
            "acme",
            "tool",
            None,
        )

    @pytest.mark.parametrize("source", ["", "   ", "https://github.com/acme"])
    def test_something_that_names_no_repository_is_refused(self, source) -> None:
        with pytest.raises(FetchError):
            fetch.parse_source(source)


# --------------------------------------------------------------------------
# Reading the release
# --------------------------------------------------------------------------


class TestGetRelease:
    def api_url(self, tag: str | None = "v1.0.0") -> str:
        base = "https://api.github.com/repos/acme/tool/releases"
        return f"{base}/tags/{tag}" if tag else f"{base}/latest"

    def payload(self, **overrides: Any) -> bytes:
        body: dict[str, Any] = {
            "tag_name": "v1.0.0",
            "assets": [
                {
                    "name": "tool-linux.tar.gz",
                    "browser_download_url": "https://example.test/tool-linux.tar.gz",
                    "size": 42,
                    "digest": f"sha256:{DIGEST}",
                }
            ],
        }
        body.update(overrides)
        return json.dumps(body).encode()

    def test_assets_and_digests_are_read(self, fake_http) -> None:
        fake_http[self.api_url()] = self.payload()
        result = fetch.get_release("acme", "tool", "v1.0.0")
        assert result.tag == "v1.0.0"
        assert result.assets[0].name == "tool-linux.tar.gz"
        assert result.assets[0].digest == DIGEST

    def test_a_missing_tag_asks_for_the_latest_release(self, fake_http) -> None:
        fake_http[self.api_url(None)] = self.payload(tag_name="v9.9.9")
        assert fetch.get_release("acme", "tool").tag == "v9.9.9"

    def test_a_digest_of_another_algorithm_is_ignored(self, fake_http) -> None:
        """Only sha256 is verified here, so anything else must read as absent."""
        fake_http[self.api_url()] = self.payload(
            assets=[
                {
                    "name": "tool.tar.gz",
                    "browser_download_url": "https://example.test/tool.tar.gz",
                    "size": 1,
                    "digest": "sha512:" + "a" * 128,
                }
            ]
        )
        assert fetch.get_release("acme", "tool", "v1.0.0").assets[0].digest is None

    def test_a_malformed_digest_is_ignored(self, fake_http) -> None:
        fake_http[self.api_url()] = self.payload(
            assets=[
                {
                    "name": "tool.tar.gz",
                    "browser_download_url": "https://example.test/tool.tar.gz",
                    "size": 1,
                    "digest": "sha256:not-hex",
                }
            ]
        )
        assert fetch.get_release("acme", "tool", "v1.0.0").assets[0].digest is None

    @pytest.mark.usefixtures("fake_http")
    def test_an_unknown_tag_mentions_private_repositories(self) -> None:
        """No route registered, so the fake answers 404 as GitHub would."""
        with pytest.raises(FetchError, match="GH_TOKEN"):
            fetch.get_release("acme", "tool", "v1.0.0")

    def test_an_exhausted_rate_limit_says_so(self, fake_http) -> None:
        headers = email.message.Message()
        headers["X-RateLimit-Remaining"] = "0"
        fake_http[self.api_url()] = urllib.error.HTTPError(
            self.api_url(), 403, "rate limit exceeded", headers, None
        )
        with pytest.raises(FetchError, match="rate limit"):
            fetch.get_release("acme", "tool", "v1.0.0")

    def test_a_forbidden_response_is_not_read_as_a_rate_limit(self, fake_http) -> None:
        fake_http[self.api_url()] = urllib.error.HTTPError(
            self.api_url(), 403, "Forbidden", email.message.Message(), None
        )
        with pytest.raises(FetchError, match="expired"):
            fetch.get_release("acme", "tool", "v1.0.0")

    def test_being_offline_is_reported_as_such(self, fake_http) -> None:
        fake_http[self.api_url()] = OSError("Name or service not known")
        with pytest.raises(FetchError, match="could not reach GitHub"):
            fetch.get_release("acme", "tool", "v1.0.0")


# --------------------------------------------------------------------------
# Choosing assets
# --------------------------------------------------------------------------


class TestSelectAssets:
    @pytest.fixture
    def full(self) -> Release:
        return release(
            asset("tool-x86_64-linux.tar.gz"),
            asset("tool-x86_64-linux.tar.gz.sha256"),
            asset("tool-aarch64-darwin.tar.gz"),
            asset("tool-aarch64-darwin.tar.gz.sha256"),
            asset("tool-windows.zip"),
        )

    def test_no_pattern_takes_every_payload_asset(self, full) -> None:
        assert [a.name for a in fetch.select_assets(full, [])] == [
            "tool-x86_64-linux.tar.gz",
            "tool-aarch64-darwin.tar.gz",
            "tool-windows.zip",
        ]

    def test_checksum_files_are_never_payload(self, full) -> None:
        """A `*.tar.gz*` pattern sweeps up sidecars; they must not be downloaded."""
        chosen = fetch.select_assets(full, ["*.tar.gz*"])
        assert [a.name for a in chosen] == [
            "tool-x86_64-linux.tar.gz",
            "tool-aarch64-darwin.tar.gz",
        ]

    def test_patterns_accumulate_without_duplicating(self, full) -> None:
        chosen = fetch.select_assets(full, ["*linux*", "*.tar.gz"])
        assert [a.name for a in chosen] == [
            "tool-x86_64-linux.tar.gz",
            "tool-aarch64-darwin.tar.gz",
        ]

    def test_release_order_is_preserved(self, full) -> None:
        """Patterns given back to front still yield the release's own order."""
        chosen = fetch.select_assets(full, ["*windows*", "*linux*"])
        assert [a.name for a in chosen] == [
            "tool-x86_64-linux.tar.gz",
            "tool-windows.zip",
        ]

    def test_a_pattern_matching_nothing_is_an_error(self, full) -> None:
        """Silently downloading fewer files than asked for is the worse failure."""
        with pytest.raises(FetchError, match="no asset"):
            fetch.select_assets(full, ["*-freebsd.tar.gz"])

    def test_the_error_lists_what_was_available(self, full) -> None:
        with pytest.raises(FetchError, match=re.escape("tool-windows.zip")):
            fetch.select_assets(full, ["*-freebsd.tar.gz"])

    def test_a_pattern_matching_only_checksums_explains_itself(self, full) -> None:
        with pytest.raises(FetchError, match="only checksum files"):
            fetch.select_assets(full, ["*.sha256"])


class TestUnpackableAssetsAreNotPayload:
    """Assets nothing downstream can open are filtered out like checksums are.

    Listing an installer and then declining it at build time sends the user
    off to work out why a file the release plainly publishes never appeared.
    """

    @pytest.fixture
    def mixed(self) -> Release:
        return release(
            asset("tool-windows.zip"),
            asset("tool-windows.msi"),
            asset("tool-linux.tar.gz"),
            asset("tool-linux.deb"),
            asset("tool-linux.rpm"),
            asset("tool.tar.gz.sig"),
            asset("README.md"),
            asset("tool.sbom.json"),
        )

    def test_installers_are_left_out_of_the_default_set(self, mixed) -> None:
        assert [a.name for a in fetch.select_assets(mixed, [])] == [
            "tool-windows.zip",
            "tool-linux.tar.gz",
        ]

    def test_a_pattern_cannot_drag_an_installer_back_in(self, mixed) -> None:
        """`-p '*windows*'` means the zip, not the .msi sitting beside it."""
        chosen = fetch.select_assets(mixed, ["*windows*"])
        assert [a.name for a in chosen] == ["tool-windows.zip"]

    def test_a_pattern_matching_only_installers_says_why(self, mixed) -> None:
        with pytest.raises(FetchError, match="cannot unpack"):
            fetch.select_assets(mixed, ["*.msi"])

    def test_a_pattern_matching_only_signatures_says_why(self, mixed) -> None:
        with pytest.raises(FetchError, match="signatures and attestations"):
            fetch.select_assets(mixed, ["*.sig"])

    def test_an_extensionless_asset_is_still_payload(self) -> None:
        """A release shipping the bare executable is the case a whitelist breaks."""
        bare = release(asset("tool-linux-amd64"), asset("tool.msi"))
        assert [a.name for a in fetch.select_assets(bare, [])] == ["tool-linux-amd64"]

    @pytest.mark.parametrize(
        "name",
        ["tool.MSI", "tool.Deb", "TOOL.PKG"],
    )
    def test_the_check_is_case_insensitive(self, name) -> None:
        assert not fetch.is_payload_asset(name)

    def test_a_reason_is_given_for_every_exclusion(self, mixed) -> None:
        """`--list` prints these, so none may come back as None or empty."""
        excluded = [
            a.name
            for a in mixed.assets
            if not fetch.is_payload_asset(a.name)
            and not fetch.is_checksum_asset(a.name)
        ]
        assert excluded  # guard against the fixture drifting to all-payload
        for name in excluded:
            assert fetch.unpackable_reason(name)


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------


class TestParseChecksums:
    def test_a_sha256sums_row_is_found_by_name(self) -> None:
        text = f"{DIGEST}  tool-linux.tar.gz\n{'b' * 64}  other.tar.gz\n"
        assert fetch.parse_checksums(text, want="tool-linux.tar.gz") == DIGEST

    def test_the_binary_mode_marker_is_stripped(self) -> None:
        """GNU coreutils writes `*name` for a file checksummed in binary mode."""
        assert fetch.parse_checksums(f"{DIGEST} *tool.tar.gz", want="tool.tar.gz") == (
            DIGEST
        )

    def test_a_recorded_directory_is_ignored(self) -> None:
        text = f"{DIGEST}  ./dist/tool.tar.gz"
        assert fetch.parse_checksums(text, want="tool.tar.gz") == DIGEST

    def test_an_uppercase_digest_is_normalised(self) -> None:
        text = f"{DIGEST.upper()}  tool.tar.gz"
        assert fetch.parse_checksums(text, want="tool.tar.gz") == DIGEST

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        text = f"# generated\n\n{DIGEST}  tool.tar.gz\n"
        assert fetch.parse_checksums(text, want="tool.tar.gz") == DIGEST

    def test_a_name_that_is_not_listed_yields_nothing(self) -> None:
        text = f"{DIGEST}  other.tar.gz"
        assert fetch.parse_checksums(text, want="tool.tar.gz") is None

    def test_a_bare_digest_needs_permission(self) -> None:
        """A lone digest says nothing about what it covers."""
        assert fetch.parse_checksums(DIGEST, want="tool.tar.gz") is None
        assert (
            fetch.parse_checksums(f"{DIGEST}\n", want="tool.tar.gz", allow_bare=True)
            == DIGEST
        )

    def test_something_that_is_not_a_digest_is_ignored(self) -> None:
        assert fetch.parse_checksums("hello world", want="tool.tar.gz") is None


class TestFindDigest:
    def test_the_api_digest_wins(self) -> None:
        """Server-computed, so it is not merely another publisher-supplied file."""
        payload = asset("tool.tar.gz", digest=DIGEST)
        found = fetch.find_digest(
            release(payload), payload, read_text=lambda _a: pytest.fail("no fetch")
        )
        assert found is not None
        assert found.digest == DIGEST
        assert found.source == "the GitHub API"

    def test_a_bare_sidecar_is_the_first_fallback(self) -> None:
        payload = asset("tool.tar.gz")
        sidecar = asset("tool.tar.gz.sha256")
        found = fetch.find_digest(
            release(payload, sidecar), payload, read_text=lambda _a: f"{DIGEST}\n"
        )
        assert found is not None
        assert found.digest == DIGEST
        assert found.source == "tool.tar.gz.sha256"

    def test_a_release_wide_manifest_is_the_second(self) -> None:
        payload = asset("tool.tar.gz")
        manifest = asset("SHA256SUMS")
        found = fetch.find_digest(
            release(payload, manifest),
            payload,
            read_text=lambda _a: f"{DIGEST}  tool.tar.gz\n",
        )
        assert found is not None
        assert found.source == "SHA256SUMS"

    def test_a_manifest_not_listing_the_asset_yields_nothing(self) -> None:
        payload = asset("tool.tar.gz")
        found = fetch.find_digest(
            release(payload, asset("SHA256SUMS")),
            payload,
            read_text=lambda _a: f"{DIGEST}  something-else.tar.gz\n",
        )
        assert found is None

    def test_a_release_with_no_checksums_at_all_yields_nothing(self) -> None:
        payload = asset("tool.tar.gz")
        assert fetch.find_digest(release(payload), payload) is None

    def test_an_unreachable_checksum_file_is_not_fatal_here(self) -> None:
        """The caller decides what unverifiable means; this only reports it."""
        payload = asset("tool.tar.gz")
        found = fetch.find_digest(
            release(payload, asset("tool.tar.gz.sha256")),
            payload,
            read_text=lambda _a: None,
        )
        assert found is None


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------


class TestDownloadAsset:
    def test_a_matching_digest_writes_the_file(self, fake_http, tmp_path) -> None:
        item = asset("tool.tar.gz")
        fake_http[item.url] = PAYLOAD
        path, digest = fetch.download_asset(item, tmp_path, expected=DIGEST)
        assert path.read_bytes() == PAYLOAD
        assert digest == DIGEST

    def test_a_mismatch_raises_and_leaves_nothing_behind(
        self, fake_http, tmp_path
    ) -> None:
        """Bytes that failed verification must never sit where `build` looks."""
        item = asset("tool.tar.gz")
        fake_http[item.url] = b"something else entirely"
        with pytest.raises(FetchError, match="does not match"):
            fetch.download_asset(item, tmp_path, expected=DIGEST)
        assert list(tmp_path.iterdir()) == []

    def test_a_failed_transfer_leaves_no_partial_file(
        self, fake_http, tmp_path
    ) -> None:
        item = asset("tool.tar.gz")
        fake_http[item.url] = OSError("connection reset")
        with pytest.raises(FetchError, match="could not download"):
            fetch.download_asset(item, tmp_path, expected=DIGEST)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.usefixtures("fake_http")
    def test_an_already_correct_file_is_not_downloaded_again(self, tmp_path) -> None:
        """Re-running over a populated directory has to be cheap."""
        item = asset("tool.tar.gz")
        (tmp_path / item.name).write_bytes(PAYLOAD)
        # No route registered: reaching the network at all would 404.
        path, digest = fetch.download_asset(item, tmp_path, expected=DIGEST)
        assert digest == DIGEST
        assert path.read_bytes() == PAYLOAD

    def test_a_stale_file_of_the_same_name_is_replaced(
        self, fake_http, tmp_path
    ) -> None:
        item = asset("tool.tar.gz")
        (tmp_path / item.name).write_bytes(b"an older version")
        fake_http[item.url] = PAYLOAD
        path, _ = fetch.download_asset(item, tmp_path, expected=DIGEST)
        assert path.read_bytes() == PAYLOAD

    def test_the_digest_is_reported_even_when_unchecked(
        self, fake_http, tmp_path
    ) -> None:
        item = asset("tool.tar.gz")
        fake_http[item.url] = PAYLOAD
        _, digest = fetch.download_asset(item, tmp_path, expected=None)
        assert digest == DIGEST

    def test_progress_is_reported_by_the_byte(self, fake_http, tmp_path) -> None:
        item = asset("tool.tar.gz")
        fake_http[item.url] = PAYLOAD
        seen: list[int] = []
        fetch.download_asset(item, tmp_path, expected=DIGEST, on_chunk=seen.append)
        assert sum(seen) == len(PAYLOAD)


class TestRedirectHandler:
    """A GitHub token must not follow a redirect to object storage."""

    def redirect_to(self, newurl: str) -> urllib.request.Request | None:
        handler = fetch._StripAuthOnRedirect()
        request = urllib.request.Request(
            "https://api.github.com/asset",
            headers={"Authorization": "Bearer secret-token"},
        )
        return handler.redirect_request(
            request, None, 302, "Found", email.message.Message(), newurl
        )

    def has_auth(self, request: urllib.request.Request | None) -> bool:
        assert request is not None
        return any(key.lower() == "authorization" for key in request.headers)

    def test_a_cross_host_redirect_drops_the_credential(self) -> None:
        assert not self.has_auth(
            self.redirect_to("https://objects.githubusercontent.com/blob")
        )

    def test_a_same_host_redirect_keeps_it(self) -> None:
        assert self.has_auth(self.redirect_to("https://api.github.com/elsewhere"))


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


class TestExtract:
    def test_a_tarball_keeps_the_executable_bit(self, tmp_path) -> None:
        """The one permission that has to survive, or the binary is unrunnable."""
        archive = make_tarball(tmp_path / "tool.tar.gz", {"tool": (PAYLOAD, 0o755)})
        written = fetch.extract(archive, tmp_path / "out")
        assert [p.name for p in written] == ["tool"]
        assert written[0].stat().st_mode & 0o111

    def test_a_plain_file_stays_unexecutable(self, tmp_path) -> None:
        archive = make_tarball(tmp_path / "tool.tar.gz", {"README": (b"hello", 0o644)})
        written = fetch.extract(archive, tmp_path / "out")
        assert not written[0].stat().st_mode & 0o111

    def test_a_traversing_member_is_refused(self, tmp_path) -> None:
        archive = make_tarball(
            tmp_path / "evil.tar.gz", {"../escaped": (b"owned", 0o644)}
        )
        with pytest.raises(FetchError, match="outside"):
            fetch.extract(archive, tmp_path / "out")
        assert not (tmp_path / "escaped").exists()

    def test_an_absolute_member_is_defanged_rather_than_refused(self, tmp_path) -> None:
        """tarfile strips the leading separator, as GNU tar does, so it stays in."""
        archive = make_tarball(
            tmp_path / "odd.tar.gz", {"/absolute/owned": (b"owned", 0o644)}
        )
        written = fetch.extract(archive, tmp_path / "out")
        assert written == [tmp_path / "out" / "absolute" / "owned"]
        assert written[0].read_bytes() == b"owned"

    def test_a_corrupt_archive_is_reported_not_raised_raw(self, tmp_path) -> None:
        archive = tmp_path / "tool.tar.gz"
        archive.write_bytes(b"not a tarball at all")
        with pytest.raises(FetchError, match="could not unpack"):
            fetch.extract(archive, tmp_path / "out")

    def test_a_zip_regains_the_executable_bit(self, tmp_path) -> None:
        """`zipfile` discards the stored mode, which would leave it unrunnable."""
        archive = make_zip(tmp_path / "tool.zip", {"tool.exe": (PAYLOAD, 0o755)})
        written = fetch.extract(archive, tmp_path / "out")
        assert written[0].stat().st_mode & 0o111

    def test_a_zip_entry_stored_without_a_mode_is_left_alone(self, tmp_path) -> None:
        archive = make_zip(tmp_path / "tool.zip", {"notes.txt": (b"hi", 0)})
        written = fetch.extract(archive, tmp_path / "out")
        assert not written[0].stat().st_mode & 0o111

    def test_something_that_is_not_an_archive_is_not_an_error(self, tmp_path) -> None:
        """A release may ship the bare executable, or an installer."""
        plain = tmp_path / "tool-linux-amd64"
        plain.write_bytes(PAYLOAD)
        assert fetch.extract(plain, tmp_path / "out") == []

    def test_a_windows_zip_leaves_the_exe_without_a_mode(self, tmp_path) -> None:
        """The precondition for the counting bug: a real .exe, no `+x` on disk.

        A zip written on Windows stores DOS attributes rather than a Unix mode,
        which is what `make_zip(..., 0)` reproduces here.
        """
        archive = make_zip(tmp_path / "tool.zip", {"tool.exe": (PAYLOAD, 0)})
        written = fetch.extract(archive, tmp_path / "out")
        assert not written[0].stat().st_mode & 0o111

    @pytest.mark.parametrize(
        ("name", "stem"),
        [
            ("tool-1.0-linux.tar.gz", "tool-1.0-linux"),
            ("tool.tgz", "tool"),
            ("tool.tar.xz", "tool"),
            ("tool.zip", "tool"),
            ("tool-linux-amd64", "tool-linux-amd64"),
        ],
    )
    def test_the_extraction_directory_drops_the_whole_suffix(self, name, stem) -> None:
        assert fetch.archive_stem(name) == stem


class TestExtractedExecutablesAreCountedByParsing:
    """`FetchedAsset.executables` must agree with what `build` will discover.

    Both decide membership from the headers. Counting the executable bit
    instead undercounts exactly the files a Windows zip strips it from, so the
    summary claims fewer binaries than the very next command finds.
    """

    def fetched(self, *paths: Path) -> fetch.FetchedAsset:
        return fetch.FetchedAsset(
            asset=asset("tool.zip"),
            path=paths[0],
            digest=DIGEST,
            extracted=tuple(paths),
        )

    def test_a_binary_without_the_executable_bit_still_counts(self, tmp_path) -> None:
        unmarked = tmp_path / "tool.exe"
        unmarked.write_bytes(PAYLOAD)
        unmarked.chmod(0o644)
        assert self.fetched(unmarked).executables == (unmarked,)

    def test_a_file_that_does_not_parse_is_not_counted(self, tmp_path) -> None:
        readme = tmp_path / "README"
        readme.write_bytes(b"just some prose\n")
        assert self.fetched(readme).executables == ()

    def test_an_executable_bit_alone_does_not_qualify(self, tmp_path) -> None:
        """The inverse error: chmod +x on a text file is not a program."""
        script = tmp_path / "notes"
        script.write_bytes(b"still just prose\n")
        script.chmod(0o755)
        assert self.fetched(script).executables == ()

    def test_the_count_matches_what_discover_finds(self, tmp_path) -> None:
        binary = tmp_path / "tool.exe"
        binary.write_bytes(PAYLOAD)
        binary.chmod(0o644)
        (tmp_path / "README").write_bytes(b"prose\n")

        found = discover.collect(tmp_path)
        assert len(self.fetched(binary, tmp_path / "README").executables) == len(
            found.candidates
        )


# --------------------------------------------------------------------------
# The whole flow
# --------------------------------------------------------------------------


class TestFetchAssets:
    @pytest.fixture
    def tarball(self, tmp_path) -> bytes:
        path = make_tarball(tmp_path / "src.tar.gz", {"tool": (PAYLOAD, 0o755)})
        data = path.read_bytes()
        path.unlink()
        return data

    def test_assets_are_downloaded_verified_and_unpacked(
        self, fake_http, tmp_path, tarball
    ) -> None:
        digest = hashlib.sha256(tarball).hexdigest()
        item = asset("tool-linux.tar.gz", digest=digest, size=len(tarball))
        fake_http[item.url] = tarball

        (result,) = fetch.fetch_assets(release(item), [item], tmp_path / "dl")
        assert result.verified
        assert result.path.name == "tool-linux.tar.gz"
        assert result.extracted_to == tmp_path / "dl" / "tool-linux"
        assert [p.name for p in result.executables] == ["tool"]

    def test_extraction_can_be_declined(self, fake_http, tmp_path, tarball) -> None:
        digest = hashlib.sha256(tarball).hexdigest()
        item = asset("tool-linux.tar.gz", digest=digest)
        fake_http[item.url] = tarball

        (result,) = fetch.fetch_assets(
            release(item), [item], tmp_path / "dl", extract_archives=False
        )
        assert result.extracted == ()
        assert result.path.is_file()

    def test_an_unverifiable_asset_stops_everything_up_front(
        self, fake_http, tmp_path
    ) -> None:
        """Nothing is written, so there is no half-done state to reason about."""
        item = asset("tool-linux.tar.gz")
        fake_http[item.url] = PAYLOAD
        with pytest.raises(FetchError, match="--allow-unverified"):
            fetch.fetch_assets(release(item), [item], tmp_path / "dl")
        assert not (tmp_path / "dl").exists()

    def test_one_unverifiable_asset_blocks_the_verifiable_ones(
        self, fake_http, tmp_path
    ) -> None:
        good = asset("good.tar.gz", digest=DIGEST)
        bad = asset("bad.tar.gz")
        fake_http[good.url] = PAYLOAD
        fake_http[bad.url] = PAYLOAD
        with pytest.raises(FetchError, match=re.escape("bad.tar.gz")):
            fetch.fetch_assets(release(good, bad), [good, bad], tmp_path / "dl")
        assert not (tmp_path / "dl").exists()

    def test_the_override_lets_them_through(self, fake_http, tmp_path) -> None:
        item = asset("tool-linux-amd64")
        fake_http[item.url] = PAYLOAD
        (result,) = fetch.fetch_assets(
            release(item), [item], tmp_path / "dl", allow_unverified=True
        )
        assert not result.verified
        assert result.digest == DIGEST  # still reported, just not checked

    def test_a_bare_executable_asset_needs_no_unpacking(
        self, fake_http, tmp_path
    ) -> None:
        """Plenty of releases ship the binary itself rather than an archive."""
        item = asset("tool-linux-amd64", digest=DIGEST)
        fake_http[item.url] = PAYLOAD
        (result,) = fetch.fetch_assets(release(item), [item], tmp_path / "dl")
        assert result.extracted_to is None
        assert result.path.read_bytes() == PAYLOAD

    def test_an_archive_that_will_not_open_is_an_error(
        self, fake_http, tmp_path
    ) -> None:
        """A verified download can still be a broken archive; say so."""
        item = asset("tool-linux.tar.gz", digest=DIGEST)
        fake_http[item.url] = PAYLOAD  # verifies, but is not a tarball
        with pytest.raises(FetchError, match="could not unpack"):
            fetch.fetch_assets(release(item), [item], tmp_path / "dl")

    def test_a_sidecar_is_consulted_when_the_api_has_no_digest(
        self, fake_http, tmp_path
    ) -> None:
        item = asset("tool-linux-amd64")
        sidecar = asset("tool-linux-amd64.sha256")
        fake_http[item.url] = PAYLOAD
        fake_http[sidecar.url] = f"{DIGEST}\n".encode()

        (result,) = fetch.fetch_assets(release(item, sidecar), [item], tmp_path / "dl")
        assert result.verification is not None
        assert result.verification.source == "tool-linux-amd64.sha256"

    def test_a_sidecar_that_disagrees_fails_the_download(
        self, fake_http, tmp_path
    ) -> None:
        item = asset("tool-linux-amd64")
        sidecar = asset("tool-linux-amd64.sha256")
        fake_http[item.url] = PAYLOAD
        fake_http[sidecar.url] = f"{'0' * 64}\n".encode()

        with pytest.raises(FetchError, match="does not match"):
            fetch.fetch_assets(release(item, sidecar), [item], tmp_path / "dl")


class TestTokenIsEnvironmentOnly:
    """Same discipline as the publish token: never an option, never in argv."""

    def test_no_token_means_none(self, monkeypatch) -> None:
        for name in fetch.TOKEN_ENVS:
            monkeypatch.delenv(name, raising=False)
        assert fetch.resolve_token() is None

    @pytest.mark.parametrize("name", fetch.TOKEN_ENVS)
    def test_either_variable_is_honoured(self, monkeypatch, name) -> None:
        for other in fetch.TOKEN_ENVS:
            monkeypatch.delenv(other, raising=False)
        monkeypatch.setenv(name, "  secret  ")
        assert fetch.resolve_token() == "secret"

    def test_gh_token_takes_precedence(self, monkeypatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "first")
        monkeypatch.setenv("GITHUB_TOKEN", "second")
        assert fetch.resolve_token() == "first"
