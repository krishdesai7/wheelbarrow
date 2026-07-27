"""Tests for binary format inspection."""

from __future__ import annotations

import struct

import pytest

from wheelforge.errors import InspectionError
from wheelforge.probe import inspect_binary

from .conftest import (
    GLIBC_INTERP,
    MUSL_INTERP,
    make_elf,
    make_fat_macho,
    make_macho,
    make_pe,
)

CPU_ARM64 = 0x0100000C
CPU_X86_64 = 0x01000007


class TestElf:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [
            (0x3E, "x86_64"),
            (0xB7, "aarch64"),
            (0x15, "ppc64le"),
            (0x16, "s390x"),
            (0xF3, "riscv64"),
        ],
    )
    def test_architecture(self, write_binary, machine, expected) -> None:
        path = write_binary("tool", make_elf(machine))
        info = inspect_binary(path)
        assert info.os == "linux"
        assert info.format == "elf"
        assert info.arch == expected

    def test_32_bit_x86(self, write_binary) -> None:
        path = write_binary("tool", make_elf(0x03, bits=32))
        assert inspect_binary(path).arch == "i686"

    def test_ppc64_big_endian_is_not_le(self, write_binary) -> None:
        path = write_binary("tool", make_elf(0x15, little=False))
        assert inspect_binary(path).arch == "ppc64"

    def test_glibc_detected_from_interpreter(self, write_binary) -> None:
        path = write_binary("tool", make_elf(0x3E, interp=GLIBC_INTERP))
        assert inspect_binary(path).libc == "glibc"

    def test_musl_detected_from_interpreter(self, write_binary) -> None:
        path = write_binary("tool", make_elf(0x3E, interp=MUSL_INTERP))
        assert inspect_binary(path).libc == "musl"

    def test_static_binary_has_no_interpreter(self, write_binary) -> None:
        path = write_binary("tool", make_elf(0x3E, interp=None))
        assert inspect_binary(path).libc == "static"

    def test_unknown_machine_is_rejected(self, write_binary) -> None:
        path = write_binary("tool", make_elf(0x7777))
        with pytest.raises(InspectionError, match="unsupported ELF machine"):
            inspect_binary(path)


class TestMachO:
    def test_arm64(self, write_binary) -> None:
        path = write_binary("tool", make_macho(CPU_ARM64))
        info = inspect_binary(path)
        assert (info.os, info.arch, info.format) == ("macos", "arm64", "macho")

    def test_x86_64(self, write_binary) -> None:
        path = write_binary("tool", make_macho(CPU_X86_64))
        assert inspect_binary(path).arch == "x86_64"

    def test_minimum_version_is_read(self, write_binary) -> None:
        path = write_binary("tool", make_macho(CPU_ARM64, minos=(12, 3)))
        assert inspect_binary(path).macos_min == (12, 3)

    def test_universal_lists_every_slice(self, write_binary) -> None:
        path = write_binary("tool", make_fat_macho([CPU_X86_64, CPU_ARM64]))
        info = inspect_binary(path)
        assert info.format == "macho-universal"
        assert info.is_universal
        assert set(info.slices) == {"x86_64", "arm64"}

    def test_java_class_file_is_not_mistaken_for_fat_macho(self, write_binary) -> None:
        # Java .class files share the 0xCAFEBABE magic; a bogus slice count is
        # how we tell them apart.
        java = b"\xca\xfe\xba\xbe" + b"\x00\x00\x00\x41" + b"\x00" * 64
        path = write_binary("Tool.class", java)
        with pytest.raises(InspectionError, match="probably not a Mach-O"):
            inspect_binary(path)


