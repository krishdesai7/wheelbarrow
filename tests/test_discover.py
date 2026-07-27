"""Finding executables in a path, and what a failure to read one means."""

import pytest

from wheelbarrow import discover
from wheelbarrow.errors import InspectionError

from .conftest import make_elf, make_macho


class TestASingleFile:
    """A named file must be readable: it is the answer to a direct question."""

    def test_it_yields_one_candidate(self, elf_binary) -> None:
        found = discover.collect(elf_binary)
        assert [c.path for c in found.candidates] == [elf_binary]
        assert found.skipped == ()
        assert found.is_single

    def test_an_unreadable_format_is_an_error_not_a_skip(self, write_binary) -> None:
        path = write_binary("notes.txt", b"just some text, not a binary")
        with pytest.raises(InspectionError, match="unrecognised executable format"):
            discover.collect(path)

    def test_a_missing_path_says_so(self, tmp_path) -> None:
        with pytest.raises(InspectionError, match="no such file or directory"):
            discover.collect(tmp_path / "nowhere")


class TestADirectory:
    """Inside a directory the same failure just means "not a binary"."""

    @pytest.fixture
    def tree(self, tmp_path):
        """A directory shaped like what `fetch` leaves behind."""
        (tmp_path / "tool-linux").mkdir()
        (tmp_path / "tool-macos").mkdir()
        (tmp_path / "tool-linux" / "tool").write_bytes(make_elf(0x3E))
        (tmp_path / "tool-macos" / "tool").write_bytes(make_macho(0x0100000C))
        # The archives the binaries came out of, still sitting beside them.
        (tmp_path / "tool-linux.tar.gz").write_bytes(b"\x1f\x8b\x08not really")
        (tmp_path / "tool-macos.tar.gz").write_bytes(b"\x1f\x8b\x08not really")
        return tmp_path

    def test_the_executables_are_found_and_the_archives_are_not(self, tree) -> None:
        found = discover.collect(tree)
        assert [c.path.parent.name for c in found.candidates] == [
            "tool-linux",
            "tool-macos",
        ]
        assert {p.name for p in found.skipped} == {
            "tool-linux.tar.gz",
            "tool-macos.tar.gz",
        }

    def test_a_directory_is_never_a_single(self, tree) -> None:
        """Even one binary plus one archive is a directory listing, not a file."""
        assert not discover.collect(tree).is_single

    def test_results_come_back_in_a_stable_order(self, tree) -> None:
        """Wheels are built in this order, so it must not depend on the file system."""
        first = [c.path for c in discover.collect(tree).candidates]
        assert first == sorted(first)

    def test_nested_directories_are_searched(self, tmp_path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "tool").write_bytes(make_elf(0x3E))
        assert len(discover.collect(tmp_path).candidates) == 1

    def test_dot_directories_are_left_alone(self, tmp_path) -> None:
        (tmp_path / "tool").write_bytes(make_elf(0x3E))
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "hook").write_bytes(make_elf(0x3E))
        found = discover.collect(tmp_path)
        assert [c.path.name for c in found.candidates] == ["tool"]

    def test_symlinks_are_skipped(self, tmp_path) -> None:
        """A link beside its target would be packaged twice under two names."""
        (tmp_path / "tool").write_bytes(make_elf(0x3E))
        (tmp_path / "tool-latest").symlink_to(tmp_path / "tool")
        found = discover.collect(tmp_path)
        assert [c.path.name for c in found.candidates] == ["tool"]

    def test_a_directory_with_no_binaries_is_an_error(self, tmp_path) -> None:
        (tmp_path / "tool.tar.gz").write_bytes(b"\x1f\x8b\x08not really")
        with pytest.raises(InspectionError, match="no executable"):
            discover.collect(tmp_path)

    def test_that_error_suggests_unpacking(self, tmp_path) -> None:
        """The overwhelmingly common cause is archives that were never unpacked."""
        (tmp_path / "tool.tar.gz").write_bytes(b"\x1f\x8b\x08not really")
        with pytest.raises(InspectionError, match="unpack"):
            discover.collect(tmp_path)

    def test_an_empty_directory_says_it_is_empty(self, tmp_path) -> None:
        with pytest.raises(InspectionError, match="empty"):
            discover.collect(tmp_path)

    def test_the_inspection_result_travels_with_the_path(self, tree) -> None:
        by_name = {
            c.path.parent.name: c.info for c in discover.collect(tree).candidates
        }
        assert by_name["tool-linux"].format == "elf"
        assert by_name["tool-macos"].format == "macho"
