"""Fetch release assets from GitHub, verified against their checksums.

`build` needs an executable on disk, and getting one is nearly always the same
four steps: find the release, pick the wanted assets out of the twenty or
thirty it publishes, check them against a digest, and unpack them. Doing that
by hand is where the mistakes happen, because the two worst outcomes both look
exactly like success -- an unverified download, and a pattern that quietly
matched nothing.

Talking to the API directly rather than shelling out to `gh` keeps
`uvx wheelforge` self-contained: a public release needs no credentials at all.
A token is read from `GH_TOKEN` or `GITHUB_TOKEN` when one is set, for private
repositories and for the larger rate limit. As with the publish token, there is
deliberately no option for it, so it cannot reach a shell history.

Verification prefers the digest the API reports per asset, which GitHub
computes itself on upload. Assets published before mid-2025 have none, so a
checksum file in the same release is the fallback, in either convention found
in the wild: a bare digest in `<asset>.sha256`, or `SHA256SUMS`-style rows of
`<digest>  <name>`. Neither is a signature. Both attest that these are the
bytes that were uploaded, not who uploaded them.

Nothing here is on the build path, and `build` never reaches out to the
network. Fetching is a separate step that happens to end where building starts.
"""

import fnmatch
import hashlib
import json
import os
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from . import __version__
from .errors import FetchError, InspectionError
from .probe import inspect_binary

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

API_ROOT: Final[str] = "https://api.github.com"

#: Checked in order; the first non-empty one wins. `gh` writes GH_TOKEN, while
#: GITHUB_TOKEN is what CI runners inject, so both are worth honouring.
TOKEN_ENVS: Final[tuple[str, ...]] = ("GH_TOKEN", "GITHUB_TOKEN")

#: Generous compared to the index check in `pypi.py`: these are multi-megabyte
#: downloads over a link we do not control, not a HEAD request.
DEFAULT_TIMEOUT: Final[float] = 30.0

CHUNK: Final[int] = 1 << 16

#: A partial download carries this until its digest has been checked, so a
#: file under its real name is always one that passed verification.
PARTIAL_SUFFIX: Final[str] = ".part"

SHA256_RE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-fA-F]{64}\Z")

#: Longest first: `.tar.gz` must be tried before `.gz` so the stem comes out as
#: `starship-x86_64-unknown-linux-musl` rather than `...musl.tar`.
ARCHIVE_SUFFIXES: Final[tuple[str, ...]] = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tgz",
    ".tbz2",
    ".txz",
    ".tar",
    ".zip",
)

TAR_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar"}
)

#: Suffixes a per-asset checksum sidecar is published under.
SIDECAR_SUFFIXES: Final[tuple[str, ...]] = (
    ".sha256",
    ".sha256sum",
    ".sha256.txt",
    ".sha256sums",
)

#: Whole-release checksum manifests, matched case-insensitively against the
#: asset name. Compared as globs so `starship-v1.26.0-checksums.txt` matches.
CHECKSUM_GLOBS: Final[tuple[str, ...]] = (
    "checksums.txt",
    "checksums",
    "*sha256sums*",
    "*checksums*",
    "*.sha256",
)

#: Platform installers. Each is a container format of its own that nothing here
#: can open, so the executable inside is out of reach even though the asset is
#: exactly the program the user wants.
INSTALLER_SUFFIXES: Final[tuple[str, ...]] = (
    ".msi",
    ".deb",
    ".rpm",
    ".pkg",
    ".dmg",
    ".apk",
    ".snap",
    ".nupkg",
    ".flatpak",
    ".appx",
    ".msix",
)

#: Signatures and attestations. They describe a payload rather than being one,
#: and wheelforge does not verify signatures -- only digests.
SIGNATURE_SUFFIXES: Final[tuple[str, ...]] = (
    ".sig",
    ".asc",
    ".pem",
    ".crt",
    ".cert",
    ".sigstore",
    ".intoto.jsonl",
    ".sbom.json",
    ".spdx.json",
)

#: Documentation and metadata shipped alongside a release.
DOC_SUFFIXES: Final[tuple[str, ...]] = (".txt", ".md", ".pdf", ".json")


@dataclass(frozen=True)
class Asset:
    """One downloadable file attached to a release."""

    name: str
    url: str
    size: int
    #: Lowercase hex sha256 as reported by the API, or None for assets
    #: uploaded before GitHub began recording one.
    digest: str | None = None


