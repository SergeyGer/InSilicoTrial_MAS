"""Async helpers that are safe inside Spark executors and Databricks notebooks.

The specification's blueprint called ``asyncio.run(main())`` directly inside an
``applyInPandas`` function. That breaks in two common situations:

* the worker thread already has a running event loop (Databricks notebooks,
  ``nest_asyncio`` users, or a Spark Connect client), where ``asyncio.run``
  raises ``RuntimeError: asyncio.run() cannot be called from a running event loop``;
* the default executor has no reusable loop, so thousands of partitions pay the
  cost of creating and tearing one down per batch.

:func:`run_sync` detects both cases: it runs the coroutine on the current loop if
one is free, otherwise on a dedicated loop owned by the calling thread.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Iterable, Sequence
from typing import Any, TypeVar

T = TypeVar("T")

_local = threading.local()


def _thread_loop() -> asyncio.AbstractEventLoop:
    loop = getattr(_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _local.loop = loop
    return loop


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def run_sync(coro: Awaitable[T]) -> T:
    """Run ``coro`` to completion from synchronous code, in any context."""
    running = _running_loop()
    if running is not None and not running.is_closed():
        # We are inside a live loop (e.g. a Databricks notebook cell): execute the
        # coroutine on a private loop in a helper thread to avoid re-entrancy.
        result: list[T] = []
        error: list[BaseException] = []

        def _target() -> None:
            loop = asyncio.new_event_loop()
            try:
                result.append(loop.run_until_complete(coro))
            except Exception as exc:
                # Ordinary failures are re-raised in the caller's thread; control-flow
                # exceptions (KeyboardInterrupt/SystemExit) are left to propagate.
                error.append(exc)
            finally:
                loop.close()

        thread = threading.Thread(target=_target, name="insilico-async-bridge", daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0]
    return _thread_loop().run_until_complete(coro)


async def gather_bounded(
    coros: Iterable[Awaitable[T]],
    *,
    limit: int,
    return_exceptions: bool = True,
) -> list[Any]:
    """Await many coroutines with a hard concurrency ceiling.

    The specification awaited every row in a partition at once. With 10,000 rows
    that opens 10,000 sockets and trips provider rate limits; this helper keeps at
    most ``limit`` requests in flight while preserving input order.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    semaphore = asyncio.Semaphore(limit)

    async def _guarded(coro: Awaitable[T]) -> T:
        async with semaphore:
            return await coro

    tasks = [asyncio.ensure_future(_guarded(coro)) for coro in coros]
    if not tasks:
        return []
    return list(await asyncio.gather(*tasks, return_exceptions=return_exceptions))


async def map_bounded(
    func,
    items: Sequence[Any],
    *,
    limit: int,
) -> list[Any]:
    """``asyncio.gather(*(func(item) for item in items))`` with a concurrency cap."""
    return await gather_bounded((func(item) for item in items), limit=limit)
