"""Tests for platform tag derivation."""

from pathlib import Path

import pytest

from wheelbarrow.errors import InspectionError
from wheelbarrow.probe import BinaryInfo
from wheelbarrow.tags import full_tag, platform_tag


def linux(
    arch: str, libc: str = "glibc", glibc_min: tuple[int, int] | None = None
) -> BinaryInfo:
    return BinaryInfo(
        path=Path(),
        format="elf",
        os="linux",
        arch=arch,
        libc=libc,
        glibc_min=glibc_min,
    )


def macos(arch: str, minos: tuple[int, int] | None = None, slices=()) -> BinaryInfo:
    return BinaryInfo(
        path=Path(),
        format="macho",
        os="macos",
        arch=arch,
        macos_min=minos,
        slices=slices or (arch,),
    )


def windows(arch: str) -> BinaryInfo:
    return BinaryInfo(path=Path(), format="pe", os="windows", arch=arch)


def script(interpreter: str = "/bin/sh") -> BinaryInfo:
    return BinaryInfo(
        path=Path(),
        format="script",
        os="any",
        arch="any",
        interpreter=interpreter,
    )


class TestLinux:
    @pytest.mark.parametrize(
        ("arch", "expected"),
        [
            ("x86_64", "manylinux_2_17_x86_64"),
            ("aarch64", "manylinux_2_17_aarch64"),
            ("i686", "manylinux_2_17_i686"),
            ("riscv64", "manylinux_2_31_riscv64"),
        ],
    )
    def test_glibc_defaults(self, arch, expected) -> None:
        assert platform_tag(linux(arch)) == expected

    def test_musl_uses_musllinux(self) -> None:
        assert platform_tag(linux("x86_64", "musl")) == "musllinux_1_2_x86_64"

    def test_static_binaries_claim_both_libc_families(self) -> None:
        """A tag says where an installer may place a wheel, not where the code
        can run. pip on Alpine accepts only musllinux, so a manylinux-only tag
        would withhold a static binary from the systems it best serves."""
        assert (
            platform_tag(linux("x86_64", "static"))
            == "manylinux_2_17_x86_64.musllinux_1_2_x86_64"
        )

    @pytest.mark.parametrize("arch", ["aarch64", "i686", "armv7l", "riscv64"])
    def test_both_halves_agree_on_the_architecture(self, arch) -> None:
        many, musl = platform_tag(linux(arch, "static")).split(".")
        assert many.endswith(f"_{arch}")
        assert musl == f"musllinux_1_2_{arch}"

    def test_dynamic_musl_stays_musl_only(self) -> None:
        """It needs a musl loader, so manylinux would be a false claim."""
        assert platform_tag(linux("x86_64", "musl")) == "musllinux_1_2_x86_64"

    def test_dynamic_glibc_stays_manylinux_only(self) -> None:
        assert platform_tag(linux("x86_64", "glibc")) == "manylinux_2_17_x86_64"

    @pytest.mark.parametrize("spelling", ["2.28", "2_28"])
    def test_glibc_override_accepts_both_spellings(self, spelling) -> None:
        tag = platform_tag(linux("x86_64"), glibc_version=spelling)
        assert tag == "manylinux_2_28_x86_64"

    def test_unknown_arch_is_rejected(self) -> None:
        with pytest.raises(InspectionError, match="no manylinux baseline"):
            platform_tag(linux("sparc64"))