class TestPe:
    @pytest.mark.parametrize(
        ("machine", "expected"),
        [(0x8664, "x86_64"), (0x014C, "i686"), (0xAA64, "arm64")],
    )
    def test_architecture(self, write_binary, machine, expected) -> None:
        path = write_binary("tool.exe", make_pe(machine))
        info = inspect_binary(path)
        assert info.os == "windows"
        assert info.arch == expected

    def test_mz_without_pe_signature(self, write_binary) -> None:
        broken = bytearray(make_pe(0x8664))
        broken[0x80:0x84] = b"XXXX"
        path = write_binary("tool.exe", bytes(broken))
        with pytest.raises(InspectionError, match="without a PE signature"):
            inspect_binary(path)


class TestGlibcFloor:
    """The manylinux baseline is a measurement, not a default, when it can be."""

    def test_the_highest_imported_version_wins(self, write_binary) -> None:
        """Entries are in link order, not version order."""
        image = make_elf(
            0x3E, glibc_versions=["GLIBC_2.2.5", "GLIBC_2.18", "GLIBC_2.14"]
        )
        assert inspect_binary(write_binary("tool", image)).glibc_min == (2, 18)

    def test_numeric_ordering_not_string_ordering(self, write_binary) -> None:
        """`2.9` sorts after `2.34` as text, which would understate the floor."""
        image = make_elf(0x3E, glibc_versions=["GLIBC_2.9", "GLIBC_2.34"])
        assert inspect_binary(write_binary("tool", image)).glibc_min == (2, 34)

    @pytest.mark.parametrize("bits", [32, 64])
    def test_both_elf_classes(self, write_binary, bits) -> None:
        image = make_elf(0x3E, bits=bits, glibc_versions=["GLIBC_2.28"])
        assert inspect_binary(write_binary("tool", image)).glibc_min == (2, 28)

    def test_big_endian_sections(self, write_binary) -> None:
        image = make_elf(0x16, little=False, glibc_versions=["GLIBC_2.28"])
        assert inspect_binary(write_binary("tool", image)).glibc_min == (2, 28)

    def test_no_version_section_reports_nothing(self, write_binary) -> None:
        """Absence must fall back to the default, not to zero."""
        assert inspect_binary(write_binary("tool", make_elf(0x3E))).glibc_min is None

    def test_non_glibc_versions_are_ignored(self, write_binary) -> None:
        image = make_elf(0x3E, glibc_versions=["GCC_3.0", "GLIBC_2.17", "ZLIB_1.2.0"])
        assert inspect_binary(write_binary("tool", image)).glibc_min == (2, 17)

    def test_a_static_binary_is_not_probed(self, write_binary) -> None:
        """It has no dynamic imports, so any section found is not a floor."""
        image = make_elf(0x3E, interp=None, glibc_versions=["GLIBC_2.34"])
        info = inspect_binary(write_binary("tool", image))
        assert info.libc == "static"
        assert info.glibc_min is None

    def test_a_truncated_section_table_does_not_raise(self, write_binary) -> None:
        """Detection is best-effort; a default is better than a crash."""
        image = bytearray(make_elf(0x3E, glibc_versions=["GLIBC_2.28"]))
        struct.pack_into("<Q", image, 0x28, 0xDEADBEEF)  # e_shoff into the void
        assert inspect_binary(write_binary("tool", bytes(image))).glibc_min is None

    def test_describe_reports_the_floor(self, write_binary) -> None:
        image = make_elf(0x3E, glibc_versions=["GLIBC_2.18"])
        assert "glibc>=2.18" in inspect_binary(write_binary("tool", image)).describe()


