"""Tests for metadata normalisation, templating and asset staging."""

from __future__ import annotations

import compileall
import stat
import tomllib

import pytest

from wheelbarrow.errors import MetadataError
from wheelbarrow.scaffold import (
    SCRIPTS_DIR,
    Launcher,
    archive_executables,
    make_spec,
    scaffold_project,
    stage_binary,
    staged_paths,
)


def spec(**overrides):
    kwargs = {
        "name": "ripgrep-bin",
        "version": "15.2.0",
        "binary_name": "rg",
        "platform_tag": "macosx_11_0_arm64",
    }
    kwargs.update(overrides)
    return make_spec(**kwargs)


class TestNameNormalisation:
    @pytest.mark.parametrize(
        ("given", "dist", "module"),
        [
            ("ripgrep-bin", "ripgrep-bin", "ripgrep_bin"),
            ("Ripgrep_Bin", "ripgrep-bin", "ripgrep_bin"),
            ("fd.find", "fd-find", "fd_find"),
            ("Some--Tool", "some-tool", "some_tool"),
        ],
    )
    def test_canonicalisation(self, given, dist, module):
        s = spec(name=given)
        assert s.dist_name == dist
        assert s.module == module

    def test_leading_digit_gets_a_valid_module_name(self):
        s = spec(name="7zip-bin")
        assert s.dist_name == "7zip-bin"
        assert s.module == "_7zip_bin"
        assert s.module.isidentifier()

    @pytest.mark.parametrize("bad", ["", "   ", "-leading", "trailing-"])
    def test_invalid_names_are_rejected(self, bad):
        with pytest.raises(MetadataError):
            spec(name=bad)


class TestVersionNormalisation:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [("15.2.0", "15.2.0"), ("v1.0", "1.0"), ("1.0.0-beta1", "1.0.0b1")],
    )
    def test_pep440_normalisation(self, given, expected):
        assert spec(version=given).version == expected

    def test_invalid_version_is_rejected(self):
        with pytest.raises(MetadataError, match="not a valid PEP 440 version"):
            spec(version="not-a-version")


class TestAliases:
    def test_defaults_to_the_binary_name(self):
        assert spec(binary_name="rg").aliases == ["rg"]

    def test_windows_extension_is_stripped(self):
        assert spec(binary_name="rg.exe").aliases == ["rg"]

    def test_multiple_aliases_are_kept(self):
        assert spec(aliases=["rg", "ripgrep"]).aliases == ["rg", "ripgrep"]

    def test_invalid_alias_is_rejected(self):
        with pytest.raises(MetadataError, match="invalid console script alias"):
            spec(aliases=["rg; rm -rf /"])


class TestStaging:
    def test_binary_is_made_executable(self, tmp_path):
        source = tmp_path / "rg"
        source.write_bytes(b"binary contents")
        source.chmod(0o644)  # as it would arrive from a zip archive

        destination = tmp_path / "staged"
        stage_binary(source, destination)

        mode = stat.S_IMODE(destination.stat().st_mode)
        assert mode == 0o755
        assert destination.read_bytes() == b"binary contents"

    def test_source_permissions_do_not_leak(self, tmp_path):
        source = tmp_path / "rg"
        source.write_bytes(b"x")
        source.chmod(0o600)
        destination = tmp_path / "staged"
        stage_binary(source, destination)
        assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def build_project(tmp_path, binary, **overrides):
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
    def project(self, request, tmp_path, elf_binary):
        return build_project(tmp_path, elf_binary, launcher=request.param)

    def test_core_files_exist(self, project):
        root, s = project
        assert (root / "pyproject.toml").is_file()
        assert (root / "README.md").is_file()
        assert (root / "src" / s.module / "__init__.py").is_file()
        assert (root / "src" / s.module / "__main__.py").is_file()

    def test_uses_the_uv_build_backend(self, project):
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["build-system"]["build-backend"] == "uv_build"
        assert data["build-system"]["requires"] == ["uv_build>=0.11.30,<0.12"]
        assert data["tool"]["uv"]["build-backend"]["module-name"] == "ripgrep_bin"
        assert data["tool"]["uv"]["build-backend"]["module-root"] == "src"

    def test_common_metadata(self, project):
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["project"]["name"] == "ripgrep-bin"
        assert data["project"]["version"] == "15.2.0"
        assert data["project"]["urls"]["Homepage"] == "https://example.com"
        assert data["project"]["keywords"] == ["search", "grep"]

    def test_metadata_strings_are_escaped(self, project):
        """A description containing quotes must not corrupt the TOML."""
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["project"]["description"] == (
            'A tool with "quotes" and \\ backslashes'
        )

    def test_generated_python_compiles(self, project):
        root, _ = project
        assert compileall.compile_dir(str(root / "src"), quiet=2, force=True)

    def test_staged_binaries_are_executable(self, project):
        root, s = project
        staged = staged_paths(s, root)
        assert staged
        for path in staged:
            assert path.is_file()
            assert stat.S_IMODE(path.stat().st_mode) == 0o755

    def test_readme_mentions_the_install_command(self, project):
        root, _ = project
        assert "uv tool install ripgrep-bin" in (root / "README.md").read_text()


