"""Shared fixtures: synthetic executables for every format we inspect.

Building headers by hand keeps the test suite hermetic -- we can exercise the
Linux and Windows code paths from a macOS CI runner without shipping binary
fixtures in the repository.
"""

import email.message
import hashlib
import io
import struct
import tarfile
import urllib.error
from typing import TYPE_CHECKING, Any, Final, Literal

import pytest

from wheelbarrow import fetch
from wheelbarrow.wheelfix import Mode

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

GLIBC_INTERP: Final[bytes] = b"/lib64/ld-linux-x86-64.so.2\x00"
MUSL_INTERP: Final[bytes] = b"/lib/ld-musl-x86_64.so.1\x00"

PT_INTERP: Final[int] = 3


SHT_STRTAB: Final[int] = 3
SHT_GNU_VERNEED: Final[int] = 0x6FFFFFFE


def make_verneed(endian: str, versions: list[str]) -> tuple[bytes, bytes]:
    """Assemble a `.gnu.version_r` section and the string table it points into.

    One `Verneed` for `libc.so.6`, carrying one `Vernaux` per symbol version.
    Both records are a fixed 16 bytes in either ELF class.
    """
    strtab = bytearray(b"\x00")

    def intern(text: str) -> int:
        offset: int = len(strtab)
        strtab.extend(text.encode() + b"\x00")
        return offset

    file_name: int = intern("libc.so.6")
    aux = b""
    for index, version in enumerate(versions):
        name: int = intern(version)
        next_aux: int = 0x10 if index + 1 < len(versions) else 0
        # vna_hash, vna_flags, vna_other, vna_name, vna_next
        aux += struct.pack(f"{endian}IHHII", 0, 0, 0, name, next_aux)

    # vn_version, vn_cnt, vn_file, vn_aux, vn_next
    verneed: bytes = (
        struct.pack(f"{endian}HHIII", 1, len(versions), file_name, 0x10, 0) + aux
    )
    return verneed, bytes(strtab)


def make_section_headers(
    endian: str, bits: int, entries: list[tuple[int, int, int, int]]
) -> bytes:
    """Pack section headers from (type, offset, size, link) tuples."""
    out = b""
    for sh_type, offset, size, link in entries:
        if bits == 0x40:
            out += struct.pack(
                f"{endian}IIQQQQIIQQ", 0, sh_type, 0, 0, offset, size, link, 0, 1, 0
            )
        else:
            out += struct.pack(
                f"{endian}IIIIIIIIII", 0, sh_type, 0, 0, offset, size, link, 0, 1, 0
            )
    return out


def make_elf(
    machine: int,
    *,
    bits: int = 64,
    little: bool = True,
    interp: bytes | None = GLIBC_INTERP,
    osabi: int = 0,
    glibc_versions: list[str] | None = None,
) -> bytes:
    """Assemble a minimal but well-formed ELF image.

    `osabi` is `EI_OSABI`; 0 is what Linux toolchains emit, and 9 is FreeBSD.
    `glibc_versions` adds a `.gnu.version_r` section declaring those symbol
    versions, which is what decides the manylinux floor.
    """
    endian: Literal["<", ">"] = "<" if little else ">"
    ei_class: Literal[1, 2] = 2 if bits == 0x40 else 1
    ident = bytes([0x7F, 0x45, 0x4C, 0x46, ei_class, 1 if little else 2, 1, osabi])
    ident += b"\x00" * 8

    if bits == 0x40:
        ehsize, phentsize, shentsize = 0x40, 0x38, 0x40
    else:
        ehsize, phentsize, shentsize = 0x34, 0x20, 0x28

    phnum: Literal[0, 1] = 1 if interp else 0
    phoff: Literal[0, 0x34, 0x40] = ehsize if interp else 0
    interp_off: int = ehsize + phentsize

    # Laid out after the program headers, so the offsets below are absolute.
    body_size: int = (phentsize + len(interp)) if interp else 0
    versions_blob = b""
    shoff = shnum = 0
    if glibc_versions is not None:
        verneed, strtab = make_verneed(endian, glibc_versions)
        verneed_off: int = ehsize + body_size
        strtab_off: int = verneed_off + len(verneed)
        shoff = strtab_off + len(strtab)
        shnum = 3  # the mandatory null header, then ours
        versions_blob = (
            verneed
            + strtab
            + make_section_headers(
                endian,
                bits,
                [
                    (0, 0, 0, 0),
                    (SHT_GNU_VERNEED, verneed_off, len(verneed), 2),
                    (SHT_STRTAB, strtab_off, len(strtab), 0),
                ],
            )
        )

    if bits == 0x40:
        header = ident + struct.pack(
            f"{endian}HHIQQQIHHHHHH",
            2,  # e_type = ET_EXEC
            machine,
            1,  # e_version
            0x400000,  # e_entry
            phoff,
            shoff,
            0,  # e_flags
            ehsize,
            phentsize,
            phnum,
            shentsize,
            shnum,
            0,  # e_shstrndx
        )
    else:
        header = ident + struct.pack(
            f"{endian}HHIIIIIHHHHHH",
            2,
            machine,
            1,
            0x8048000,
            phoff,
            shoff,
            0,
            ehsize,
            phentsize,
            phnum,
            shentsize,
            shnum,
            0,
        )

    body = b""
    if interp:
        if bits == 64:
            body = struct.pack(
                f"{endian}IIQQQQQQ",
                PT_INTERP,
                4,  # p_flags = R
                interp_off,
                0,
                0,
                len(interp),  # p_filesz
                len(interp),  # p_memsz
                1,  # p_align
            )
        else:
            body: bytes = struct.pack(
                f"{endian}IIIIIIII",
                PT_INTERP,
                interp_off,
                0,
                0,
                len(interp),
                len(interp),
                4,
                1,
            )
        body += interp

    return header + body + versions_blob + b"\x00" * 256


