"""Input inspection: Determine which platform a binary was built for.

Wheels that embed a native executable must carry a platform tag. If they do not,
package managers cannot determine if a given wheel can be installed on the current machine.
Rather than assuming that the binary matches the machine running Wheelbarrow,
this module reads its headers directly. This enables cross-building.

Only the header prefix of each format is parsed. This is enough to recover the CPU
architecture, the operating system, and (where it is cheap to do so) the libc
flavour on Linux and the minimum macOS version.
"""

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .errors import InspectionError

# Format constants
ELF_MAGIC: Final[bytes] = b"\x7fELF"
PE_MAGIC: Final[bytes] = b"MZ"

# Keyed by the first four bytes read as a big-endian integer. A little-endian
# Mach-O stores MH_MAGIC_64 (0xFEEDFACF) as `cf fa ed fe`, which reads back as
# 0xCFFAEDFE, so the byte-reversed constant is the little-endian case.
MACHO_MAGICS: Final[dict[int, tuple[bytes, Literal["<", ">"]]]] = {
    0xCFFAEDFE: (b"\x40", "<"),  # MH_MAGIC_64, little endian
    0xFEEDFACF: (b"\x40", ">"),  # MH_MAGIC_64, big endian
    0xCEFAEDFE: (b"\x20", "<"),  # MH_MAGIC, little endian
    0xFEEDFACE: (b"\x20", ">"),  # MH_MAGIC, big endian
}
FAT_MAGIC: Final[int] = 0xCAFEBABE
FAT_MAGIC_64: Final[int] = 0xCAFEBABF

# ELF e_machine -> normalised architecture name.
ELF_MACHINES: Final[dict[int, str]] = {
    0x03: "i686",
    0x08: "mips",
    0x14: "ppc",
    0x15: "ppc64",
    0x16: "s390x",
    0x28: "armv7l",
    0x2B: "sparc64",
    0x3E: "x86_64",
    0xB7: "aarch64",
    0xF3: "riscv64",
    0x102: "loongarch64",
}

# Mach-O cputype -> normalised architecture name.
MACHO_CPUS: Final[dict[int, str]] = {
    0x00000007: "i686",
    0x0000000C: "armv7l",
    0x01000007: "x86_64",
    0x0100000C: "arm64",
}

# PE COFF machine -> normalised architecture name.
PE_MACHINES: Final[dict[int, str]] = {
    0x014C: "i686",
    0x01C4: "armv7l",
    0x8664: "x86_64",
    0xAA64: "arm64",
}

LC_VERSION_MIN_MACOSX: Final[int] = 0x24
LC_BUILD_VERSION: Final[int] = 0x32
PT_INTERP: Final[int] = 0x03


@dataclass(frozen=True)
class BinaryInfo:
    """Information about an input binary."""

    path: Path
    format: str  # "elf" | "macho" | "macho-universal" | "pe"
    os: str  # "linux" | "macos" | "windows"
    arch: str  # Normalised architecture name, e.g. "x86_64", "aarch64", "arm64"
    libc: str | None = None  # "glibc" | "musl" | "static" (Linux only)
    macos_min: tuple[int, int] | None = None
    slices: tuple[str, ...] = ()  # Architectures in a universal binary

    @property
    def is_universal(self) -> bool:
        return len(self.slices) > 1

    def describe(self) -> str:
        bits: list[str] = [f"{self.format}", f"{self.os}/{self.arch}"]
        if self.libc:
            bits.append(f"libc={self.libc}")
        if self.macos_min:
            bits.append(f"minos={self.macos_min[0]}.{self.macos_min[1]}")
        if self.is_universal:
            bits.append(f"slices={'+'.join(self.slices)}")
        return ", ".join(bits)


def inspect_binary(path: Path) -> BinaryInfo:
    """Read `path` and determine which platform it targets."""
    path = Path(path)
    if not path.exists():
        raise InspectionError(f"no such file: {path}")
    if path.is_dir():
        raise InspectionError(f"expected a file, got a directory: {path}")

    try:
        with path.open("rb") as fh:
            head: bytes = fh.read(0x1000)
    except OSError as exc:  # pragma: no cover - unreadable file
        raise InspectionError(f"could not read {path}: {exc}") from exc

    if len(head) < 0x04:
        raise InspectionError(f"{path} is too small to be an executable")

    if head.startswith(ELF_MAGIC):
        return _parse_elf(path, head)
    if head.startswith(PE_MAGIC):
        return _parse_pe(path, head)

    magic_be = struct.unpack(">I", head[:0x04])[0]
    if magic_be in (FAT_MAGIC, FAT_MAGIC_64):
        return _parse_macho_universal(path, head, magic_be)
    if magic_be in MACHO_MAGICS:
        return _parse_macho(path, head, magic_be)

    if head.startswith(b"#!"):
        raise InspectionError(
            f"{path} is a script, not a native executable. Scripts are not tied "
            f"to a platform; pass --platform-tag explicitly if you really want "
            f"to package it."
        )
    raise InspectionError(
        f"unrecognised executable format in {path} "
        f"(leading bytes: {head[:0x04].hex()}). Pass --platform-tag to skip detection."
    )


