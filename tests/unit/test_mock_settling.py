"""The mock tests' own rule: a check waits until every resource of its test is registered.

A resource reaches the mocks some time after its constructor returns, when its inputs have
resolved. On 2026-11-27 a check in `test_infra_auth_api.py` ran before the role policy it
read was registered and failed on CI ("0 registered"); it had won that race until then. The
guard added that day found nine more checks that ran over an incomplete stack on the
development machine without failing. `conftest.settled` and `conftest.after_settled` make
the wait complete, and `conftest.registered` refuses to answer before it is. These tests
hold both, with a resource that is late on purpose.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pulumi
import pulumi_aws as aws
import pytest

from tests.unit import conftest
from tests.unit.conftest import after_settled, registered, require_settled, settled, unregistered

TOPIC = "aws:sns/topic:Topic"
TESTS = Path(__file__).resolve().parent


def late_input(release: asyncio.Event, value: str) -> pulumi.Output[str]:
    """An input that resolves only when `release` is set: its resource cannot be registered
    before then."""

    async def wait() -> str:
        await release.wait()
        return value

    return pulumi.Output.from_input(asyncio.ensure_future(wait()))


@pulumi.runtime.test
def test_a_check_that_does_not_wait_is_refused_by_name() -> Any:
    """The race, made certain: the check runs when the first topic is registered, and the
    second cannot be until the check lets it."""
    release = asyncio.Event()
    early = aws.sns.Topic("settle1-early")
    aws.sns.Topic("settle1-late", display_name=late_input(release, "late"))

    def check(_: str) -> None:
        try:
            assert unregistered() == [f"{TOPIC} settle1-late"]
            with pytest.raises(AssertionError, match=r"before 1 of this test's resources"):
                registered(TOPIC, "settle1-")
            with pytest.raises(AssertionError, match="settle1-late"):
                require_settled()
        finally:
            release.set()

    # On purpose: one resource's own output, which says nothing of the other resource.
    return early.id.apply(check)


@pulumi.runtime.test
def test_after_settled_waits_for_a_resource_that_is_late() -> Any:
    release = asyncio.Event()
    asyncio.get_event_loop().call_later(0.05, release.set)
    early = aws.sns.Topic("settle2-early")
    aws.sns.Topic("settle2-late", display_name=late_input(release, "late"))

    def check(args: list[Any]) -> None:
        # The values asked for, in order, and nothing added to them.
        assert args == ["settle2-early-id", "first", 2]
        assert unregistered() == []
        assert [name for name, _ in registered(TOPIC, "settle2-")] == [
            "settle2-early",
            "settle2-late",
        ]

    return after_settled(early.id, "first", 2).apply(check)


@pulumi.runtime.test
def test_settled_also_waits_for_a_resource_constructed_while_it_waits() -> Any:
    first = aws.sns.Topic("settle3-first")
    # Constructed in a callback, once the first topic is registered.
    first.arn.apply(lambda arn: aws.sns.Topic("settle3-second", display_name=arn))

    def check(count: int) -> None:
        assert count == 2
        assert {name for name, _ in registered(TOPIC, "settle3-")} == {
            "settle3-first",
            "settle3-second",
        }

    return settled().apply(check)


def test_each_test_starts_with_no_resources_of_its_own() -> None:
    assert conftest.CREATED == [] and unregistered() == []
    # With nothing on its way, a read answers (here: resources of earlier tests, by prefix).
    assert registered(TOPIC, "no-such-prefix-") == []


# The names a mock test's `return ....apply(check)` may begin with. Each is `after_settled`
# or `settled` itself, or a helper of its module that is built on `after_settled`.
WAITS = {
    "after_settled",
    "settled",
    "_after",
    "_after_everything",
    "_clients_ready",
    "_ready",
    "stack.ready",
}
HELPERS = ("_after", "_after_everything", "_clients_ready", "_ready", "ready")


def mock_test_files() -> list[Path]:
    return [
        path
        for path in sorted(TESTS.glob("test_*.py"))
        if "@pulumi.runtime.test" in path.read_text(encoding="utf-8")
        and path.name != Path(__file__).name
    ]


def statement_heads(source: str) -> list[str]:
    """For each `.apply(check` in a module: what its `return` statement begins with."""
    heads = []
    for found in re.finditer(r"\.apply\(\s*check", source):
        before = source[: found.start()]
        statement = before[before.rindex("\n    return ") + len("\n    return ") :]
        heads.append(statement.split("(", 1)[0].strip())
    return heads


def helper_bodies(source: str) -> list[tuple[str, str]]:
    """(name, body) of each wait helper a module defines."""
    lines = source.splitlines()
    bodies = []
    for at, line in enumerate(lines):
        defined = re.match(r"( *)def (\w+)\(", line)
        if defined is None or defined.group(2) not in HELPERS:
            continue
        indent = len(defined.group(1))
        body = []
        for following in lines[at + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        bodies.append((defined.group(2), "\n".join(body)))
    return bodies


def test_every_mock_test_waits_through_the_settled_helpers() -> None:
    """No test combines outputs itself to decide when its check runs: each check is applied
    to `after_settled(...)`, `settled()`, or a helper of its module built on `after_settled`."""
    files = mock_test_files()
    assert len(files) >= 9
    checks = helpers = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert "pulumi.Output.all(" not in source, path.name
        for head in statement_heads(source):
            assert head in WAITS, (path.name, head)
            checks += 1
        for name, body in helper_bodies(source):
            assert "after_settled(" in body, (path.name, name)
            helpers += 1
    # The scan is not vacuous: it found the checks and the helpers.
    assert checks >= 60 and helpers >= 6


def test_only_two_modules_read_the_list_of_registrations_directly() -> None:
    """`registered()` refuses to answer too early; the raw list does not. One module reads it
    after its program has run to its end, the other behind `require_settled`."""
    readers = {
        path.name
        for path in TESTS.glob("test_*.py")
        if re.search(
            r"^from tests\.unit\.conftest import .*\bREGISTERED\b",
            path.read_text(encoding="utf-8"),
            re.M,
        )
    }
    assert readers == {"test_infra_program.py", "test_tenant_program.py"}
    assert "require_settled()" in (TESTS / "test_infra_program.py").read_text(encoding="utf-8")