class TestDirectLauncher:
    @pytest.fixture
    def project(self, tmp_path, elf_binary):
        return build_project(tmp_path, elf_binary, aliases=["rg", "ripgrep"])

    def test_binary_is_staged_outside_the_module(self, project):
        """Inside `src/` it would also be swept in as package data."""
        root, s = project
        assert (root / SCRIPTS_DIR / "rg").is_file()
        assert not (root / "src" / s.module / "bin").exists()

    def test_one_copy_per_alias_named_after_the_alias(self, project):
        root, s = project
        assert staged_paths(s, root) == [
            root / SCRIPTS_DIR / "rg",
            root / SCRIPTS_DIR / "ripgrep",
        ]

    def test_no_console_scripts(self, project):
        """A console script would overwrite the binary of the same name."""
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert "scripts" not in data["project"]

    def test_data_scripts_mapping(self, project):
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["tool"]["uv"]["build-backend"]["data"]["scripts"] == SCRIPTS_DIR

    def test_archive_paths_target_the_data_directory(self, project):
        _, s = project
        assert archive_executables(s) == {
            "ripgrep_bin-15.2.0.data/scripts/rg",
            "ripgrep_bin-15.2.0.data/scripts/ripgrep",
        }

    def test_installed_name_follows_the_first_alias(self):
        s = spec(binary_name="fd-v10", aliases=["fd"])
        assert s.installed_name == "fd"

    def test_locator_prefers_paths_beside_the_package(self, project):
        """`sysconfig` is wrong under `pip install --target`, so it comes last."""
        root, s = project
        source = (root / "src" / s.module / "__init__.py").read_text()
        beside = source.index('beside / "bin"')
        via_sysconfig = source.index("Path(scripts) / BINARY_NAME")
        assert beside < via_sysconfig


class TestShimLauncher:
    @pytest.fixture
    def project(self, tmp_path, elf_binary):
        return build_project(
            tmp_path, elf_binary, launcher=Launcher.SHIM, aliases=["rg", "ripgrep"]
        )

    def test_binary_is_staged_inside_the_package(self, project):
        root, s = project
        assert (root / "src" / s.module / "bin" / "rg").is_file()
        assert not (root / SCRIPTS_DIR).exists()

    def test_one_shared_copy_regardless_of_alias_count(self, project):
        root, s = project
        assert staged_paths(s, root) == [root / "src" / s.module / "bin" / "rg"]

    def test_console_scripts_cover_every_alias(self, project):
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert data["project"]["scripts"] == {
            "rg": "ripgrep_bin.__main__:main",
            "ripgrep": "ripgrep_bin.__main__:main",
        }

    def test_no_data_scripts_mapping(self, project):
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())
        assert "data" not in data["tool"]["uv"]["build-backend"]

    def test_archive_path_is_inside_the_package(self, project):
        _, s = project
        assert archive_executables(s) == {"ripgrep_bin/bin/rg"}

    def test_installed_name_keeps_the_source_file_name(self):
        s = spec(binary_name="fd-v10", aliases=["fd"], launcher=Launcher.SHIM)
        assert s.installed_name == "fd-v10"
