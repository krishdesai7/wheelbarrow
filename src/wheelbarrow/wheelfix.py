"""Wheel post-processing: apply the platform tag and enforce file modes.

Build backends produce a `py3-none-any` wheel for our project, because as far
as they can tell it is pure Python -- the executable is just package data. Two
things must therefore be fixed up afterwards:

1. The compatibility tag, in the wheel's file name and its `WHEEL` metadata,
   so installers refuse to put a Linux binary on a Mac. `Root-Is-Purelib`
   follows from it: false for a real platform tag, since the package root then
   holds machine code, and true for `any`, which only a script produces.
2. The executable bit on the staged binary. Zip archives carry Unix modes in
   `external_attr`, and pip and uv honour them on extraction, but whether a
   backend preserves the source file's mode is backend-specific. Setting it
   here makes the result independent of that.

Both edits require rewriting the archive, so they happen in a single pass that
also regenerates `RECORD` with fresh hashes.
"""

import base64
import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Final

from .errors import BuildError

#: Fixed timestamp for every entry, so repeated builds of identical inputs
#: produce byte-identical wheels. 1980-01-01 is the earliest a zip can encode.
ZIP_EPOCH: Final[tuple[int, int, int, int, int, int]] = (1980, 1, 1, 0, 0, 0)


class Mode(IntEnum):
    EXEC = 0o755
    DATA = 0o644
    DIR = 0o755


@dataclass(frozen=True)
class RetagResult:
    path: Path
    tag: str
    executables: tuple[str, ...]


def retag_wheel(
    wheel: Path,
    *,
    tag: str,
    executable_paths: set[str],
    output_dir: Path | None = None,
) -> RetagResult:
    """Rewrite `wheel` with compatibility tag `tag`.

    `tag` is a full three-part tag such as `py3-none-macosx_11_0_arm64`.
    `executable_paths` lists archive members that must be marked mode 0o755.
    The original file is replaced unless `output_dir` is given.
    """
    wheel = Path(wheel)
    if not wheel.is_file():
        raise BuildError(f"wheel not found: {wheel}")

    name, version = _parse_wheel_name(wheel.name)
    dist_info: str = f"{name}-{version}.dist-info"
    target_dir: Path = Path(output_dir) if output_dir else wheel.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    target: Path = target_dir / f"{name}-{version}-{tag}.whl"

    record_name: str = f"{dist_info}/RECORD"
    wheel_meta_name: str = f"{dist_info}/WHEEL"
    wheel_json_name: str = f"{dist_info}/WHEEL.json"

    buffer = io.BytesIO()
    records: list[tuple[str, str, int]] = []
    seen_wheel_meta = False

    with zipfile.ZipFile(wheel) as src:
        if wheel_meta_name not in src.namelist():
            raise BuildError(
                f"{wheel.name} has no {wheel_meta_name}; is it a valid wheel?"
            )
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                if item.filename == record_name:
                    continue  # regenerated below
                if item.is_dir():
                    _write_dir(dst, item.filename)
                    continue

                data: bytes = src.read(item.filename)
                if item.filename == wheel_meta_name:
                    data = _rewrite_wheel_metadata(data, tag)
                    seen_wheel_meta = True
                elif item.filename == wheel_json_name:
                    # uv_build writes this alongside WHEEL as a non-standard
                    # convenience copy. Leaving it stale would ship a wheel
                    # whose two metadata files disagree about the tag.
                    data = _rewrite_wheel_json(data, tag)

                mode: Mode = (
                    Mode.EXEC if item.filename in executable_paths else Mode.DATA
                )
                _write_file(dst, item.filename, data, mode)
                records.append((item.filename, _sha256_digest(data), len(data)))

            record_body: bytes = _render_record(records, record_name)
            _write_file(dst, record_name, record_body, Mode.DATA)

    if not seen_wheel_meta:  # pragma: no cover - guarded above
        raise BuildError(f"{wheel.name}: WHEEL metadata was not rewritten")

    target.write_bytes(buffer.getvalue())
    if target != wheel:
        wheel.unlink()

    return RetagResult(
        path=target,
        tag=tag,
        executables=tuple(sorted(executable_paths)),
    )


