"""Tests for metadata normalisation, templating and asset staging."""

import compileall
import hashlib
import stat
import tomllib
from typing import TYPE_CHECKING

import pytest

from wheelbarrow.errors import MetadataError
from wheelbarrow.probe import inspect_binary
from wheelbarrow.scaffold import (
    SCRIPTS_DIR,
    Launcher,
    PackageSpec,
    archive_executables,
    describe_input,
    make_spec,
    render_readme,
    scaffold_project,
    stage_binary,
    staged_paths,
)

from .conftest import MUSL_INTERP, make_elf, make_fat_macho, make_macho, make_pe

if TYPE_CHECKING:
    from pathlib import Path


def spec(**overrides) -> PackageSpec:
    kwargs: dict[str, str] = {
        "name": "wheelbarrow-bin",
        "version": "15.2.0",
        "binary_name": "wb",
        "platform_tag": "macosx_11_0_arm64",
    }
    kwargs.update(overrides)
    return make_spec(**kwargs)  # type: ignore[bad-arg-type]


class TestNameNormalisation:
    @pytest.mark.parametrize(
        ("given", "dist", "module"),
        [
            ("wheelbarrow-bin", "wheelbarrow-bin", "wheelbarrow_bin"),
            ("Wheelbarrow_Bin", "wheelbarrow-bin", "wheelbarrow_bin"),
            ("bw.barrowwheel", "bw-barrowwheel", "bw_barrowwheel"),
            ("Some--Tool", "some-tool", "some_tool"),
        ],
    )
    def test_canonicalisation(self, given, dist, module) -> None:
        s: PackageSpec = spec(name=given)
        assert s.dist_name == dist
        assert s.module == module

    def test_leading_digit_gets_a_valid_module_name(self) -> None:
        s: PackageSpec = spec(name="7zip-bin")
        assert s.dist_name == "7zip-bin"
        assert s.module == "_7zip_bin"
        assert s.module.isidentifier()

    @pytest.mark.parametrize("bad", ["", "   ", "-leading", "trailing-"])
    def test_invalid_names_are_rejected(self, bad) -> None:
        with pytest.raises(MetadataError):
            spec(name=bad)


class TestVersionNormalisation:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [("15.2.0", "15.2.0"), ("v1.0", "1.0"), ("1.0.0-beta1", "1.0.0b1")],
    )
    def test_pep440_normalisation(self, given, expected) -> None:
        assert spec(version=given).version == expected

    def test_invalid_version_is_rejected(self) -> None:
        with pytest.raises(MetadataError, match="not a valid PEP 440 version"):
            spec(version="not-a-version")


class TestAliases:
    def test_defaults_to_the_binary_name(self) -> None:
        assert spec(binary_name="rg").aliases == ["rg"]

    def test_windows_extension_is_stripped(self) -> None:
        assert spec(binary_name="rg.exe").aliases == ["rg"]

    def test_multiple_aliases_are_kept(self) -> None:
        assert spec(aliases=["wb", "wheelbarrow"]).aliases == ["wb", "wheelbarrow"]

    def test_invalid_alias_is_rejected(self) -> None:
        with pytest.raises(MetadataError, match="invalid console script alias"):
            spec(aliases=["wb; rm -rf /"])


class TestStaging:
    def test_binary_is_made_executable(self, tmp_path) -> None:
        source = tmp_path / "wb"
        source.write_bytes(b"binary contents")
        source.chmod(0o644)  # as it would arrive from a zip archive

        destination = tmp_path / "staged"
        stage_binary(source, destination)

        mode = stat.S_IMODE(destination.stat().st_mode)
        assert mode == 0o755
        assert destination.read_bytes() == b"binary contents"

    def test_source_permissions_do_not_leak(self, tmp_path) -> None:
        source = tmp_path / "wb"
        source.write_bytes(b"x")
        source.chmod(0o600)
        destination = tmp_path / "staged"
        stage_binary(source, destination)
        assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def build_project(
    tmp_path: Path, binary: Path, **overrides
) -> tuple[Path, PackageSpec]:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    s = spec(
        description='A tool with "quotes" and \\ backslashes',
        licence="MIT",
        author="Krish Desai",
        author_email="krish@example.com",
        homepage="https://example.com",
        keywords=["search", "grep"],
        **overrides,
    )
    scaffold_project(s, binary, root)
    return root, s


