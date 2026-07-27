"""Finding the executables in a path.

`fetch` leaves a directory holding one unpacked archive per platform, and the
archives themselves sitting beside them. Pointing `inspect` or `build` at that
directory should just work, which means telling the executables apart from
everything else without consulting the file system's opinion of what is
runnable -- an extracted `.zip` may have lost its mode bits, and a `.tar.gz` is
mode 0644 either way. The headers decide, exactly as they do everywhere else.

The one asymmetry worth knowing about is what a failure to inspect means. A
file named explicitly must be readable: refusing to parse it is the answer to a
direct question. The same failure inside a directory just means "not a binary",
which is the common case rather than an error.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import InspectionError
from .probe import BinaryInfo, inspect_binary

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass(frozen=True)
class Candidate:
    """A file that parsed as an executable, and what it turned out to be."""

    path: Path
    info: BinaryInfo


@dataclass(frozen=True)
class Discovery:
    """What a path yielded: the executables, and the files passed over."""

    candidates: tuple[Candidate, ...]
    skipped: tuple[Path, ...] = ()

    @property
    def is_single(self) -> bool:
        """Whether this came from one named file rather than a directory."""
        return len(self.candidates) == 1 and not self.skipped


def collect(source: Path) -> Discovery:
    """Inspect `source`, or every executable beneath it if it is a directory."""
    source = Path(source)
    if not source.exists():
        raise InspectionError(f"no such file or directory: {source}")
    if not source.is_dir():
        return Discovery((Candidate(source, inspect_binary(source)),))
    return _walk(source)


def _walk(root: Path) -> Discovery:
    """Inspect everything under `root`, keeping what parses."""
    candidates: list[Candidate] = []
    skipped: list[Path] = []
    for path in _files(root):
        try:
            candidates.append(Candidate(path, inspect_binary(path)))
        except InspectionError:
            skipped.append(path)

    if not candidates:
        examined: str = f"{len(skipped)} file(s) examined" if skipped else "it is empty"
        raise InspectionError(
            f"{root} holds no executable wheelbarrow can recognise ({examined}). "
            f"If the binaries are still inside downloaded archives, unpack them "
            f"first -- `wheelbarrow fetch` does that as it downloads."
        )

    return Discovery(tuple(candidates), tuple(skipped))


def _files(root: Path) -> Iterator[Path]:
    """Every regular file under `root`, in a stable order.

    Dot-directories are skipped, and so are symlinks: one may point outside the
    tree or close a cycle, and a link beside its target would otherwise be
    packaged twice under two names.
    """
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".") or entry.is_symlink():
            continue
        if entry.is_dir():
            yield from _files(entry)
        elif entry.is_file():
            yield entry
