"""End-to-end build tests, including cross-platform packaging."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests intentionally execute built files
import sys
import zipfile
from typing import TYPE_CHECKING

import pytest

from wheelbarrow.builder import BuildResult, build_package, build_packages
from wheelbarrow.errors import BuildError
from wheelbarrow.probe import BinaryInfo, inspect_binary
from wheelbarrow.scaffold import (
    Launcher,
    PackageSpec,
    archive_executables,
    make_spec,
    staged_paths,
)
from wheelbarrow.tags import platform_tag
from wheelbarrow.wheelfix import retag_wheel

from .conftest import make_elf, make_pe

if TYPE_CHECKING:
    from pathlib import Path


def build(
    binary: Path,
    out: Path,
    *,
    keep_project: Path | None = None,
    overwrite: bool = False,
    **overrides,
) -> BuildResult:
    info: BinaryInfo = inspect_binary(binary)
    spec: PackageSpec = make_spec(
        name=overrides.pop("name", "demo-bin"),
        version=overrides.pop("version", "1.2.3"),
        binary_name=binary.name,
        platform_tag=overrides.pop("platform_tag", None) or platform_tag(info),
        **overrides,
    )
    return build_package(
        binary, spec, out, keep_project=keep_project, overwrite=overwrite
    )


def entry_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0o7777


class TestCrossPlatformBuild:
    """The wheel's tag must follow the binary, not the build machine."""

    def test_linux_binary_gets_a_manylinux_wheel(self, write_binary, tmp_path) -> None:
        binary = write_binary("tool", make_elf(0x3E))
        result: BuildResult = build(binary, tmp_path / "dist")

        assert result.tag == "py3-none-manylinux_2_17_x86_64"
        assert result.wheel.name == "demo_bin-1.2.3-py3-none-manylinux_2_17_x86_64.whl"

    def test_aarch64_musl_binary(self, write_binary, tmp_path) -> None:
        from .conftest import MUSL_INTERP

        binary = write_binary("tool", make_elf(0xB7, interp=MUSL_INTERP))
        result: BuildResult = build(binary, tmp_path / "dist")
        assert result.tag == "py3-none-musllinux_1_2_aarch64"

    def test_windows_binary_gets_a_win_amd64_wheel(
        self, write_binary, tmp_path
    ) -> None:
        binary = write_binary("tool.exe", make_pe(0x8664))
        result: BuildResult = build(binary, tmp_path / "dist")

        assert result.tag == "py3-none-win_amd64"
        assert result.wheel.name.endswith("-py3-none-win_amd64.whl")


class TestWheelContents:
    @pytest.fixture(params=[Launcher.DIRECT, Launcher.SHIM])
    def built(self, request, write_binary, tmp_path) -> BuildResult:
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

    def test_binary_is_stored_executable(self, built) -> None:
        expected = archive_executables(built.spec)
        with zipfile.ZipFile(built.wheel) as zf:
            names = set(zf.namelist())
            assert expected <= names, f"missing {expected - names}"
            for path in expected:
                assert entry_mode(zf.getinfo(path)) == 0o755, path

    def test_other_files_are_not_executable(self, built) -> None:
        executables = archive_executables(built.spec)
        with zipfile.ZipFile(built.wheel) as zf:
            for item in zf.infolist():
                if item.is_dir() or item.filename in executables:
                    continue
                assert entry_mode(item) == 0o644, item.filename

    def test_wheel_metadata_declares_the_tag(self, wheel) -> None:
        with zipfile.ZipFile(wheel) as zf:
            text = zf.read("demo_bin-1.2.3.dist-info/WHEEL").decode()
        assert "Tag: py3-none-manylinux_2_17_x86_64" in text
        # Platform-specific content belongs in platlib, not purelib.
        assert "Root-Is-Purelib: false" in text
        assert text.count("Tag:") == 1

    def test_record_is_complete_and_correct(self, wheel) -> None:
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

    def test_binary_is_not_stored_twice(self, built) -> None:
        """The staging layout must not let the binary in as package data too.

        Counted by content rather than by size: direct mode stores one copy per
        alias on purpose, so the question is whether those bytes appear anywhere
        they were not asked for.
        """
        expected = archive_executables(built.spec)
        with zipfile.ZipFile(built.wheel) as zf:
            wanted = hashlib.sha256(zf.read(next(iter(expected)))).hexdigest()
            digests = [
                hashlib.sha256(zf.read(item.filename)).hexdigest()
                for item in zf.infolist()
                if not item.is_dir()
            ]
        assert digests.count(wanted) == len(expected)

    def test_no_stray_record_entry_from_the_backend(self, wheel) -> None:
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
        assert names.count("demo_bin-1.2.3.dist-info/RECORD") == 1