class TestProjectRenderingCommon:
    """Behaviour that must hold whichever launcher is selected."""

    @pytest.fixture(params=[Launcher.DIRECT, Launcher.SHIM])
    def project(self, request, tmp_path, elf_binary) -> tuple[Path, PackageSpec]:
        return build_project(tmp_path, elf_binary, launcher=request.param)

    def test_core_files_exist(self, project) -> None:
        root, s = project
        assert (root / "pyproject.toml").is_file()
        assert (root / "README.md").is_file()
        assert (root / "src" / s.module / "__init__.py").is_file()
        assert (root / "src" / s.module / "__main__.py").is_file()

    def test_uses_the_uv_build_backend(self, project) -> None:
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["build-system"]["build-backend"] == "uv_build"
        assert data["build-system"]["requires"] == ["uv_build>=0.11.30,<0.12"]
        assert data["tool"]["uv"]["build-backend"]["module-name"] == "wheelbarrow_bin"
        assert data["tool"]["uv"]["build-backend"]["module-root"] == "src"

    def test_common_metadata(self, project) -> None:
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["project"]["name"] == "wheelbarrow-bin"
        assert data["project"]["version"] == "15.2.0"
        assert data["project"]["urls"]["Homepage"] == "https://example.com"
        assert data["project"]["keywords"] == ["search", "grep"]

    def test_licence_is_written_under_the_key_pep_621_defines(self, project) -> None:
        """Our identifiers say `licence`; the emitted key must say `license`.

        Backends ignore an unrecognised key silently, so getting this wrong
        drops the field from the wheel's METADATA without any error.
        """
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["project"]["license"] == "MIT"
        assert "licence" not in data["project"]

    def test_metadata_strings_are_escaped(self, project) -> None:
        """A description containing quotes must not corrupt the TOML."""
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["project"]["description"] == (
            'A tool with "quotes" and \\ backslashes'
        )

    def test_generated_python_compiles(self, project) -> None:
        root, _ = project
        assert compileall.compile_dir(str(root / "src"), quiet=2, force=True)

    def test_staged_binaries_are_executable(self, project) -> None:
        root, s = project
        staged = staged_paths(s, root)
        assert staged
        for path in staged:
            assert path.is_file()
            assert stat.S_IMODE(path.stat().st_mode) == 0o755

    def test_readme_mentions_the_install_command(self, project) -> None:
        root, _ = project
        assert "uv tool install wheelbarrow-bin" in (root / "README.md").read_text()


class TestDirectLauncher:
    @pytest.fixture
    def project(self, tmp_path, elf_binary) -> tuple[Path, PackageSpec]:
        return build_project(tmp_path, elf_binary, aliases=["wb", "wheelbarrow"])

    def test_binary_is_staged_outside_the_module(self, project) -> None:
        """Inside `src/` it would also be swept in as package data."""
        root, s = project
        assert (root / SCRIPTS_DIR / "wb").is_file()
        assert not (root / "src" / s.module / "bin").exists()

    def test_one_copy_per_alias_named_after_the_alias(self, project) -> None:
        root, s = project
        assert staged_paths(s, root) == [
            root / SCRIPTS_DIR / "wb",
            root / SCRIPTS_DIR / "wheelbarrow",
        ]

    def test_no_console_scripts(self, project) -> None:
        """A console script would overwrite the binary of the same name."""
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert "scripts" not in data["project"]

    def test_data_scripts_mapping(self, project) -> None:
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["tool"]["uv"]["build-backend"]["data"]["scripts"] == SCRIPTS_DIR

    def test_archive_paths_target_the_data_directory(self, project) -> None:
        _, s = project
        assert archive_executables(s) == {
            "wheelbarrow_bin-15.2.0.data/scripts/wb",
            "wheelbarrow_bin-15.2.0.data/scripts/wheelbarrow",
        }

    def test_installed_name_follows_the_first_alias(self) -> None:
        s = spec(binary_name="bw-v10", aliases=["bw"])
        assert s.installed_name == "bw"

    def test_locator_prefers_paths_beside_the_package(self, project) -> None:
        """`sysconfig` is wrong under `pip install --target`, so it comes last."""
        root, s = project
        source = (root / "src" / s.module / "__init__.py").read_text()
        beside = source.index('beside / "bin"')
        via_sysconfig = source.index("Path(scripts) / BINARY_NAME")
        assert beside < via_sysconfig