@dataclass(frozen=True)
class Release:
    owner: str
    repo: str
    tag: str
    assets: tuple[Asset, ...] = ()

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def named(self, name: str) -> Asset | None:
        """Return the asset called `name`, if the release has one."""
        for asset in self.assets:
            if asset.name == name:
                return asset
        return None


@dataclass(frozen=True)
class Verification:
    """Where the digest an asset was checked against came from."""

    digest: str
    #: Human-readable provenance, e.g. `the GitHub API` or a sidecar's name.
    source: str


@dataclass(frozen=True)
class FetchedAsset:
    """The outcome of downloading -- and possibly unpacking -- one asset."""

    asset: Asset
    path: Path
    digest: str
    verification: Verification | None = None
    extracted_to: Path | None = None
    extracted: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def verified(self) -> bool:
        return self.verification is not None

    @property
    def executables(self) -> tuple[Path, ...]:
        """Extracted files that parse as executables -- what `build` will find.

        Decided by the headers, never by the executable bit, for the same
        reason `discover.collect` decides it that way: a Windows-produced zip
        stores DOS attributes rather than a Unix mode, so `starship.exe` comes
        out of one with no `+x` and would go uncounted here while `build`
        packaged it perfectly happily.
        """
        return tuple(p for p in self.extracted if _parses_as_executable(p))


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------


def _parses_as_executable(path: Path) -> bool:
    try:
        inspect_binary(path)
    except InspectionError, OSError:
        return False
    return True


def archive_stem(name: str) -> str:
    """Strip a compound archive suffix. `Path.stem` only removes the last one."""
    lowered: str = name.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def is_checksum_asset(name: str) -> bool:
    """Whether an asset is checksum material rather than a payload.

    These are filtered out of the download set even when a pattern matches
    them, because a `*.tar.gz*` pattern -- the natural way to ask for the
    tarballs -- sweeps the sidecars up too. They are fetched anyway when a
    digest is needed, so nothing is lost by not treating them as payload.
    """
    lowered: str = name.lower()
    if lowered.endswith(SIDECAR_SUFFIXES):
        return True
    return any(fnmatch.fnmatch(lowered, glob) for glob in CHECKSUM_GLOBS)


def unpackable_reason(name: str) -> str | None:
    """Which unusable category an asset falls into, or `None` if it might build.

    Stated as a blacklist rather than a whitelist because the interesting case
    is the asset with no extension at all: a release that ships the bare
    executable is exactly what `build` wants, and a whitelist would drop it.

    Phrased as a plural noun so it reads both as a count (`3 installers
    wheelforge cannot unpack`) and as the reason a pattern matched nothing.
    """
    lowered: str = name.lower()
    if lowered.endswith(INSTALLER_SUFFIXES):
        return "installers wheelforge cannot unpack"
    if lowered.endswith(SIGNATURE_SUFFIXES):
        return "signatures and attestations, which are not payloads"
    if lowered.endswith(DOC_SUFFIXES):
        return "documentation and metadata rather than programs"
    return None


def is_payload_asset(name: str) -> bool:
    """Whether an asset is something `build` could eventually consume.

    This is the set `--list` shows and the set a bare `fetch` downloads. Both
    exclusions are held to the same rule as `is_checksum_asset`: an asset that
    cannot become a wheel is filtered out even when a pattern matches it, so
    `-p '*windows*'` does not drag in the `.msi` beside the `.zip`.
    """
    return not is_checksum_asset(name) and unpackable_reason(name) is None


# --------------------------------------------------------------------------
# Parsing the source
# --------------------------------------------------------------------------

#: `owner/repo`, with the character classes GitHub actually permits.
SLUG_RE: Final[re.Pattern[str]] = re.compile(
    r"\A(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?\Z"
)


