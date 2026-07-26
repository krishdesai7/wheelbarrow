"""End-to-end build tests, including cross-platform packaging."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from wheelbarrow.builder import build_package
from wheelbarrow.probe import inspect_binary
from wheelbarrow.scaffold import make_spec
from wheelbarrow.tags import platform_tag

from .conftest import make_elf, make_pe


def build(binary: Path, out: Path, *, keep_project: Path | None = None, **overrides):
    info = inspect_binary(binary)
    spec = make_spec(
        name=overrides.pop("name", "demo-bin"),
        version=overrides.pop("version", "1.2.3"),
        binary_name=binary.name,
        platform_tag=platform_tag(info),
        **overrides,
    )
    return build_package(binary, spec, out, keep_project=keep_project)


def entry_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0o7777


class TestCrossPlatformBuild:
    """The wheel's tag must follow the binary, not the build machine."""

    def test_linux_binary_gets_a_manylinux_wheel(self, write_binary, tmp_path):
        binary = write_binary("tool", make_elf(0x3E))
        result = build(binary, tmp_path / "dist")

        assert result.tag == "py3-none-manylinux_2_17_x86_64"
        assert result.wheel.name == "demo_bin-1.2.3-py3-none-manylinux_2_17_x86_64.whl"

    def test_aarch64_musl_binary(self, write_binary, tmp_path):
        from .conftest import MUSL_INTERP

        binary = write_binary("tool", make_elf(0xB7, interp=MUSL_INTERP))
        result = build(binary, tmp_path / "dist")
        assert result.tag == "py3-none-musllinux_1_2_aarch64"

    def test_windows_binary_gets_a_win_amd64_wheel(self, write_binary, tmp_path):
        binary = write_binary("tool.exe", make_pe(0x8664))
        result = build(binary, tmp_path / "dist")

        assert result.tag == "py3-none-win_amd64"
        assert result.wheel.name.endswith("-py3-none-win_amd64.whl")


class TestWheelContents:
    @pytest.fixture
    def wheel(self, write_binary, tmp_path):
        binary = write_binary("tool", make_elf(0x3E))
        result = build(binary, tmp_path / "dist", aliases=["tool", "demo"])
        return result.wheel

    def test_binary_is_stored_executable(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            info = zf.getinfo("demo_bin/bin/tool")
            assert entry_mode(info) == 0o755

    def test_other_files_are_not_executable(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            for item in zf.infolist():
                if item.is_dir() or item.filename == "demo_bin/bin/tool":
                    continue
                assert entry_mode(item) == 0o644, item.filename

    def test_wheel_metadata_declares_the_tag(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            text = zf.read("demo_bin-1.2.3.dist-info/WHEEL").decode()
        assert "Tag: py3-none-manylinux_2_17_x86_64" in text
        # Platform-specific content belongs in platlib, not purelib.
        assert "Root-Is-Purelib: false" in text
        assert text.count("Tag:") == 1

    def test_record_is_complete_and_correct(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            record_name = "demo_bin-1.2.3.dist-info/RECORD"
            rows = list(csv.reader(io.StringIO(zf.read(record_name).decode())))
            listed = set()

            for path, digest, size in rows:
                listed.add(path)
                if path == record_name:
                    assert (digest, size) == ("", "")
                    continue
                data = zf.read(path)
                expected = base64.urlsafe_b64encode(
                    hashlib.sha256(data).digest()
                ).rstrip(b"=")
                assert digest == f"sha256={expected.decode()}"
                assert int(size) == len(data)

            archived = {i.filename for i in zf.infolist() if not i.is_dir()}
            assert archived == listed

    def test_entry_points_cover_every_alias(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            text = zf.read("demo_bin-1.2.3.dist-info/entry_points.txt").decode()
        assert "tool = demo_bin.__main__:main" in text
        assert "demo = demo_bin.__main__:main" in text

    def test_no_stray_record_entry_from_the_backend(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
        assert names.count("demo_bin-1.2.3.dist-info/RECORD") == 1


class TestReproducibility:
    def test_identical_inputs_produce_identical_wheels(self, write_binary, tmp_path):
        binary = write_binary("tool", make_elf(0x3E))
        first = build(binary, tmp_path / "a").wheel.read_bytes()
        second = build(binary, tmp_path / "b").wheel.read_bytes()
        assert first == second


class TestKeepProject:
    def test_generated_project_can_be_kept(self, write_binary, tmp_path):
        binary = write_binary("tool", make_elf(0x3E))
        kept = tmp_path / "kept"
        result = build(binary, tmp_path / "dist", keep_project=kept)
        assert result.project_dir == kept
        assert (kept / "pyproject.toml").is_file()
        assert (kept / "src" / "demo_bin" / "bin" / "tool").is_file()


def install_command(wheel: Path, target: Path) -> list[str] | None:
    """Return an installer invocation, preferring pip and falling back to uv."""
    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"], capture_output=True, check=False
    )
    if probe.returncode == 0:
        base = [sys.executable, "-m", "pip", "install"]
    elif shutil.which("uv"):
        base = [shutil.which("uv"), "pip", "install", "--python", sys.executable]
    else:
        return None
    return [*base, "--no-deps", "--target", str(target), str(wheel)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and exec")
class TestInstalledWheelRuns:
    """Install a wheel wrapping a real executable and run it.

    Uses a tiny shell script rather than a compiled binary so the test stays
    portable; an explicit platform tag bypasses format detection for it.
    """

    def test_round_trip(self, tmp_path):
        binary = tmp_path / "greet"
        binary.write_text("#!/bin/sh\nprintf 'hello %s\\n' \"$1\"\nexit 7\n")
        binary.chmod(0o755)

        spec = make_spec(
            name="greet-bin",
            version="0.1.0",
            binary_name="greet",
            platform_tag="any",
        )
        result = build_package(binary, spec, tmp_path / "dist")

        target = tmp_path / "site"
        command = install_command(result.wheel, target)
        if command is None:
            pytest.skip("neither pip nor uv is available to install the wheel")
        subprocess.run(command, check=True, capture_output=True)

        installed = target / "greet_bin" / "bin" / "greet"
        assert installed.is_file()
        assert stat.S_IMODE(installed.stat().st_mode) & 0o111, (
            "pip did not preserve the executable bit"
        )

        completed = subprocess.run(
            [sys.executable, "-m", "greet_bin", "world"],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
            env={"PYTHONPATH": str(target), "PATH": "/usr/bin:/bin"},
        )
        assert completed.stdout.strip() == "hello world"
        assert completed.returncode == 7, "exit status must propagate"
