import pytest

from app.core.errors import AppError
from app.services.rate_limit import SlidingWindowLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_reports_the_wait() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(3, clock=clock)
    for _ in range(3):
        assert limiter.hit("u") is None
        clock.now += 1
    wait = limiter.hit("u")
    assert wait is not None and 56.9 < wait <= 58.0  # the oldest hit leaves after 60 s


def test_the_window_slides_and_a_limited_hit_is_not_recorded() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(2, clock=clock)
    assert limiter.hit("u") is None and limiter.hit("u") is None
    assert limiter.hit("u") is not None
    assert limiter.hit("u") is not None  # still limited: refusals do not extend the block
    clock.now += 60
    assert limiter.hit("u") is None


def test_keys_are_independent() -> None:
    limiter = SlidingWindowLimiter(1, clock=Clock())
    assert limiter.hit("alice") is None
    assert limiter.hit("alice") is not None
    assert limiter.hit("bob") is None


def test_enforce_raises_429_with_retry_after() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(1, clock=clock)
    limiter.enforce("u", "chat")
    clock.now += 10
    with pytest.raises(AppError) as info:
        limiter.enforce("u", "chat")
    error = info.value
    assert error.status_code == 429 and error.code == "rate_limited"
    assert error.headers["Retry-After"] == "50"
    assert error.details["retry_after_s"] == 50


def test_stale_keys_are_swept() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(1, clock=clock)
    for index in range(5200):
        limiter.hit(f"user-{index}")
    clock.now += 61
    limiter.hit("fresh")
    assert len(limiter._hits) < 10