def parse_source(source: str) -> tuple[str, str, str | None]:
    """Resolve a user-supplied release reference to `(owner, repo, tag)`.

    Accepts the URL of a release page, of a specific asset, or of the
    repository itself, as well as the bare `owner/repo` shorthand. A `None` tag
    means the latest release, which is also what `/releases/latest` denotes.
    """
    text: str = source.strip().rstrip("/")
    if not text:
        raise FetchError("no release was given")

    match: re.Match[str] | None = SLUG_RE.match(text)
    if match:
        return match["owner"], match["repo"], None

    # Bare `github.com/...` has no scheme, and urlsplit would read the host as
    # a relative path; give it one so netloc and path split correctly.
    if "://" not in text:
        text = f"https://{text}"

    parts: urllib.parse.SplitResult = urllib.parse.urlsplit(text)
    segments: list[str] = [s for s in parts.path.split("/") if s]

    # The API spells the same release `/repos/<owner>/<repo>/releases/...`.
    if segments and segments[0] == "repos":
        segments = segments[1:]

    if len(segments) < 2:
        raise FetchError(
            f"could not tell which repository {source!r} refers to. Give a "
            f"release URL like "
            f"https://github.com/starship/starship/releases/tag/v1.26.0, or "
            f"just `owner/repo`."
        )

    owner: str = segments[0]
    repo: str = segments[1].removesuffix(".git")
    tag: str | None = _tag_from_segments(segments[2:])
    return owner, repo, tag


def _tag_from_segments(rest: list[str]) -> str | None:
    """Pull the tag out of the path below `<owner>/<repo>`.

    Tags may contain slashes, so everything after the marker is rejoined
    rather than taken as a single segment.
    """
    if len(rest) < 2 or rest[0] != "releases":
        return None
    marker: str = rest[1]
    if marker == "latest":
        return None
    if marker in {"tag", "tags"}:
        return "/".join(rest[2:]) or None
    if marker == "download":
        # `/releases/download/<tag>/<asset>`: the asset name is the last
        # segment, and everything between it and the marker is the tag.
        return "/".join(rest[2:-1]) or None
    return None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def resolve_token() -> str | None:
    """Return a GitHub token from the environment, if one is set.

    Environment-only, like the publish token: there is no option for it, so it
    cannot be captured in a shell history or show up in `ps`.
    """
    for name in TOKEN_ENVS:
        value: str = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drop `Authorization` when a redirect leaves the original host.

    Asset downloads redirect from the API to object storage, and urllib copies
    every header onto the new request. Forwarding a GitHub token to a third
    party would leak it, and the storage host rejects a request that arrives
    carrying a second set of credentials anyway, so this is both a
    confidentiality fix and the reason authenticated downloads work at all.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        new: urllib.request.Request | None = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )
        if new is not None and _host(newurl) != _host(req.full_url):
            for key in list(new.headers):
                if key.lower() == "authorization":
                    del new.headers[key]
        return new