class TestElfOsAbi:
    """ELF is not a Linux format; `EI_OSABI` is what tells the systems apart."""

    @pytest.mark.parametrize("osabi", [0x00, 0x03])
    def test_sysv_and_gnu_both_mean_linux(self, write_binary, osabi) -> None:
        """Linux toolchains emit 0 far more often than 3."""
        path = write_binary("tool", make_elf(0x3E, osabi=osabi))
        assert inspect_binary(path).os == "linux"

    @pytest.mark.parametrize(
        ("osabi", "expected"),
        [(0x09, "freebsd"), (0x02, "netbsd"), (0x0C, "openbsd"), (0x06, "solaris")],
    )
    def test_other_systems_are_named_not_assumed_to_be_linux(
        self, write_binary, osabi, expected
    ) -> None:
        path = write_binary("tool", make_elf(0x3E, osabi=osabi))
        info = inspect_binary(path)
        assert info.os == expected
        assert info.arch == "x86_64", "the architecture is still readable"

    def test_libc_is_not_reported_for_a_non_linux_elf(self, write_binary) -> None:
        """The interpreter names that system's own loader, not a libc flavour."""
        path = write_binary("tool", make_elf(0x3E, osabi=0x09, interp=GLIBC_INTERP))
        assert inspect_binary(path).libc is None

    def test_an_unknown_osabi_is_refused_rather_than_guessed(
        self, write_binary
    ) -> None:
        path = write_binary("tool", make_elf(0x3E, osabi=0x42))
        with pytest.raises(InspectionError, match="unrecognised ELF OS ABI 0x42"):
            inspect_binary(path)


class TestScript:
    """A `#!` file is source text, so there is no architecture to detect."""

    def test_a_script_is_not_tied_to_any_platform(self, shell_script) -> None:
        info = inspect_binary(shell_script)
        assert info.format == "script"
        assert info.os == "any"
        assert info.arch == "any"
        assert info.is_script

    @pytest.mark.parametrize(
        ("first_line", "expected"),
        [
            (b"#!/bin/sh", "/bin/sh"),
            (b"#!/usr/bin/env bash", "/usr/bin/env bash"),
            (b"#!/bin/bash -eu", "/bin/bash -eu"),
            (b"#! /bin/sh", "/bin/sh"),
            (b"#!/usr/bin/env python3\r", "/usr/bin/env python3"),
        ],
    )
    def test_the_interpreter_is_reported_verbatim(
        self, write_binary, first_line, expected
    ) -> None:
        """Reported, not resolved: `env bash` is what the kernel will run."""
        path = write_binary("tool", first_line + b"\nexit 0\n")
        assert inspect_binary(path).interpreter == expected

    def test_a_shebang_alone_is_enough(self, write_binary) -> None:
        """No trailing newline, and nothing else in the file."""
        path = write_binary("tool", b"#!/bin/sh")
        assert inspect_binary(path).interpreter == "/bin/sh"

    def test_undecodable_bytes_do_not_raise(self, write_binary) -> None:
        """The encoding of a script is not ours to assume."""
        path = write_binary("tool", b"#!/bin/\xff\xfesh\nexit 0\n")
        assert inspect_binary(path).is_script

    def test_a_long_first_line_is_truncated_like_the_kernel_does(
        self, write_binary
    ) -> None:
        path = write_binary("tool", b"#!/bin/sh " + b"x" * 0x400 + b"\n")
        interpreter = inspect_binary(path).interpreter
        assert interpreter is not None
        assert len(interpreter) < 0x100

    def test_describe_names_the_interpreter(self, shell_script) -> None:
        assert inspect_binary(shell_script).describe() == "script, interpreter=/bin/sh"


class TestRejections:
    def test_missing_file(self, tmp_path) -> None:
        with pytest.raises(InspectionError, match="no such file"):
            inspect_binary(tmp_path / "nope")

    def test_directory(self, tmp_path) -> None:
        with pytest.raises(InspectionError, match="expected a file"):
            inspect_binary(tmp_path)

    def test_shebang_without_an_interpreter(self, write_binary) -> None:
        path = write_binary("tool", b"#!\necho hi\n")
        with pytest.raises(InspectionError, match="not followed by an interpreter"):
            inspect_binary(path)

    def test_unrecognised_bytes(self, write_binary) -> None:
        path = write_binary("tool", b"just some plain text file contents")
        with pytest.raises(InspectionError, match="unrecognised executable format"):
            inspect_binary(path)

    def test_tiny_file(self, write_binary) -> None:
        path = write_binary("tool", b"ab")
        with pytest.raises(InspectionError, match="too small"):
            inspect_binary(path)