class TestScriptBuild:
    """Not every tool is machine code; a `#!` script packages the same way."""

    @pytest.fixture(params=[Launcher.DIRECT, Launcher.SHIM])
    def built(self, request, shell_script, tmp_path) -> BuildResult:
        return build(shell_script, tmp_path / "dist", launcher=request.param)

    def test_the_wheel_is_pure_python_tagged(self, built) -> None:
        """Nothing in a script constrains where it can be installed."""
        assert built.tag == "py3-none-any"
        assert built.wheel.name == "demo_bin-1.2.3-py3-none-any.whl"

    def test_purelib_is_declared(self, built) -> None:
        """The counterpart of the `any` tag: no platform-specific content."""
        with zipfile.ZipFile(built.wheel) as zf:
            text = zf.read("demo_bin-1.2.3.dist-info/WHEEL").decode()
        assert "Tag: py3-none-any" in text
        assert "Root-Is-Purelib: true" in text

    def test_the_script_is_stored_executable(self, built) -> None:
        """A script that is not executable is not a command."""
        expected = archive_executables(built.spec)
        with zipfile.ZipFile(built.wheel) as zf:
            assert expected <= set(zf.namelist())
            for path in expected:
                assert entry_mode(zf.getinfo(path)) == 0o755, path

    def test_the_shebang_survives_the_round_trip(self, built) -> None:
        """Installers rewrite `#!python` shebangs; ours must be left alone."""
        with zipfile.ZipFile(built.wheel) as zf:
            for path in archive_executables(built.spec):
                assert zf.read(path).startswith(b"#!/bin/sh\n")

    def test_the_alias_drops_the_extension(self, shell_script, tmp_path) -> None:
        """`tool.sh` should install as `tool`, not `tool.sh`."""
        result: BuildResult = build(shell_script, tmp_path / "dist")
        assert result.spec.aliases == ["tool"]
        with zipfile.ZipFile(result.wheel) as zf:
            assert "demo_bin-1.2.3.data/scripts/tool" in zf.namelist()


class TestLauncherLayouts:
    @pytest.fixture
    def elf(self, write_binary):
        return write_binary("tool", make_elf(0x3E))

    def test_direct_puts_the_binary_in_data_scripts(self, elf, tmp_path) -> None:
        result = build(elf, tmp_path / "dist", aliases=["tool"])
        with zipfile.ZipFile(result.wheel) as zf:
            names = zf.namelist()
        assert "demo_bin-1.2.3.data/scripts/tool" in names
        assert "demo_bin/bin/tool" not in names
        # A console script of the same name would clobber the binary.
        assert "demo_bin-1.2.3.dist-info/entry_points.txt" not in names

    def test_direct_names_the_script_after_the_alias(self, elf, tmp_path) -> None:
        result = build(elf, tmp_path / "dist", aliases=["renamed"])
        with zipfile.ZipFile(result.wheel) as zf:
            assert "demo_bin-1.2.3.data/scripts/renamed" in zf.namelist()

    def test_shim_keeps_the_binary_in_the_package(self, elf, tmp_path) -> None:
        result = build(elf, tmp_path / "dist", launcher=Launcher.SHIM)
        with zipfile.ZipFile(result.wheel) as zf:
            names = zf.namelist()
            text = zf.read("demo_bin-1.2.3.dist-info/entry_points.txt").decode()
        assert "demo_bin/bin/tool" in names
        assert not any(".data/scripts" in n for n in names)
        assert "tool = demo_bin.__main__:main" in text

    def test_shim_shares_one_copy_across_aliases(self, elf, tmp_path) -> None:
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

    @pytest.mark.parametrize(
        ("tag", "pure"),
        [
            ("py3-none-manylinux_2_17_x86_64", False),
            # A script's wheel really is pure, and both files must say so.
            ("py3-none-any", True),
        ],
    )
    def test_wheel_json_is_kept_in_step(
        self, write_binary, tmp_path, tag, pure
    ) -> None:
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

        retagged = retag_wheel(source, tag=tag, executable_paths=set())
        with zipfile.ZipFile(retagged.path) as zf:
            payload = json.loads(
                zf.read("demo_bin-1.2.3.dist-info/WHEEL.json").decode()
            )
            meta = zf.read("demo_bin-1.2.3.dist-info/WHEEL").decode()
        assert payload["tags"] == [tag]
        assert payload["root-is-purelib"] is pure
        assert payload["unknown-key"] == "preserved"
        # The whole point of rewriting WHEEL.json: the two must agree.
        assert f"Root-Is-Purelib: {str(pure).lower()}" in meta


