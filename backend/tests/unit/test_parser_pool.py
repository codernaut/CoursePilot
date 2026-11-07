"""The parser runs in a child process that is killed on timeout and recreated afterwards."""

import os
import time
from collections.abc import AsyncIterator, Callable

import pytest

from app.ingestion.parser_pool import ParseFn, ParserPool
from app.ingestion.parsers import ParseError, ParseLimitError, ParseLimits
from tests import parser_support

LIMITS = ParseLimits(timeout_s=1.0)


PoolFactory = Callable[[ParseFn], ParserPool]


@pytest.fixture
async def pool() -> AsyncIterator[PoolFactory]:
    created: list[ParserPool] = []

    def make(fn: ParseFn) -> ParserPool:
        p = ParserPool(fn, grace_s=0.0)
        created.append(p)
        return p

    yield make
    for p in created:
        await p.aclose()


async def test_a_parse_runs_in_another_process_and_returns_the_sections(pool: PoolFactory) -> None:
    parsed = await pool(parser_support.pid_parse).parse("txt", b"x", LIMITS)
    assert int(parsed.sections[0].text) != os.getpid()
    assert parsed.page_count == 1


async def test_the_child_is_reused_between_documents(pool: PoolFactory) -> None:
    p = pool(parser_support.pid_parse)
    first = await p.parse("txt", b"x", LIMITS)
    second = await p.parse("txt", b"x", LIMITS)
    assert first.sections[0].text == second.sections[0].text and p.restarts == 0


async def test_a_parse_error_travels_back_with_its_message(pool: PoolFactory) -> None:
    p = pool(parser_support.failing_parse)
    with pytest.raises(ParseError, match="broken in a readable way"):
        await p.parse("txt", b"x", LIMITS)
    assert p.restarts == 0  # an ordinary failure does not cost a restart


async def test_a_hung_parser_is_killed_at_the_timeout_and_the_pool_recovers(
    pool: PoolFactory,
) -> None:
    p = pool(parser_support.slow_parse)
    started = time.monotonic()
    with pytest.raises(ParseLimitError, match="took longer than 1 seconds"):
        await p.parse("txt", b"x", LIMITS)
    assert time.monotonic() - started < 20
    assert p.restarts == 1

    # The same pool object is usable again: a fresh child is created for the next document.
    p._parse_fn = parser_support.ok_parse
    parsed = await p.parse("txt", b"hello", LIMITS)
    assert parsed.sections[0].text == "hello"


async def test_a_child_that_dies_gives_a_readable_error_and_the_pool_recovers(
    pool: PoolFactory,
) -> None:
    p = pool(parser_support.crash_parse)
    with pytest.raises(ParseError, match="parser stop unexpectedly"):
        await p.parse("txt", b"x", LIMITS)
    p._parse_fn = parser_support.ok_parse
    assert (await p.parse("txt", b"again", LIMITS)).sections[0].text == "again"
    assert p.restarts == 1
