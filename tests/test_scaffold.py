"""Tests for metadata normalisation, templating and asset staging."""

from __future__ import annotations

import compileall
import stat
import tomllib

import pytest

from wheelbarrow.errors import MetadataError
from wheelbarrow.scaffold import make_spec, scaffold_project, stage_binary


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


class TestProjectRendering:
    @pytest.fixture
    def project(self, tmp_path, elf_binary):
        root = tmp_path / "project"
        root.mkdir()
        s = spec(
            description='A tool with "quotes" and \\ backslashes',
            license="MIT",
            author="Krish Desai",
            author_email="krish@example.com",
            homepage="https://example.com",
            keywords=["search", "grep"],
            aliases=["rg", "ripgrep"],
        )
        scaffold_project(s, elf_binary, root)
        return root, s

    def test_layout(self, project):
        root, s = project
        assert (root / "pyproject.toml").is_file()
        assert (root / "README.md").is_file()
        assert (root / "src" / s.module / "__init__.py").is_file()
        assert (root / "src" / s.module / "__main__.py").is_file()
        assert (root / "src" / s.module / "bin" / "rg").is_file()

    def test_pyproject_is_valid_toml(self, project):
        root, _ = project
        data = tomllib.loads((root / "pyproject.toml").read_text())

        assert data["project"]["name"] == "ripgrep-bin"
        assert data["project"]["version"] == "15.2.0"
        assert data["build-system"]["build-backend"] == "hatchling.build"
        assert data["project"]["scripts"] == {
            "rg": "ripgrep_bin.__main__:main",
            "ripgrep": "ripgrep_bin.__main__:main",
        }
        assert data["project"]["urls"]["Homepage"] == "https://example.com"
        assert data["project"]["keywords"] == ["search", "grep"]
        assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
            "src/ripgrep_bin"
        ]

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

    def test_staged_binary_is_executable(self, project):
        root, s = project
        staged = root / "src" / s.module / "bin" / "rg"
        assert stat.S_IMODE(staged.stat().st_mode) == 0o755

    def test_readme_mentions_the_install_command(self, project):
        root, _ = project
        assert "uv tool install ripgrep-bin" in (root / "README.md").read_text()