class TestOutputCollisions:
    """Two inputs can resolve to one tag; the second must not win silently."""

    def test_a_differing_wheel_of_the_same_name_is_refused(
        self, write_binary, tmp_path
    ) -> None:
        """A glibc build and a static build both answer to manylinux."""
        dist = tmp_path / "dist"
        build(write_binary("a/tool", make_elf(0x3E)), dist, platform_tag="linux_x86_64")
        second = write_binary("b/tool", make_elf(0x3E) + b"different")

        with pytest.raises(BuildError, match="already exists"):
            build(second, dist, platform_tag="linux_x86_64")

    def test_the_first_wheel_is_left_intact(self, write_binary, tmp_path) -> None:
        dist = tmp_path / "dist"
        first = build(
            write_binary("a/tool", make_elf(0x3E)), dist, platform_tag="linux_x86_64"
        )
        original = first.wheel.read_bytes()

        with pytest.raises(BuildError):
            build(
                write_binary("b/tool", make_elf(0x3E) + b"different"),
                dist,
                platform_tag="linux_x86_64",
            )
        assert first.wheel.read_bytes() == original

    def test_an_identical_rebuild_is_not_a_collision(
        self, write_binary, tmp_path
    ) -> None:
        """Builds are reproducible, so the same input twice is a no-op."""
        binary = write_binary("tool", make_elf(0x3E))
        dist = tmp_path / "dist"
        first = build(binary, dist).wheel.read_bytes()
        assert build(binary, dist).wheel.read_bytes() == first

    def test_overwrite_allows_it(self, write_binary, tmp_path) -> None:
        dist = tmp_path / "dist"
        build(write_binary("a/tool", make_elf(0x3E)), dist, platform_tag="linux_x86_64")
        result = build(
            write_binary("b/tool", make_elf(0x3E) + b"different"),
            dist,
            platform_tag="linux_x86_64",
            overwrite=True,
        )
        assert result.wheel.is_file()

    def test_no_intermediate_wheel_is_left_in_the_output(
        self, write_binary, tmp_path
    ) -> None:
        """The backend's untagged `py3-none-any` must never reach `dist/`."""
        dist = tmp_path / "dist"
        build(write_binary("tool", make_elf(0x3E)), dist)
        assert [p.name for p in dist.iterdir()] == [
            "demo_bin-1.2.3-py3-none-manylinux_2_17_x86_64.whl"
        ]


