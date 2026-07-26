"""Tests for binary format inspection."""

from __future__ import annotations

import pytest

from wheelbarrow.errors import InspectionError
from wheelbarrow.probe import inspect_binary

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


class TestRejections:
    def test_missing_file(self, tmp_path) -> None:
        with pytest.raises(InspectionError, match="no such file"):
            inspect_binary(tmp_path / "nope")

    def test_directory(self, tmp_path) -> None:
        with pytest.raises(InspectionError, match="expected a file"):
            inspect_binary(tmp_path)

    def test_shell_script_gets_a_useful_message(self, write_binary) -> None:
        path = write_binary("tool", b"#!/bin/sh\necho hi\n")
        with pytest.raises(InspectionError, match="is a script"):
            inspect_binary(path)

    def test_unrecognised_bytes(self, write_binary) -> None:
        path = write_binary("tool", b"just some plain text file contents")
        with pytest.raises(InspectionError, match="unrecognised executable format"):
            inspect_binary(path)

    def test_tiny_file(self, write_binary) -> None:
        path = write_binary("tool", b"ab")
        with pytest.raises(InspectionError, match="too small"):
            inspect_binary(path)