class TestMeasuredGlibcFloor:
    """A measured requirement beats the default, but only upwards."""

    def test_a_higher_requirement_raises_the_floor(self) -> None:
        """The default 2.17 is exactly CentOS 7's glibc, so being one version
        too low produces a wheel that installs there and dies at exec."""
        info = linux("x86_64", "glibc", glibc_min=(2, 18))
        assert platform_tag(info) == "manylinux_2_18_x86_64"

    def test_a_lower_requirement_does_not_lower_the_floor(self) -> None:
        """Importing nothing newer than 2.5 is not evidence that the binary
        works on a 2.5 system; the rest of that platform has moved on too."""
        info = linux("x86_64", "glibc", glibc_min=(2, 5))
        assert platform_tag(info) == "manylinux_2_17_x86_64"

    def test_the_comparison_is_numeric_not_lexicographic(self) -> None:
        """`2.9` must not read as newer than `2.34`."""
        assert platform_tag(linux("x86_64", "glibc", (2, 9))) == "manylinux_2_17_x86_64"
        assert platform_tag(linux("x86_64", "glibc", (2, 34))) == "manylinux_2_34_x86_64"

    def test_an_explicit_override_still_wins(self) -> None:
        info = linux("x86_64", "glibc", glibc_min=(2, 34))
        assert platform_tag(info, glibc_version="2.28") == "manylinux_2_28_x86_64"

    def test_a_measured_floor_on_an_arch_with_no_default(self) -> None:
        info = linux("sparc64", "glibc", glibc_min=(2, 30))
        assert platform_tag(info) == "manylinux_2_30_sparc64"


class TestMacos:
    def test_uses_recorded_minimum(self) -> None:
        assert platform_tag(macos("arm64", (12, 3))) == "macosx_12_0_arm64"

    def test_minor_version_is_dropped_from_macos_11_onwards(self) -> None:
        # PEP 600-era convention: macOS 11+ tags always carry a 0 minor.
        assert platform_tag(macos("arm64", (14, 5))) == "macosx_14_0_arm64"

    def test_legacy_minor_version_is_preserved(self) -> None:
        assert platform_tag(macos("x86_64", (10, 12))) == "macosx_10_12_x86_64"

    def test_defaults_per_architecture(self) -> None:
        assert platform_tag(macos("arm64")) == "macosx_11_0_arm64"
        assert platform_tag(macos("x86_64")) == "macosx_10_12_x86_64"

    def test_universal_binary_is_tagged_universal2(self) -> None:
        info = macos("x86_64", (11, 0), slices=("x86_64", "arm64"))
        assert platform_tag(info) == "macosx_11_0_universal2"

    def test_universal2_never_goes_below_11(self) -> None:
        info = macos("x86_64", (10, 12), slices=("x86_64", "arm64"))
        assert platform_tag(info) == "macosx_11_0_universal2"

    def test_explicit_override_wins(self) -> None:
        tag = platform_tag(macos("arm64", (14, 0)), macos_min=(13, 0))
        assert tag == "macosx_13_0_arm64"


class TestWindows:
    @pytest.mark.parametrize(
        ("arch", "expected"),
        [("x86_64", "win_amd64"), ("i686", "win32"), ("arm64", "win_arm64")],
    )
    def test_tags(self, arch, expected) -> None:
        assert platform_tag(windows(arch)) == expected


class TestUntaggableSystems:
    """Wheel tags exist for Linux, macOS and Windows. Nothing else."""

    @pytest.mark.parametrize("system", ["freebsd", "netbsd", "openbsd", "solaris"])
    def test_refused_rather_than_passed_off_as_linux(self, system) -> None:
        info = BinaryInfo(path=Path(), format="elf", os=system, arch="x86_64")
        with pytest.raises(InspectionError, match="no wheel platform tag exists"):
            platform_tag(info)

    def test_the_error_points_at_the_way_out(self) -> None:
        info = BinaryInfo(path=Path(), format="elf", os="freebsd", arch="x86_64")
        with pytest.raises(InspectionError, match="--platform-tag"):
            platform_tag(info)


class TestScript:
    def test_a_script_gets_the_any_tag(self) -> None:
        assert platform_tag(script()) == "any"

    def test_the_interpreter_does_not_change_the_tag(self) -> None:
        """There is no wheel tag for "needs bash", so none is invented."""
        assert platform_tag(script("/usr/bin/env bash")) == "any"

    def test_binary_options_are_ignored(self) -> None:
        assert platform_tag(script(), glibc_version="2.28", macos_min=(12, 0)) == "any"

    def test_the_full_tag_is_the_pure_python_one(self) -> None:
        assert full_tag(platform_tag(script())) == "py3-none-any"


def test_full_tag_composition() -> None:
    assert full_tag("win_amd64") == "py3-none-win_amd64"
    assert full_tag("win_amd64", python="py38") == "py38-none-win_amd64"