class TestCompressedTagSets:
    """A static binary claims both libc families, in one wheel."""

    @pytest.fixture
    def built(self, write_binary, tmp_path) -> BuildResult:
        # No PT_INTERP: statically linked, so it satisfies glibc and musl both.
        return build(write_binary("tool", make_elf(0x3E, interp=None)), tmp_path / "d")

    def test_the_file_name_carries_the_compressed_set(self, built) -> None:
        assert built.wheel.name == (
            "demo_bin-1.2.3-py3-none-manylinux_2_17_x86_64.musllinux_1_2_x86_64.whl"
        )

    def test_wheel_metadata_expands_it_to_one_tag_per_line(self, built) -> None:
        """PEP 427 takes the expanded form here, unlike the file name."""
        with zipfile.ZipFile(built.wheel) as zf:
            text = zf.read("demo_bin-1.2.3.dist-info/WHEEL").decode()
        assert "Tag: py3-none-manylinux_2_17_x86_64" in text
        assert "Tag: py3-none-musllinux_1_2_x86_64" in text
        assert text.count("Tag:") == 2
        assert "Root-Is-Purelib: false" in text

    def test_the_wheel_is_still_installable_and_correct(self, built) -> None:
        with zipfile.ZipFile(built.wheel) as zf:
            assert zf.testzip() is None
            assert archive_executables(built.spec) <= set(zf.namelist())


class TestBatchBuilds:
    """`build_packages` builds a directory's worth of binaries in one pass."""

    def plan(self, binary: Path, **overrides) -> tuple[Path, PackageSpec]:
        info: BinaryInfo = inspect_binary(binary)
        return binary, make_spec(
            name=overrides.pop("name", "demo-bin"),
            version=overrides.pop("version", "1.2.3"),
            binary_name=binary.name,
            platform_tag=overrides.pop("platform_tag", None) or platform_tag(info),
            **overrides,
        )

    @pytest.fixture
    def two_platforms(self, write_binary) -> list[tuple[Path, PackageSpec]]:
        return [
            self.plan(write_binary("linux/tool", make_elf(0x3E, interp=None))),
            self.plan(write_binary("windows/tool", make_pe(0x8664))),
        ]

    def test_one_wheel_per_binary(self, two_platforms, tmp_path) -> None:
        results = build_packages(two_platforms, tmp_path / "dist")
        assert len(results) == 2
        assert len(list((tmp_path / "dist").glob("*.whl"))) == 2

    def test_they_differ_only_in_platform_tag(self, two_platforms, tmp_path) -> None:
        results = build_packages(two_platforms, tmp_path / "dist")
        tags = {r.tag for r in results}
        assert len(tags) == 2
        assert {r.spec.dist_name for r in results} == {"demo-bin"}
        assert {r.spec.version for r in results} == {"1.2.3"}

    def test_each_result_is_reported_as_it_lands(self, two_platforms, tmp_path) -> None:
        """A long batch must not be silent while it works."""
        seen: list[str] = []
        build_packages(
            two_platforms, tmp_path / "dist", on_built=lambda r: seen.append(r.tag)
        )
        assert len(seen) == 2

    def test_a_batch_wheel_matches_the_same_build_done_alone(
        self, two_platforms, tmp_path
    ) -> None:
        """Batching must be a loop, not a different code path."""
        batched = build_packages(two_platforms, tmp_path / "batch")
        binary, spec = two_platforms[0]
        alone = build_package(binary, spec, tmp_path / "alone")
        assert alone.wheel.read_bytes() == batched[0].wheel.read_bytes()

    def test_kept_projects_do_not_overwrite_each_other(
        self, two_platforms, tmp_path
    ) -> None:
        """One --keep-project directory, so each build needs its own subtree."""
        build_packages(
            two_platforms, tmp_path / "dist", keep_project=tmp_path / "projects"
        )
        kept = sorted(p.name for p in (tmp_path / "projects").iterdir())
        assert len(kept) == 2
        assert all(
            (tmp_path / "projects" / k / "pyproject.toml").is_file() for k in kept
        )

    def test_a_single_plan_keeps_the_project_where_asked(
        self, two_platforms, tmp_path
    ) -> None:
        """One binary means no subdirectory, exactly as before."""
        build_packages(
            two_platforms[:1], tmp_path / "dist", keep_project=tmp_path / "project"
        )
        assert (tmp_path / "project" / "pyproject.toml").is_file()