class TestWindowsSuffix:
    """Direct mode renames the staged file, and Windows reads the suffix.

    A `starship.exe` installed as `Scripts\\starship` is a file Windows will
    not execute, so `.exe` has to survive being renamed after the alias.
    """

    def test_exe_survives_the_rename(self) -> None:
        s = spec(binary_name="starship.exe", platform_tag="win_amd64")
        assert s.installed_name == "starship.exe"

    def test_the_alias_itself_is_unchanged(self) -> None:
        """The suffix is on the file; the command is still `starship`."""
        s = spec(binary_name="starship.exe", platform_tag="win_amd64")
        assert s.aliases == ["starship"]

    @pytest.mark.parametrize("suffix", [".exe", ".EXE", ".com", ".bat", ".cmd"])
    def test_every_pathext_suffix_is_kept(self, suffix) -> None:
        s = spec(binary_name=f"tool{suffix}", platform_tag="win_amd64")
        assert s.installed_name == f"tool{suffix}"

    @pytest.mark.parametrize("suffix", [".sh", ".py", ".bin", ".v10"])
    def test_other_suffixes_are_still_dropped(self, suffix) -> None:
        """On POSIX an extension on a command name is noise, not meaning."""
        s = spec(binary_name=f"tool{suffix}", platform_tag="any")
        assert s.installed_name == "tool"

    def test_an_explicit_alias_does_not_gain_a_second_suffix(self) -> None:
        s = spec(binary_name="starship.exe", aliases=["starship.exe"])
        assert s.installed_name == "starship.exe"

    def test_every_alias_gets_the_suffix(self) -> None:
        s = spec(binary_name="tool.exe", aliases=["tool", "othertool"])
        assert s.installed_names == ["tool.exe", "othertool.exe"]
        assert archive_executables(s) == {
            "wheelbarrow_bin-15.2.0.data/scripts/tool.exe",
            "wheelbarrow_bin-15.2.0.data/scripts/othertool.exe",
        }

    def test_shim_mode_is_untouched(self) -> None:
        """There the file keeps its own name inside the package regardless."""
        s = spec(binary_name="tool.exe", launcher=Launcher.SHIM)
        assert s.installed_names == ["tool.exe"]

    def test_the_locator_looks_for_the_installed_name(
        self, tmp_path, elf_binary
    ) -> None:
        """`binary_path()` must agree with what was actually installed."""
        root, s = build_project(tmp_path, elf_binary, binary_name="tool.exe")
        source = (root / "src" / s.module / "__init__.py").read_text()
        assert 'BINARY_NAME = "tool.exe"' in source
        assert (root / SCRIPTS_DIR / "tool.exe").is_file()


class TestShimLauncher:
    @pytest.fixture
    def project(self, tmp_path, elf_binary) -> tuple[Path, PackageSpec]:
        return build_project(
            tmp_path, elf_binary, launcher=Launcher.SHIM, aliases=["wb", "wheelbarrow"]
        )

    def test_binary_is_staged_inside_the_package(self, project) -> None:
        root, s = project
        assert (root / "src" / s.module / "bin" / "wb").is_file()
        assert not (root / SCRIPTS_DIR).exists()

    def test_one_shared_copy_regardless_of_alias_count(self, project) -> None:
        root, s = project
        assert staged_paths(s, root) == [root / "src" / s.module / "bin" / "wb"]

    def test_console_scripts_cover_every_alias(self, project) -> None:
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["project"]["scripts"] == {
            "wb": "wheelbarrow_bin.__main__:main",
            "wheelbarrow": "wheelbarrow_bin.__main__:main",
        }

    def test_no_data_scripts_mapping(self, project) -> None:
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert "data" not in data["tool"]["uv"]["build-backend"]

    def test_archive_path_is_inside_the_package(self, project) -> None:
        _, s = project
        assert archive_executables(s) == {"wheelbarrow_bin/bin/wb"}

    def test_installed_name_keeps_the_source_file_name(self) -> None:
        s = spec(binary_name="bw-v10", aliases=["bw"], launcher=Launcher.SHIM)
        assert s.installed_name == "bw-v10"


class TestProvenanceDescription:
    """What the generated README says about the file it wraps."""

    def describe(self, path, tag):
        return describe_input(inspect_binary(path), path, tag)

    def test_the_digest_is_of_the_file_as_packaged(self, write_binary) -> None:
        """It exists to be checked against what the tool's publisher lists."""
        data = make_elf(0x3E)
        binary = write_binary("tool", data)
        found = self.describe(binary, "manylinux_2_17_x86_64")
        assert found.sha256 == hashlib.sha256(data).hexdigest()

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (make_elf(0x3E), "ELF executable, linux/x86_64"),
            (make_pe(0x8664), "PE executable, windows/x86_64"),
            (make_macho(0x0100000C), "Mach-O executable, macos/arm64"),
        ],
    )
    def test_the_format_is_named_in_prose(self, write_binary, data, expected) -> None:
        """`BinaryInfo.describe` is for a terminal; this ends up on PyPI."""
        found = self.describe(write_binary("tool", data), "any")
        assert found.kind.startswith(expected)

    def test_linkage_is_spelled_out(self, write_binary) -> None:
        binary = write_binary("tool", make_elf(0x3E, interp=None))
        assert "statically linked" in self.describe(binary, "any").kind

    def test_a_script_names_its_interpreter(self, shell_script) -> None:
        found = self.describe(shell_script, "any")
        assert found.kind == "script run by `/bin/sh`"

    def test_the_macos_floor_is_carried_over(self, write_binary) -> None:
        binary = write_binary("tool", make_macho(0x0100000C, minos=(12, 3)))
        assert "macOS 12.3+" in self.describe(binary, "macosx_12_3_arm64").kind