def _parse_elf(path: Path, head: bytes) -> BinaryInfo:
    if len(head) < 0x14:
        raise InspectionError(f"{path}: truncated ELF header")

    ei_class: int = head[0x04]  # 1 = 32-bit, 2 = 64-bit
    ei_data: int = head[0x05]  # 1 = little endian, 2 = big endian
    if ei_class not in (1, 2):
        raise InspectionError(f"{path}: invalid ELF class {ei_class}")
    endian: Literal["<", ">"] = "<" if ei_data == 1 else ">"

    (e_machine,) = struct.unpack_from(f"{endian}H", head, 0x12)
    arch: str | None = ELF_MACHINES.get(e_machine)
    if arch is None:
        raise InspectionError(
            f"{path}: unsupported ELF machine 0x{e_machine:x}. "
            f"Pass --platform-tag to override detection."
        )
    # ppc64 in little-endian mode is a distinct wheel platform.
    if arch == "ppc64" and ei_data == 1:
        arch = "ppc64le"
    # 32-bit ARM ELF is only armv7l for our purposes when it is 32-bit.
    if arch == "armv7l" and ei_class == 2:
        arch = "aarch64"

    libc: str = _elf_libc(path, head, ei_class, endian)
    return BinaryInfo(path=path, format="elf", os="linux", arch=arch, libc=libc)


def _elf_libc(path: Path, head: bytes, ei_class: int, endian: str) -> str:
    """Infer the libc flavour from the ELF program interpreter.

    A binary with no PT_INTERP segment is statically linked and will run under
    any libc, which is the common case for Rust and Go tools.
    """
    try:
        if ei_class == 2:  # ELF64
            e_phoff = struct.unpack_from(f"{endian}Q", head, 0x20)[0]
            e_phentsize = struct.unpack_from(f"{endian}H", head, 0x36)[0]
            e_phnum = struct.unpack_from(f"{endian}H", head, 0x38)[0]
        else:  # ELF32
            e_phoff = struct.unpack_from(f"{endian}I", head, 0x1C)[0]
            e_phentsize = struct.unpack_from(f"{endian}H", head, 0x2A)[0]
            e_phnum = struct.unpack_from(f"{endian}H", head, 0x2C)[0]

        if not e_phoff or not e_phnum or e_phnum > 0x400:
            return "static"

        with path.open("rb") as fh:
            fh.seek(e_phoff)
            table: bytes = fh.read(e_phentsize * e_phnum)
            for i in range(e_phnum):
                entry: bytes = table[i * e_phentsize : (i + 1) * e_phentsize]
                if len(entry) < e_phentsize:
                    break
                (p_type,) = struct.unpack_from(f"{endian}I", entry, 0)
                if p_type != PT_INTERP:
                    continue
                if ei_class == 2:
                    p_offset = struct.unpack_from(f"{endian}Q", entry, 0x08)[0]
                    p_filesz = struct.unpack_from(f"{endian}Q", entry, 0x20)[0]
                else:
                    p_offset = struct.unpack_from(f"{endian}I", entry, 0x04)[0]
                    p_filesz = struct.unpack_from(f"{endian}I", entry, 0x10)[0]
                if p_filesz > 0x1000:
                    return "glibc"
                fh.seek(p_offset)
                interp: str = (
                    fh.read(p_filesz).rstrip(b"\x00").decode("utf-8", "replace")
                )
                return "musl" if "musl" in interp else "glibc"
    except (OSError, struct.error):
        return "glibc"  # be conservative: assume the stricter requirement
    return "static"


def _parse_macho(path: Path, head: bytes, magic_be: int) -> BinaryInfo:
    bits, endian = MACHO_MAGICS[magic_be]
    if len(head) < 0x20:
        raise InspectionError(f"{path}: truncated Mach-O header")

    (cputype,) = struct.unpack_from(f"{endian}I", head, 0x04)
    arch = MACHO_CPUS.get(cputype & 0xFFFFFFFF) or MACHO_CPUS.get(cputype)
    if arch is None:
        raise InspectionError(
            f"{path}: unsupported Mach-O cputype 0x{cputype & 0xFFFFFFFF:x}. "
            f"Pass --platform-tag to override detection."
        )

    (ncmds,) = struct.unpack_from(f"{endian}I", head, 0x10)
    lc_start: Literal[0x1C, 0x20] = 0x20 if bits == b"\x40" else 0x1C
    macos_min: tuple[int, int] | None = _macho_min_version(
        head, endian, ncmds, lc_start
    )

    return BinaryInfo(
        path=path,
        format="macho",
        os="macos",
        arch=arch,
        macos_min=macos_min,
        slices=(arch,),
    )