def make_macho(cputype: int, *, minos: tuple[int, int] = (11, 0)) -> bytes:
    """Assemble a 64-bit little-endian Mach-O with an LC_BUILD_VERSION."""
    version: int = (minos[0] << 0x10) | (minos[1] << 0x08)
    load_cmd: bytes = struct.pack("<IIIIII", 0x32, 24, 1, version, version, 0)
    header: bytes = struct.pack(
        "<IiIIIIII",
        0xFEEDFACF,  # written little-endian by struct, so bytes are cf fa ed fe
        cputype,
        0,  # cpusubtype
        2,  # MH_EXECUTE
        1,  # ncmds
        len(load_cmd),
        0,  # flags
        0,  # reserved
    )
    return header + load_cmd + b"\x00" * 128


def make_fat_macho(cputypes: list[int], *, minos: tuple[int, int] = (11, 0)) -> bytes:
    """Assemble a universal binary wrapping one Mach-O per architecture."""
    entry_size = 20
    header_size: int = 8 + entry_size * len(cputypes)
    slices: list[bytes] = [make_macho(ct, minos=minos) for ct in cputypes]

    offset: int = (header_size + 0xFFF) // 0x1000 * 0x1000
    out: bytes = struct.pack(">II", 0xCAFEBABE, len(cputypes))
    offsets: list[int] = []
    for cputype, payload in zip(cputypes, slices, strict=True):
        offsets.append(offset)
        out += struct.pack(">iIIII", cputype, 0, offset, len(payload), 12)
        offset += (len(payload) + 0xFFF) // 0x1000 * 0x1000

    blob = bytearray(out)
    for off, payload in zip(offsets, slices, strict=True):
        if len(blob) < off:
            blob.extend(b"\x00" * (off - len(blob)))
        blob[off : off + len(payload)] = payload
    return bytes(blob)


def make_pe(machine: int) -> bytes:
    """Assemble a minimal PE/COFF image."""
    pe_off = 0x80
    dos = bytearray(b"\x00" * pe_off)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, pe_off)
    coff: bytes = b"PE\x00\x00" + struct.pack(
        "<HHIIIHH", machine, 1, 0, 0, 0, 0xE0, 0x22
    )
    return bytes(dos) + coff + b"\x00" * 128


@pytest.fixture
def write_binary(tmp_path: Path) -> Callable[[str, bytes, Mode], Path]:
    """Return a helper that writes bytes to a file and marks it executable."""

    def _write(name: str, data: bytes, mode: Mode = Mode.DATA) -> Path:
        path = tmp_path / name
        # `name` may carry a directory, for tests needing two same-named files.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    return _write


@pytest.fixture
def elf_binary(write_binary: Callable[[str, bytes, Mode], Path]) -> Path:
    return write_binary("tool", make_elf(0x3E), Mode.DATA)


@pytest.fixture
def macho_binary(write_binary) -> Path:
    return write_binary("tool", make_macho(0x0100000C))


#: A real, runnable script: the installed-wheel test executes what it packages,
#: and `$@` forwarding is what proves the launcher passes arguments through.
SHELL_SCRIPT: Final[bytes] = b'#!/bin/sh\nexec echo "tool:" "$@"\n'


@pytest.fixture
def shell_script(write_binary: Callable[[str, bytes, Mode], Path]) -> Path:
    return write_binary("tool.sh", SHELL_SCRIPT, Mode.EXEC)


@pytest.fixture
def fake_http(monkeypatch) -> dict[str, bytes | Exception]:
    """Route `fetch`'s only HTTP entry point to an in-memory table.

    Every request the module makes funnels through `fetch._open`, so replacing
    that one function is what guarantees the suite never reaches GitHub. An
    unregistered URL answers 404, exactly as a missing release would; mapping a
    URL to an exception simulates a transport failure.
    """
    routes: dict[str, bytes | Exception] = {}

    def _open(url: str, **_kwargs: Any) -> Any:
        payload: bytes | Exception | None = routes.get(url)
        if payload is None:
            raise urllib.error.HTTPError(
                url, 404, "Not Found", email.message.Message(), None
            )
        if isinstance(payload, Exception):
            raise payload
        return io.BytesIO(payload)

    monkeypatch.setattr(fetch, "_open", _open)
    return routes


@pytest.fixture
def tarball() -> tuple[bytes, str]:
    """A gzipped tar holding one executable script, with its sha256."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("tool")
        info.size = len(SHELL_SCRIPT)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(SHELL_SCRIPT))
    data: bytes = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()
