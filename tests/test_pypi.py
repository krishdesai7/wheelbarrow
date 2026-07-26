"""Tests for the PyPI name check.

Every test stubs `urlopen`: the suite must stay hermetic, and a real request
would make results depend on what happens to be registered today.
"""

from __future__ import annotations

import urllib.error

import pytest

from wheelbarrow.pypi import DEFAULT_TIMEOUT, NameStatus, check_name


class FakeResponse:
    """The sliver of `HTTPResponse` that `check_name` touches."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc) -> None:
        return None


@pytest.fixture
def calls(monkeypatch):
    """Capture urlopen calls and let each test say what the index replies."""
    seen: list[dict[str, object]] = []
    reply: dict[str, object] = {"response": FakeResponse(200)}

    def fake_urlopen(request, timeout=None):
        seen.append({"request": request, "timeout": timeout})
        outcome = reply["response"]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("wheelbarrow.pypi.urllib.request.urlopen", fake_urlopen)
    return seen, reply


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://pypi.org/simple/demo/", code=code, msg="", hdrs=None, fp=None
    )


class TestStatus:
    def test_200_means_the_name_is_taken(self, calls) -> None:
        _, reply = calls
        reply["response"] = FakeResponse(200)
        assert check_name("demo-bin") is NameStatus.TAKEN

    def test_404_means_the_name_is_available(self, calls) -> None:
        _, reply = calls
        reply["response"] = http_error(404)
        assert check_name("demo-bin") is NameStatus.AVAILABLE

    @pytest.mark.parametrize("code", [403, 429, 500, 503])
    def test_other_http_codes_are_unknown(self, calls, code) -> None:
        """A rate limit or outage must never read as `available`."""
        _, reply = calls
        reply["response"] = http_error(code)
        assert check_name("demo-bin") is NameStatus.UNKNOWN

    @pytest.mark.parametrize(
        "failure",
        [
            urllib.error.URLError("no route to host"),
            TimeoutError("timed out"),
            OSError("connection reset"),
            ValueError("unknown url type"),
        ],
    )
    def test_network_failures_are_unknown(self, calls, failure) -> None:
        """Building offline is supported, so this can never raise."""
        _, reply = calls
        reply["response"] = failure
        assert check_name("demo-bin") is NameStatus.UNKNOWN

    def test_an_unexpected_success_code_is_unknown(self, calls) -> None:
        _, reply = calls
        reply["response"] = FakeResponse(204)
        assert check_name("demo-bin") is NameStatus.UNKNOWN


class TestRequest:
    def test_asks_the_simple_index_for_the_name(self, calls) -> None:
        seen, _ = calls
        check_name("demo-bin")
        assert seen[0]["request"].full_url == "https://pypi.org/simple/demo-bin/"

    def test_uses_head_so_no_body_is_fetched(self, calls) -> None:
        seen, _ = calls
        check_name("demo-bin")
        assert seen[0]["request"].get_method() == "HEAD"

    def test_identifies_itself(self, calls) -> None:
        seen, _ = calls
        check_name("demo-bin")
        assert "wheelbarrow" in seen[0]["request"].get_header("User-agent")

    def test_a_timeout_is_always_set(self, calls) -> None:
        """Without one, a hung index would hang the build."""
        seen, _ = calls
        check_name("demo-bin")
        assert seen[0]["timeout"] == DEFAULT_TIMEOUT

    def test_the_timeout_is_overridable(self, calls) -> None:
        seen, _ = calls
        check_name("demo-bin", timeout=0.5)
        assert seen[0]["timeout"] == 0.5

    def test_names_are_url_quoted(self, calls) -> None:
        """Normalisation should prevent this, but the URL is built defensively."""
        seen, _ = calls
        check_name("../etc/passwd")
        assert "https://pypi.org/simple/..%2Fetc%2Fpasswd/" == (
            seen[0]["request"].full_url
        )
