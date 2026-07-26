"""Ask PyPI whether a project name is already registered.

The simple index answers this cheaply and without authentication: a `HEAD` of
`/simple/<name>/` is 200 for a name that exists and 404 for one that does not.
A registered name stays 200 even when every release has been deleted or
yanked, which is exactly the case worth warning about -- the name cannot be
claimed again.

Two things this deliberately does *not* do. It never decides whether a build
should proceed: 200 only means the name exists, not that it belongs to someone
else, and rebuilding a package you already own is the common case. And it never
turns a network problem into a failure -- wheelbarrow builds offline, so an
unreachable index yields `UNKNOWN` and the build carries on.
"""

import urllib.error
import urllib.parse
import urllib.request
from enum import StrEnum, auto
from typing import Final

from . import __version__

#: The simple index. `{name}` must already be PEP 503 normalised.
SIMPLE_INDEX: Final[str] = "https://pypi.org/simple/{name}/"

#: Kept short: this sits in front of every build, and the answer is advisory.
DEFAULT_TIMEOUT: Final[float] = 3.0


class NameStatus(StrEnum):
    """What the index said about a project name."""

    #: The name is registered. It may or may not belong to you.
    TAKEN = auto()
    #: No such project; the name is free to claim.
    AVAILABLE = auto()
    #: The index could not be reached, or answered something unexpected.
    UNKNOWN = auto()


def check_name(
    name: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    index: str = SIMPLE_INDEX,
) -> NameStatus:
    """Report whether `name` is registered on PyPI.

    `name` is expected to be PEP 503 normalised, as `scaffold.make_spec`
    returns it. Any failure to reach the index is reported as `UNKNOWN` rather
    than raised: this is advice, not a gate.
    """
    url: str = index.format(name=urllib.parse.quote(name, safe=""))
    request = urllib.request.Request(  # ruff: ignore[suspicious-url-open-usage]
        url,
        method="HEAD",
        headers={"User-Agent": f"wheelbarrow/{__version__}"},
    )

    try:
        with urllib.request.urlopen(  # ruff: ignore[suspicious-url-open-usage]
            request, timeout=timeout
        ) as response:
            return NameStatus.TAKEN if response.status == 200 else NameStatus.UNKNOWN
    except urllib.error.HTTPError as exc:
        # HTTPError is an OSError subclass, so it has to be caught first.
        return NameStatus.AVAILABLE if exc.code == 404 else NameStatus.UNKNOWN
    except OSError, ValueError:
        # Offline, DNS failure, proxy refusal, TLS problem, timeout: all of
        # these mean "no answer", never "the name is free".
        return NameStatus.UNKNOWN