class TestProvenanceTagNotes:
    """The tag is glossed, but only where it and the binary agree."""

    def note(self, path, tag) -> str:
        return describe_input(inspect_binary(path), path, tag).note

    def test_a_script_is_told_its_tag_promises_too_much(self, shell_script) -> None:
        """The one thing an installer needs to know, and the tag cannot say it."""
        note = self.note(shell_script, "any")
        assert "/bin/sh" in note
        assert "install anywhere" in note

    def test_a_static_binary_explains_the_compressed_set(self, write_binary) -> None:
        binary = write_binary("tool", make_elf(0x3E, interp=None))
        note = self.note(binary, "manylinux_2_17_x86_64.musllinux_1_2_x86_64")
        assert "no C library" in note
        assert "Alpine" in note

    def test_a_glibc_binary_quotes_the_floor_from_the_tag(self, write_binary) -> None:
        binary = write_binary("tool", make_elf(0x3E))
        assert "2.28 or newer" in self.note(binary, "manylinux_2_28_x86_64")

    def test_a_musl_binary_says_so(self, write_binary) -> None:
        binary = write_binary("tool", make_elf(0x3E, interp=MUSL_INTERP))
        assert "musl" in self.note(binary, "musllinux_1_2_x86_64")

    def test_a_universal_binary_names_both_slices(self, write_binary) -> None:
        binary = write_binary("tool", make_fat_macho([0x01000007, 0x0100000C]))
        note = self.note(binary, "macosx_11_0_universal2")
        assert "x86_64" in note
        assert "arm64" in note

    def test_a_windows_binary_needs_no_gloss(self, write_binary) -> None:
        """`win_amd64` says everything there is to say."""
        assert self.note(write_binary("tool.exe", make_pe(0x8664)), "win_amd64") == ""

    def test_an_overridden_tag_gets_no_gloss_from_the_binary(
        self, write_binary
    ) -> None:
        """--platform-tag can contradict the file; then say nothing about it."""
        binary = write_binary("tool", make_elf(0x3E, interp=None))
        # Static, but forced onto a plain manylinux tag: the both-families
        # explanation would describe a tag this wheel does not carry.
        assert self.note(binary, "manylinux_2_17_x86_64") == ""

    def test_a_script_forced_onto_a_platform_tag_is_left_alone(
        self, shell_script
    ) -> None:
        assert self.note(shell_script, "manylinux_2_17_x86_64") == ""


class TestReadmeProvenance:
    """The rendered README carries the facts, and survives without them."""

    def readme(self, path, tag, **overrides) -> str:
        return render_readme(
            spec(
                platform_tag=tag,
                binary_name=path.name,
                provenance=describe_input(inspect_binary(path), path, tag),
                **overrides,
            )
        )

    def test_the_digest_and_tag_are_both_present(self, write_binary) -> None:
        data = make_elf(0x3E, interp=None)
        binary = write_binary("tool", data)
        tag = "manylinux_2_17_x86_64.musllinux_1_2_x86_64"
        text = self.readme(binary, tag)
        assert hashlib.sha256(data).hexdigest() in text
        assert tag in text

    def test_the_launcher_is_explained_not_just_named(self, write_binary) -> None:
        """`direct` and `shim` are wheelbarrow's jargon, not a user's."""
        binary = write_binary("tool", make_elf(0x3E))
        direct = self.readme(binary, "any", launcher=Launcher.DIRECT)
        shim = self.readme(binary, "any", launcher=Launcher.SHIM)
        assert "starts no interpreter" in direct
        assert "execv" in shim
        assert direct != shim

    def test_it_renders_without_any_provenance(self) -> None:
        """A library caller building a spec by hand still gets a valid README."""
        text = render_readme(spec())
        assert "sha256" not in text
        assert "macosx_11_0_arm64" in text

    def test_generated_markdown_is_wrapped(self, shell_script) -> None:
        """A long interpreter path must not leave a 200-column line behind."""
        text = self.readme(shell_script, "any")
        assert max(len(line) for line in text.splitlines()) <= 88