class TestBatchTagCollisions:
    """Two binaries claiming one wheel name is refused before anything runs."""

    def plan(self, binary: Path) -> tuple[Path, PackageSpec]:
        return binary, make_spec(
            name="demo-bin",
            version="1.2.3",
            binary_name=binary.name,
            platform_tag=platform_tag(inspect_binary(binary)),
        )

    @pytest.fixture
    def clashing(self, write_binary) -> list[tuple[Path, PackageSpec]]:
        """Two different binaries that resolve to the same platform tag."""
        return [
            self.plan(write_binary("a/tool", make_elf(0x3E, interp=None))),
            self.plan(write_binary("b/tool", make_elf(0x3E, interp=None) + b"\x00")),
        ]

    def test_the_batch_is_refused(self, clashing, tmp_path) -> None:
        with pytest.raises(BuildError, match="same platform tag"):
            build_packages(clashing, tmp_path / "dist")

    def test_both_paths_are_named(self, clashing, tmp_path) -> None:
        with pytest.raises(BuildError) as excinfo:
            build_packages(clashing, tmp_path / "dist")
        assert str(clashing[0][0]) in str(excinfo.value)
        assert str(clashing[1][0]) in str(excinfo.value)

    def test_nothing_is_built_before_the_refusal(self, clashing, tmp_path) -> None:
        """The whole point of a batch is that nobody is watching each wheel."""
        with pytest.raises(BuildError):
            build_packages(clashing, tmp_path / "dist")
        assert not (tmp_path / "dist").exists()

    def test_identical_binaries_are_a_rebuild_not_a_clash(
        self, write_binary, tmp_path
    ) -> None:
        """Builds are reproducible, so a duplicate input loses nothing."""
        data = make_elf(0x3E, interp=None)
        plans = [
            self.plan(write_binary("a/tool", data)),
            self.plan(write_binary("b/tool", data)),
        ]
        results = build_packages(plans, tmp_path / "dist")
        assert len(results) == 2
        assert len(list((tmp_path / "dist").glob("*.whl"))) == 1


class TestReproducibility:
    def test_identical_inputs_produce_identical_wheels(
        self, write_binary, tmp_path
    ) -> None:
        binary = write_binary("tool", make_elf(0x3E))
        first = build(binary, tmp_path / "a").wheel.read_bytes()
        second = build(binary, tmp_path / "b").wheel.read_bytes()
        assert first == second


class TestKeepProject:
    def test_generated_project_can_be_kept(self, write_binary, tmp_path) -> None:
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
    elif (uv := shutil.which("uv")) is not None:
        base = [uv, "pip", "install", "--python", sys.executable]
    else:
        return None
    return [*base, "--no-deps", "--target", str(target), str(wheel)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions and exec")
class TestInstalledWheelRuns:
    """Install a wheel wrapping a real executable and run it.

    Uses a tiny shell script rather than a compiled binary so the test stays
    portable, which also makes this the end-to-end check that a script is
    detected, tagged and installed without any explicit platform tag.
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
            platform_tag=platform_tag(inspect_binary(binary)),
            launcher=request.param,
        )
        result = build_package(binary, spec, tmp_path / "dist")

        target = tmp_path / "site"
        command = install_command(result.wheel, target)
        if command is None:
            pytest.skip("neither pip nor uv is available to install the wheel")
        subprocess.run(command, check=True, capture_output=True)  # ruff: ignore[subprocess-without-shell-equals-true]

        # Direct-mode wheels put the binary in `.data/scripts`, which installers
        # unpack into a `bin/` beside the packages under `--target`.
        if request.param is Launcher.DIRECT:
            executable = target / "bin" / "greet"
        else:
            executable = target / "greet_bin" / "bin" / "greet"
        return target, executable

    def test_executable_bit_survives_installation(self, installed) -> None:
        _, executable = installed
        assert executable.is_file()
        assert stat.S_IMODE(executable.stat().st_mode) & 0o111, (
            "the installer did not preserve the executable bit"
        )

    def test_running_it_directly_works(self, installed) -> None:
        _, executable = installed
        completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            [str(executable), "world"], capture_output=True, text=True, check=False
        )
        assert completed.stdout.strip() == "hello world"
        assert completed.returncode == 7, "exit status must propagate"

    def test_python_dash_m_works(self, installed) -> None:
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

    def test_binary_path_resolves_to_the_installed_file(self, installed) -> None:
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