def _request(url: str, *, token: str | None, accept: str) -> urllib.request.Request:
    headers: dict[str, str] = {
        "Accept": accept,
        "User-Agent": f"wheelforge/{__version__}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)  # ruff: ignore[suspicious-url-open-usage]


def _open(url: str, *, timeout: float, token: str | None, accept: str) -> Any:
    opener: urllib.request.OpenerDirector = urllib.request.build_opener(
        _StripAuthOnRedirect
    )
    return opener.open(_request(url, token=token, accept=accept), timeout=timeout)


def _api_failure(exc: urllib.error.HTTPError, what: str) -> FetchError:
    """Turn an API status into something a user can act on."""
    if exc.code == 404:
        return FetchError(
            f"{what} was not found. Check the tag spelling, and note that a "
            f"private repository needs a token in GH_TOKEN or GITHUB_TOKEN."
        )
    if exc.code in {401, 403} and exc.headers.get("X-RateLimit-Remaining") == "0":
        return FetchError(
            "GitHub's rate limit is exhausted for this IP. Unauthenticated "
            "requests are limited to 60 per hour; setting GH_TOKEN or "
            "GITHUB_TOKEN raises that to 5000."
        )
    if exc.code in {401, 403}:
        return FetchError(
            f"GitHub refused the request for {what} ({exc.code} {exc.reason}). "
            f"If a token is set, check that it has not expired and can read "
            f"this repository."
        )
    return FetchError(f"GitHub returned {exc.code} {exc.reason} for {what}")


def get_release(
    owner: str,
    repo: str,
    tag: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    token: str | None = None,
    api_root: str = API_ROOT,
) -> Release:
    """Look up a release and the assets attached to it.

    A `None` tag asks for the latest release, which GitHub defines as the most
    recent non-prerelease, non-draft one -- not simply the newest tag.
    """
    base: str = f"{api_root}/repos/{owner}/{repo}/releases"
    if tag is None:
        url: str = f"{base}/latest"
        what: str = f"the latest release of {owner}/{repo}"
    else:
        url = f"{base}/tags/{urllib.parse.quote(tag, safe='')}"
        what = f"release {tag} of {owner}/{repo}"

    try:
        with _open(
            url, timeout=timeout, token=token, accept="application/vnd.github+json"
        ) as response:
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _api_failure(exc, what) from exc
    except OSError as exc:
        raise FetchError(f"could not reach GitHub to look up {what}: {exc}") from exc
    except ValueError as exc:
        raise FetchError(f"GitHub returned an unreadable response for {what}") from exc

    return Release(
        owner=owner,
        repo=repo,
        tag=str(payload.get("tag_name") or tag or "latest"),
        assets=tuple(_parse_asset(a) for a in payload.get("assets") or ()),
    )


def _parse_asset(payload: dict[str, Any]) -> Asset:
    #: `digest` is `sha256:<hex>`; other algorithms would need their own
    #: handling, so anything unrecognised is treated as absent.
    raw: str = str(payload.get("digest") or "")
    algorithm, _, hexdigest = raw.partition(":")
    digest: str | None = (
        hexdigest.lower()
        if algorithm == "sha256" and SHA256_RE.match(hexdigest)
        else None
    )
    return Asset(
        name=str(payload["name"]),
        url=str(payload["browser_download_url"]),
        size=int(payload.get("size") or 0),
        digest=digest,
    )


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def select_assets(release: Release, patterns: Sequence[str]) -> list[Asset]:
    """Return the payload assets matching `patterns`, in release order.

    A pattern matching nothing is an error rather than an empty result: it
    almost always means a typo or a naming change between versions, and
    silently downloading fewer files than asked for is the failure that would
    surface much later as a missing wheel.
    """
    payload: list[Asset] = [a for a in release.assets if is_payload_asset(a.name)]
    if not patterns:
        return payload

    wanted: set[str] = set()
    for pattern in patterns:
        matched: list[Asset] = [a for a in payload if fnmatch.fnmatch(a.name, pattern)]
        if not matched:
            available: str = "\n".join(f"    {a.name}" for a in payload)
            raise FetchError(
                f"no asset in {release.slug} {release.tag} matches "
                f"{pattern!r}{_filtered_detail(release, pattern)}. "
                f"Available:\n{available}"
            )
        wanted.update(a.name for a in matched)

    return [a for a in payload if a.name in wanted]


def _filtered_detail(release: Release, pattern: str) -> str:
    """Explain a pattern that matched only assets `select_assets` filters out.

    Without this the message reads as though the release does not publish the
    file at all, when in fact it does and wheelforge declined it -- which
    sends the reader looking for a naming change that never happened.
    """
    swept: list[Asset] = [a for a in release.assets if fnmatch.fnmatch(a.name, pattern)]
    if not swept:
        return ""

    reasons: list[str] = sorted(
        {
            "checksum files, which are fetched automatically when needed"
            if is_checksum_asset(a.name)
            else str(unpackable_reason(a.name))
            for a in swept
        }
    )
    return " (it matched only " + "; ".join(reasons) + ")"


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------


def parse_checksums(text: str, *, want: str, allow_bare: bool = False) -> str | None:
    """Find the sha256 recorded for `want` in a checksum file.

    Handles both conventions: `SHA256SUMS`-style `<digest>  <name>` rows, and a
    sidecar holding nothing but the digest. `allow_bare` is set only for a file
    whose own name already identifies the asset, since a bare digest carries no
    other clue about what it covers.
    """
    for line in text.splitlines():
        stripped: str = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields: list[str] = stripped.split()
        if not SHA256_RE.match(fields[0]):
            continue
        if len(fields) == 1:
            if allow_bare:
                return fields[0].lower()
            continue
        # A leading `*` is GNU coreutils' binary-mode marker, and the recorded
        # name may carry a directory the release does not reproduce.
        recorded: str = Path(fields[-1].lstrip("*")).name
        if recorded == want:
            return fields[0].lower()
    return None


def _read_asset_text(asset: Asset, *, timeout: float, token: str | None) -> str | None:
    """Download a small checksum asset into memory, or None if unreachable."""
    try:
        with _open(
            asset.url, timeout=timeout, token=token, accept="application/octet-stream"
        ) as response:
            return response.read(64 * 1024).decode("utf-8", "replace")
    except OSError:
        # A missing or unreadable checksum file is not fatal here; the caller
        # decides what an unverifiable asset means.
        return None


def find_digest(
    release: Release,
    asset: Asset,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    token: str | None = None,
    read_text: Callable[[Asset], str | None] | None = None,
) -> Verification | None:
    """Determine what `asset` should hash to, and say where that came from.

    The API's own digest is preferred: GitHub computes it on upload, so unlike
    a sidecar it is not simply another file the publisher supplied. Older
    assets have none, and then a checksum file in the same release is consulted
    -- the per-asset sidecar first, since a whole-release manifest may not list
    every file.
    """
    if asset.digest:
        return Verification(asset.digest, "the GitHub API")

    read: Callable[[Asset], str | None] = read_text or (
        lambda a: _read_asset_text(a, timeout=timeout, token=token)
    )
    return _sidecar_digest(release, asset, read) or _manifest_digest(
        release, asset, read
    )


def _sidecar_digest(
    release: Release, asset: Asset, read: Callable[[Asset], str | None]
) -> Verification | None:
    """Look for a `<asset>.sha256`-style file covering this asset alone.

    Only here is a bare digest accepted, because only here does the file's own
    name say what it covers.
    """
    for suffix in SIDECAR_SUFFIXES:
        sidecar: Asset | None = release.named(asset.name + suffix)
        if sidecar is None:
            continue
        text: str | None = read(sidecar)
        if text is None:
            continue
        digest: str | None = parse_checksums(text, want=asset.name, allow_bare=True)
        if digest:
            return Verification(digest, sidecar.name)
    return None


def _manifest_digest(
    release: Release, asset: Asset, read: Callable[[Asset], str | None]
) -> Verification | None:
    """Fall back to a whole-release checksum file listing many assets."""
    for candidate in release.assets:
        # A name starting with the asset's own is a sidecar, already tried.
        if not is_checksum_asset(candidate.name) or candidate.name.startswith(
            asset.name
        ):
            continue
        text: str | None = read(candidate)
        if text is None:
            continue
        digest: str | None = parse_checksums(text, want=asset.name)
        if digest:
            return Verification(digest, candidate.name)
    return None


def file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------


def download_asset(
    asset: Asset,
    dest_dir: Path,
    *,
    expected: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    token: str | None = None,
    on_chunk: Callable[[int], None] | None = None,
) -> tuple[Path, str]:
    """Download `asset` into `dest_dir`, returning its path and its sha256.

    Bytes are hashed as they arrive and written to a `.part` file that is only
    renamed into place once the digest matches, so a file sitting under its
    real name is always one that passed. A mismatch leaves nothing behind for a
    later `build` to pick up by accident.

    An existing file whose digest already matches is left alone, which makes
    re-running over a populated directory cheap.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target: Path = dest_dir / asset.name
    partial: Path = dest_dir / f"{asset.name}{PARTIAL_SUFFIX}"

    if expected and target.is_file() and file_digest(target) == expected:
        if on_chunk:
            on_chunk(target.stat().st_size)
        return target, expected

    digest = hashlib.sha256()
    try:
        with (
            _open(
                asset.url,
                timeout=timeout,
                token=token,
                accept="application/octet-stream",
            ) as response,
            partial.open("wb") as out,
        ):
            while chunk := response.read(CHUNK):
                digest.update(chunk)
                out.write(chunk)
                if on_chunk:
                    on_chunk(len(chunk))
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise FetchError(f"could not download {asset.name}: {exc}") from exc

    actual: str = digest.hexdigest()
    if expected and actual != expected:
        partial.unlink(missing_ok=True)
        raise FetchError(
            f"{asset.name} does not match its recorded checksum and was "
            f"discarded.\n"
            f"  expected  {expected}\n"
            f"  received  {actual}\n"
            f"A single failure is usually a truncated transfer, so retry once. "
            f"A repeat means the bytes being served are not the bytes the "
            f"release recorded."
        )

    partial.replace(target)
    return target, actual


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def extract(archive: Path, dest: Path) -> list[Path]:
    """Unpack `archive` into `dest`, returning the regular files written.

    Returns an empty list for anything that is not a recognised archive -- a
    release that ships the bare executable, or an installer such as `.msi`,
    needs no unpacking and is not an error.
    """
    lowered: str = archive.name.lower()
    if lowered.endswith(tuple(TAR_SUFFIXES)):
        return _extract_tar(archive, dest)
    if lowered.endswith(".zip"):
        return _extract_zip(archive, dest)
    return []


def _extract_tar(archive: Path, dest: Path) -> list[Path]:
    """Extract a tarball under the `data` filter.

    The filter refuses `..` traversal, links pointing outside the destination
    and device nodes, and drops setuid bits -- all of which matter for an
    archive downloaded from the internet. It keeps the executable bit, which is
    the one permission that has to survive. An absolute member name is not
    refused but defanged: the leading separator is stripped, as GNU tar does,
    so it lands under `dest` like everything else.
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as tar:
            tar.extractall(dest, filter="data")
            members: list[tarfile.TarInfo] = tar.getmembers()
    except tarfile.FilterError as exc:
        raise FetchError(
            f"refusing to unpack {archive.name}: it contains a member that "
            f"would be written outside {dest} ({exc})"
        ) from exc
    except tarfile.TarError as exc:
        raise FetchError(f"could not unpack {archive.name}: {exc}") from exc

    # The same strip the filter applied. Joining an absolute member name would
    # otherwise discard `dest` entirely and report a path nothing was written
    # to, since `Path("/a") / "/b"` is `/b`.
    return [dest / m.name.lstrip("/") for m in members if m.isfile()]


def _extract_zip(archive: Path, dest: Path) -> list[Path]:
    """Extract a zip, restoring the executable bit that `zipfile` discards.

    `ZipFile.extract` sanitises member paths itself, but applies none of the
    stored Unix mode, so an executable would come out unrunnable.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                extracted = Path(zf.extract(member, dest))
                if member.is_dir():
                    continue
                mode: int = member.external_attr >> 16
                if mode & 0o111:
                    extracted.chmod(0o755)
                written.append(extracted)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FetchError(f"could not unpack {archive.name}: {exc}") from exc
    return written


# --------------------------------------------------------------------------
# The whole flow
# --------------------------------------------------------------------------


def fetch_assets(
    release: Release,
    assets: Sequence[Asset],
    dest: Path,
    *,
    extract_archives: bool = True,
    allow_unverified: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    token: str | None = None,
    on_start: Callable[[Asset, Verification | None], None] | None = None,
    on_chunk: Callable[[int], None] | None = None,
) -> list[FetchedAsset]:
    """Download, verify and unpack `assets` into `dest`.

    Every digest is resolved before anything is downloaded, so a release with
    no usable checksums fails immediately rather than part way through -- there
    is no state to unwind, and nothing has been written yet.
    """
    plan: list[tuple[Asset, Verification | None]] = [
        (a, find_digest(release, a, timeout=timeout, token=token)) for a in assets
    ]

    unverifiable: list[Asset] = [a for a, v in plan if v is None]
    if unverifiable and not allow_unverified:
        listed: str = "\n".join(f"    {a.name}" for a in unverifiable)
        raise FetchError(
            f"no checksum is published for these assets of {release.slug} "
            f"{release.tag}:\n{listed}\n"
            f"GitHub only began recording a digest per asset in 2025, and this "
            f"release ships no checksum file either. Pass --allow-unverified "
            f"to download them anyway, having satisfied yourself some other "
            f"way that they are what they claim to be."
        )

    results: list[FetchedAsset] = []
    for asset, verification in plan:
        if on_start:
            on_start(asset, verification)
        path, digest = download_asset(
            asset,
            dest,
            expected=verification.digest if verification else None,
            timeout=timeout,
            token=token,
            on_chunk=on_chunk,
        )

        extracted: list[Path] = []
        target: Path | None = None
        if extract_archives and is_archive(asset.name):
            target = dest / archive_stem(asset.name)
            extracted = extract(path, target)

        results.append(
            FetchedAsset(
                asset=asset,
                path=path,
                digest=digest,
                verification=verification,
                extracted_to=target,
                extracted=tuple(extracted),
            )
        )

    return results
