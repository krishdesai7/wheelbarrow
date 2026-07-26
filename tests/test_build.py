"""End-to-end build tests, including cross-platform packaging."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from wheelbarrow.builder import build_package
from wheelbarrow.probe import inspect_binary
from wheelbarrow.scaffold import (
    Launcher,
    archive_executables,
    make_spec,
    staged_paths,
)
from wheelbarrow.tags import platform_tag
from wheelbarrow.wheelfix import retag_wheel

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
    @pytest.fixture(params=[Launcher.DIRECT, Launcher.SHIM])
    def built(self, request, write_binary, tmp_path):
        binary = write_binary("tool", make_elf(0x3E))
        return build(
            binary,
            tmp_path / "dist",
            aliases=["tool", "demo"],
            launcher=request.param,
        )

    @pytest.fixture
    def wheel(self, built):
        return built.wheel

    def test_binary_is_stored_executable(self, built):
        expected = archive_executables(built.spec)
        with zipfile.ZipFile(built.wheel) as zf:
            names = set(zf.namelist())
            assert expected <= names, f"missing {expected - names}"
            for path in expected:
                assert entry_mode(zf.getinfo(path)) == 0o755, path

    def test_other_files_are_not_executable(self, built):
        executables = archive_executables(built.spec)
        with zipfile.ZipFile(built.wheel) as zf:
            for item in zf.infolist():
                if item.is_dir() or item.filename in executables:
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

    def test_binary_is_not_stored_twice(self, built):
        """The staging layout must not let the binary in as package data too."""
        with zipfile.ZipFile(built.wheel) as zf:
            big = [i for i in zf.infolist() if i.file_size > 1024]
        assert len(big) == len(archive_executables(built.spec))

    def test_no_stray_record_entry_from_the_backend(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
        assert names.count("demo_bin-1.2.3.dist-info/RECORD") == 1


class TestLauncherLayouts:
    @pytest.fixture
    def elf(self, write_binary):
        return write_binary("tool", make_elf(0x3E))

    def test_direct_puts_the_binary_in_data_scripts(self, elf, tmp_path):
        result = build(elf, tmp_path / "dist", aliases=["tool"])
        with zipfile.ZipFile(result.wheel) as zf:
            names = zf.namelist()
        assert "demo_bin-1.2.3.data/scripts/tool" in names
        assert "demo_bin/bin/tool" not in names
        # A console script of the same name would clobber the binary.
        assert "demo_bin-1.2.3.dist-info/entry_points.txt" not in names

    def test_direct_names_the_script_after_the_alias(self, elf, tmp_path):
        result = build(elf, tmp_path / "dist", aliases=["renamed"])
        with zipfile.ZipFile(result.wheel) as zf:
            assert "demo_bin-1.2.3.data/scripts/renamed" in zf.namelist()

    def test_shim_keeps_the_binary_in_the_package(self, elf, tmp_path):
        result = build(elf, tmp_path / "dist", launcher=Launcher.SHIM)
        with zipfile.ZipFile(result.wheel) as zf:
            names = zf.namelist()
            text = zf.read("demo_bin-1.2.3.dist-info/entry_points.txt").decode()
        assert "demo_bin/bin/tool" in names
        assert not any(".data/scripts" in n for n in names)
        assert "tool = demo_bin.__main__:main" in text

    def test_shim_shares_one_copy_across_aliases(self, elf, tmp_path):
        result = build(
            elf, tmp_path / "dist", aliases=["tool", "demo"], launcher=Launcher.SHIM
        )
        with zipfile.ZipFile(result.wheel) as zf:
            text = zf.read("demo_bin-1.2.3.dist-info/entry_points.txt").decode()
            binaries = [
                i.filename
                for i in zf.infolist()
                if not i.is_dir() and i.filename.startswith("demo_bin/bin/")
            ]
        assert binaries == ["demo_bin/bin/tool"]
        assert "tool = demo_bin.__main__:main" in text
        assert "demo = demo_bin.__main__:main" in text


class TestWheelJsonRewrite:
    """uv's non-standard WHEEL.json must not contradict WHEEL after retagging."""

    def test_wheel_json_is_kept_in_step(self, write_binary, tmp_path):
        binary = write_binary("tool", make_elf(0x3E))
        result = build(binary, tmp_path / "dist")

        # Inject a WHEEL.json as `uv build` would, then retag again.
        staged = tmp_path / "staged"
        staged.mkdir()
        source = staged / "demo_bin-1.2.3-py3-none-any.whl"
        with zipfile.ZipFile(result.wheel) as src, zipfile.ZipFile(source, "w") as dst:
            for item in src.infolist():
                if item.is_dir():
                    continue
                dst.writestr(item.filename, src.read(item.filename))
            dst.writestr(
                "demo_bin-1.2.3.dist-info/WHEEL.json",
                json.dumps(
                    {
                        "wheel-version": "1.0",
                        "generator": "uv 0.11.32",
                        "root-is-purelib": True,
                        "tags": ["py3-none-any"],
                        "unknown-key": "preserved",
                    }
                ),
            )

        retagged = retag_wheel(
            source, tag="py3-none-manylinux_2_17_x86_64", executable_paths=set()
        )
        with zipfile.ZipFile(retagged.path) as zf:
            payload = json.loads(
                zf.read("demo_bin-1.2.3.dist-info/WHEEL.json").decode()
            )
        assert payload["tags"] == ["py3-none-manylinux_2_17_x86_64"]
        assert payload["root-is-purelib"] is False
        assert payload["unknown-key"] == "preserved"


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
        assert (kept / "src" / "demo_bin" / "__main__.py").is_file()
        for staged in staged_paths(result.spec, kept):
            assert staged.is_file()


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

    @pytest.fixture(params=[Launcher.DIRECT, Launcher.SHIM])
    def installed(self, request, tmp_path):
        """Build, install and return where the executable landed."""
        binary = tmp_path / "greet"
        binary.write_text("#!/bin/sh\nprintf 'hello %s\\n' \"$1\"\nexit 7\n")
        binary.chmod(0o755)

        spec = make_spec(
            name="greet-bin",
            version="0.1.0",
            binary_name="greet",
            platform_tag="any",
            launcher=request.param,
        )
        result = build_package(binary, spec, tmp_path / "dist")

        target = tmp_path / "site"
        command = install_command(result.wheel, target)
        if command is None:
            pytest.skip("neither pip nor uv is available to install the wheel")
        subprocess.run(command, check=True, capture_output=True)

        # Direct-mode wheels put the binary in `.data/scripts`, which installers
        # unpack into a `bin/` beside the packages under `--target`.
        if request.param is Launcher.DIRECT:
            executable = target / "bin" / "greet"
        else:
            executable = target / "greet_bin" / "bin" / "greet"
        return target, executable

    def test_executable_bit_survives_installation(self, installed):
        _, executable = installed
        assert executable.is_file()
        assert stat.S_IMODE(executable.stat().st_mode) & 0o111, (
            "the installer did not preserve the executable bit"
        )

    def test_running_it_directly_works(self, installed):
        _, executable = installed
        completed = subprocess.run(
            [str(executable), "world"], capture_output=True, text=True, check=False
        )
        assert completed.stdout.strip() == "hello world"
        assert completed.returncode == 7, "exit status must propagate"

    def test_python_dash_m_works(self, installed):
        target, _ = installed
        completed = subprocess.run(
            [sys.executable, "-m", "greet_bin", "world"],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # a resolution bug could otherwise exec-loop forever
            env={"PYTHONPATH": str(target), "PATH": "/usr/bin:/bin"},
        )
        assert completed.stdout.strip() == "hello world"
        assert completed.returncode == 7, "exit status must propagate"

    def test_binary_path_resolves_to_the_installed_file(self, installed):
        target, executable = installed
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from greet_bin import binary_path; print(binary_path())",
            ],
            cwd=target,
            capture_output=True,
            text=True,
            check=False,
            env={"PYTHONPATH": str(target), "PATH": "/usr/bin:/bin"},
        )
        assert completed.stdout.strip() == str(executable), completed.stderr
