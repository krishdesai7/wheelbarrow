"""Map an inspected binary onto a PEP 425 wheel platform tag.

The wheels we produce contain no compiled Python extension, so the interpreter
and ABI tags stay at `py3-none`; only the platform tag is constrained. That
combination is what lets a single wheel serve every CPython on a given OS and
architecture.
"""

from typing import TYPE_CHECKING, Final

from .errors import InspectionError

if TYPE_CHECKING:
    from .probe import BinaryInfo

#: Oldest glibc we claim compatibility with, per architecture. 2.17 is the
#: manylinux2014 baseline and covers essentially every live distro; the newer
#: architectures never shipped with a glibc that old.
DEFAULT_GLIBC: Final[dict[str, str]] = {
    "x86_64": "2_17",
    "i686": "2_17",
    "aarch64": "2_17",
    "ppc64le": "2_17",
    "s390x": "2_17",
    "armv7l": "2_17",
    "riscv64": "2_31",
    "loongarch64": "2_36",
}

DEFAULT_MUSL: Final[str] = "1_2"

#: Deployment target assumed when a Mach-O binary does not record one.
DEFAULT_MACOS_MIN: Final[dict[str, tuple[int, int]]] = {
    "arm64": (11, 0),
    "x86_64": (10, 12),
}

WINDOWS_TAGS: Final[dict[str, str]] = {
    "x86_64": "win_amd64",
    "i686": "win32",
    "arm64": "win_arm64",
    "armv7l": "win_arm32",
}


def platform_tag(
    info: BinaryInfo,
    *,
    glibc_version: str | None = None,
    macos_min: tuple[int, int] | None = None,
    universal2: bool = False,
) -> str:
    """Return the wheel platform tag matching `info`."""
    if info.os == "any":
        # A script holds no machine code, so nothing about the wheel's contents
        # constrains where it may be installed. Its interpreter does, but there
        # is no tag for "needs /bin/sh"; the closest honest answer is `any`,
        # and `--platform-tag` is there to narrow it.
        return "any"
    if info.os == "linux":
        return _linux_tag(info, glibc_version)
    if info.os == "macos":
        return _macos_tag(info, macos_min, universal2)
    if info.os == "windows":
        tag: str | None = WINDOWS_TAGS.get(info.arch)
        if tag is None:
            raise InspectionError(
                f"no wheel platform tag for Windows/{info.arch}; "
                f"pass --platform-tag explicitly"
            )
        return tag
    # Reachable for the BSDs and Solaris, whose binaries are ELF and so look
    # Linux-shaped until EI_OSABI is read. Python packaging defines platform
    # tags for Linux, macOS and Windows only, so there is nothing honest to
    # return; refusing beats shipping a FreeBSD binary as manylinux.
    raise InspectionError(
        f"no wheel platform tag exists for {info.os}; Python packaging defines "
        f"tags for Linux, macOS and Windows only. Pass --platform-tag explicitly "
        f"to package it anyway."
    )


def _linux_tag(info: BinaryInfo, glibc_version: str | None) -> str:
    arch: str = info.arch
    musl: str = f"musllinux_{DEFAULT_MUSL}_{arch}"
    if info.libc == "musl":
        return musl

    version: str | None = glibc_version or _glibc_baseline(info)
    if version is None:
        raise InspectionError(
            f"no manylinux baseline known for {arch}; pass --platform-tag explicitly"
        )
    many: str = f"manylinux_{_normalise_glibc(version)}_{arch}"
    if info.libc != "static":
        return many

    # A static binary depends on no libc at all, so it satisfies both families.
    # A tag is a statement about where installers may *place* the wheel, not
    # about where the code can run: pip on Alpine accepts only musllinux, so
    # tagging this manylinux alone would withhold the wheel from exactly the
    # systems a static build exists to serve. PEP 425 compressed tag sets let
    # one wheel say both, which is the only answer that is not a lie by
    # omission.
    return many


def _glibc_baseline(info: BinaryInfo) -> str | None:
    """The manylinux floor to claim when the caller did not name one.

    A measured requirement can only raise the floor, never lower it. Needing
    no symbol newer than 2.5 is not evidence that a binary works on a 2.5
    system -- the rest of the platform has moved too -- so the default stays
    a lower bound.
    """
    default: str | None = DEFAULT_GLIBC.get(info.arch)
    if info.glibc_min is None:
        return default
    measured: str = f"{info.glibc_min[0]}_{info.glibc_min[1]}"
    if default is None:
        return measured
    return default


def _normalise_glibc(version: str) -> str:
    """Accept both `2.17` and `2_17` spellings."""
    return version.strip().replace(".", "_")


def _macos_tag(
    info: BinaryInfo, override: tuple[int, int] | None, universal2: bool
) -> str:
    arch: str = info.arch
    if universal2 or (info.is_universal and {"arm64", "x86_64"} <= set(info.slices)):
        arch_tag: str = "universal2"
        # universal2 wheels are only meaningful from macOS 11 onwards.
        minimum: tuple[int, int] = override or _max_version(info.macos_min, (11, 0))
    else:
        arch_tag = arch
        minimum = override or info.macos_min or DEFAULT_MACOS_MIN.get(arch, (10, 12))

    major, minor = minimum
    # From macOS 11 the platform tag drops the minor version entirely.
    if major >= 11:
        minor = 0
    return f"macosx_{major}_{minor}_{arch_tag}"


def _max_version(a: tuple[int, int] | None, b: tuple[int, int]) -> tuple[int, int]:
    return b if a is None else max(a, b)


def full_tag(platform: str, *, python: str = "py3", abi: str = "none") -> str:
    """Compose the three-part compatibility tag written into WHEEL."""
    return f"{python}-{abi}-{platform}"
