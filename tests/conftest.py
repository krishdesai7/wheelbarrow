"""Shared fixtures: synthetic executables for every format we inspect.

Building headers by hand keeps the test suite hermetic -- we can exercise the
Linux and Windows code paths from a macOS CI runner without shipping binary
fixtures in the repository.
"""

import struct
from typing import TYPE_CHECKING, Final, Literal

import pytest

from wheelbarrow.wheelfix import Mode

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

GLIBC_INTERP: Final[bytes] = b"/lib64/ld-linux-x86-64.so.2\x00"
MUSL_INTERP: Final[bytes] = b"/lib/ld-musl-x86_64.so.1\x00"

PT_INTERP: Final[int] = 3


def make_elf(
    machine: int,
    *,
    bits: int = 64,
    little: bool = True,
    interp: bytes | None = GLIBC_INTERP,
) -> bytes:
    """Assemble a minimal but well-formed ELF image."""
    endian: Literal["<", ">"] = "<" if little else ">"
    ei_class: Literal[1, 2] = 2 if bits == 0x40 else 1
    ident = bytes([0x7F, 0x45, 0x4C, 0x46, ei_class, 1 if little else 2, 1, 0])
    ident += b"\x00" * 8

    if bits == 0x40:
        ehsize, phentsize = 0x40, 0x38
    else:
        ehsize, phentsize = 0x34, 0x20

    phnum: Literal[0, 1] = 1 if interp else 0
    phoff: Literal[0, 0x34, 0x40] = ehsize if interp else 0
    interp_off: int = ehsize + phentsize

    if bits == 0x40:
        header = ident + struct.pack(
            f"{endian}HHIQQQIHHHHHH",
            2,  # e_type = ET_EXEC
            machine,
            1,  # e_version
            0x400000,  # e_entry
            phoff,
            0,  # e_shoff
            0,  # e_flags
            ehsize,
            phentsize,
            phnum,
            64,  # e_shentsize
            0,  # e_shnum
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
            0,
            0,
            ehsize,
            phentsize,
            phnum,
            40,
            0,
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

    return header + body + b"\x00" * 256


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
