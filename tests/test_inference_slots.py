"""Concurrent requests to one model must queue for a slot, not fail at once."""
import asyncio

import pytest
from fastapi import HTTPException

from aios.app import ACTIVE, _SLOTS, acquire_slot
from aios.runtime import RuntimeConfig


def config(parallel=1, timeout=5.0):
    # model_construct: the field validator forbids the sub-second timeouts tests need.
    return RuntimeConfig.model_construct(parallel=parallel, timeout=timeout)


@pytest.fixture(autouse=True)
def fresh_slots():
    _SLOTS.clear()
    yield
    _SLOTS.clear()


def test_second_request_waits_for_the_first_instead_of_failing():
    async def scenario():
        first = await acquire_slot('model', config())
        served = []

        async def second():
            slot = await acquire_slot('model', config())
            served.append('second')
            slot.release()

        task = asyncio.create_task(second())
        await asyncio.sleep(0.05)
        assert served == []  # queued behind the first, not rejected
        first.release()
        await asyncio.wait_for(task, 1)
        assert served == ['second']
    asyncio.run(scenario())


def test_gives_up_with_a_clear_busy_error_after_the_timeout():
    async def scenario():
        held = await acquire_slot('model', config(timeout=0.1))
        with pytest.raises(HTTPException) as error:
            await acquire_slot('model', config(timeout=0.1))
        assert error.value.status_code == 429 and 'busy' in error.value.detail.lower()
        held.release()
    asyncio.run(scenario())


def test_parallel_setting_allows_that_many_at_once():
    async def scenario():
        slots = [await acquire_slot('model', config(parallel=3)) for _ in range(3)]
        with pytest.raises(HTTPException):
            await acquire_slot('model', config(parallel=3, timeout=0.05))
        for slot in slots:
            slot.release()
    asyncio.run(scenario())


def test_release_is_idempotent_and_the_gauge_returns_to_zero():
    async def scenario():
        before = ACTIVE._value.get()
        slot = await acquire_slot('model', config())
        assert ACTIVE._value.get() == before + 1
        slot.release()
        slot.release()  # every exit path calls it; the second call must be a no-op
        assert ACTIVE._value.get() == before
        # Exactly one permit came back: one acquire succeeds, a second still queues.
        again = await acquire_slot('model', config())
        with pytest.raises(HTTPException):
            await acquire_slot('model', config(timeout=0.05))
        again.release()
    asyncio.run(scenario())


def test_a_cancelled_waiter_does_not_swallow_the_slot():
    async def scenario():
        held = await acquire_slot('model', config())
        waiter = asyncio.create_task(acquire_slot('model', config()))
        await asyncio.sleep(0.05)
        waiter.cancel()  # e.g. Open WebUI aborting a title request while it queues
        with pytest.raises(asyncio.CancelledError):
            await waiter
        held.release()
        later = await asyncio.wait_for(acquire_slot('model', config()), 1)
        later.release()
    asyncio.run(scenario())


def test_models_do_not_share_a_queue():
    async def scenario():
        busy = await acquire_slot('first', config())
        other = await asyncio.wait_for(acquire_slot('second', config()), 1)
        other.release()
        busy.release()
    asyncio.run(scenario())