# --------------------------------------------------------------------------
# Zip helpers
# --------------------------------------------------------------------------


def _write_file(zf: zipfile.ZipFile, name: str, data: bytes, mode: Mode) -> None:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    info.create_system = 3  # Unix, so external_attr is read as a mode
    info.external_attr = (mode.value & 0xFFFF) << 0x10
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, data)


def _write_dir(zf: zipfile.ZipFile, name: str) -> None:
    info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
    info.create_system = 3
    info.external_attr = (
        Mode.DIR.value & 0xFFFF
    ) << 0x10 | 0x10  # 0x10 = FILE_ATTRIBUTE_DIRECTORY
    zf.writestr(info, b"")


def _sha256_digest(data: bytes) -> str:
    digest: bytes = hashlib.sha256(data).digest()
    encoded: str = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}"


def _render_record(records: list[tuple[str, str, int]], record_name: str) -> bytes:
    """Build a PEP 376 RECORD. Its own entry carries no hash or size."""
    out = io.StringIO(newline="")
    writer: csv.Writer = csv.writer(out, lineterminator="\n")
    for path, digest, size in records:
        writer.writerow([path, digest, size])
    writer.writerow([record_name, "", ""])
    return out.getvalue().encode("utf-8")


def _is_pure(tag: str) -> bool:
    """Whether `tag` claims no platform at all.

    Only the platform component of the compatibility tag is consulted, and it
    may be a dot-separated set. A wheel is pure exactly when every platform it
    advertises is `any`, which for wheelbarrow means the packaged executable is
    a script rather than machine code.
    """
    platform: str = tag.rsplit("-", 1)[-1]
    return all(part == "any" for part in platform.split("."))


def _rewrite_wheel_metadata(data: bytes, tag: str) -> bytes:
    """Replace the `Tag:` lines and set `Root-Is-Purelib:` to match."""
    lines: list[str] = data.decode("utf-8").splitlines()
    kept: list[str] = [
        line
        for line in lines
        if not line.lower().startswith(("tag:", "root-is-purelib:"))
    ]
    # Keep the trailing blank line convention of message-style metadata.
    while kept and not kept[-1].strip():
        kept.pop()
    purelib: str = "true" if _is_pure(tag) else "false"
    kept.extend((f"Root-Is-Purelib: {purelib}", f"Tag: {tag}"))
    return ("\n".join(kept) + "\n").encode("utf-8")


def _rewrite_wheel_json(data: bytes, tag: str) -> bytes:
    """Keep uv_build's `WHEEL.json` in step with the rewritten `WHEEL`.

    The file is a uv extension rather than part of the wheel specification, so
    unknown keys are preserved and only the two that describe placement and
    compatibility are replaced.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        # Not something we understand; leave it exactly as the backend wrote it.
        return data
    if not isinstance(payload, dict):
        return data

    payload["tags"] = [tag]
    payload["root-is-purelib"] = _is_pure(tag)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


_WHEEL_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<name>.+?)-(?P<version>[^-]+?)"
    r"(?:-(?P<build>[0-9][^-]*?))?"
    r"-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl"
)


def _parse_wheel_name(filename: str) -> tuple[str, str]:
    match: re.Match[str] | None = _WHEEL_NAME_RE.fullmatch(filename)
    if not match:
        raise BuildError(f"cannot parse wheel file name: {filename}")
    return match.group("name"), match.group("version")


def escape_filename_component(value: str) -> str:
    """Escape a name or version for use in a wheel file name (PEP 427)."""
    return re.sub(r"[^\w\d.]+", "_", value, flags=re.UNICODE)