def _macho_min_version(
    head: bytes, endian: str, ncmds: int, offset: int
) -> tuple[int, int] | None:
    """Walk the load commands looking for a deployment target."""
    for _ in range(min(ncmds, 0x200)):
        if offset + 0x08 > len(head):
            return None
        cmd, cmdsize = struct.unpack_from(f"{endian}II", head, offset)
        if cmdsize < 0x08:
            return None
        version: int | None = None
        if cmd == LC_BUILD_VERSION and offset + 0x10 <= len(head):
            (version,) = struct.unpack_from(f"{endian}I", head, offset + 0x0C)
        elif cmd == LC_VERSION_MIN_MACOSX and offset + 0x0C <= len(head):
            (version,) = struct.unpack_from(f"{endian}I", head, offset + 0x08)
        if version is not None:
            return ((version >> 0x10) & 0xFFFF, (version >> 0x08) & 0xFF)
        offset += cmdsize
    return None


def _parse_macho_universal(path: Path, head: bytes, magic_be: int) -> BinaryInfo:
    """Parse a fat (universal) Mach-O container and summarise its slices."""
    is_64: bool = magic_be == FAT_MAGIC_64
    (nfat,) = struct.unpack_from(">I", head, 0x04)
    if nfat == 0 or nfat > 0x20:
        # 0xCAFEBABE is also the Java class-file magic; a slice count is
        # the cheapest way to tell the two apart.
        raise InspectionError(
            f"{path}: looks like a fat Mach-O header but declares {nfat} slices; "
            f"it is probably not a Mach-O binary at all."
        )

    entry_size: Literal[0x20, 0x14] = 0x20 if is_64 else 0x14
    arches: list[str] = []
    macos_min: tuple[int, int] | None = None
    for i in range(nfat):
        off: int = 0x08 + i * entry_size
        if off + entry_size > len(head):
            break
        (cputype,) = struct.unpack_from(">i", head, off)
        arch: str | None = MACHO_CPUS.get(cputype & 0xFFFFFFFF)
        if arch is None:
            continue
        arches.append(arch)
        if macos_min is None:
            macos_min = _slice_min_version(path, head, off, is_64)

    if not arches:
        raise InspectionError(f"{path}: fat Mach-O contains no recognised slices")

    return BinaryInfo(
        path=path,
        format="macho-universal",
        os="macos",
        arch=arches[0],
        macos_min=macos_min,
        slices=tuple(dict.fromkeys(arches)),
    )


def _slice_min_version(
    path: Path, head: bytes, entry_off: int, is_64: bool
) -> tuple[int, int] | None:
    """Read the deployment target out of one slice of a fat binary."""
    try:
        if is_64:
            (slice_off,) = struct.unpack_from(">Q", head, entry_off + 0x08)
        else:
            (slice_off,) = struct.unpack_from(">I", head, entry_off + 0x08)
        with path.open("rb") as fh:
            fh.seek(slice_off)
            sub: bytes = fh.read(0x1000)
        if len(sub) < 0x20:
            return None
        magic_be: int = struct.unpack(">I", sub[:0x04])[0]
        if magic_be not in MACHO_MAGICS:
            return None
        bits, endian = MACHO_MAGICS[magic_be]
        (ncmds,) = struct.unpack_from(f"{endian}I", sub, 0x10)
        return _macho_min_version(sub, endian, ncmds, 0x20 if bits == b"\x40" else 0x1C)
    except (OSError, struct.error):
        return None


# --------------------------------------------------------------------------
# PE
# --------------------------------------------------------------------------


def _parse_pe(path: Path, head: bytes) -> BinaryInfo:
    if len(head) < 0x40:
        raise InspectionError(f"{path}: truncated DOS header")
    (e_lfanew,) = struct.unpack_from("<I", head, 0x3C)
    if e_lfanew + 0x06 > len(head):
        raise InspectionError(f"{path}: PE header lies beyond the header window")
    if head[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        raise InspectionError(f"{path}: MZ header without a PE signature")

    (machine,) = struct.unpack_from("<H", head, e_lfanew + 0x04)
    arch: str | None = PE_MACHINES.get(machine)
    if arch is None:
        raise InspectionError(
            f"{path}: unsupported PE machine 0x{machine:x}. "
            f"Pass --platform-tag to override detection."
        )
    return BinaryInfo(path=path, format="pe", os="windows", arch=arch)
