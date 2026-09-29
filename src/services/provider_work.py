"""Bound synchronous provider work without letting late workers publish.

Slots belong to the actual worker, not its awaiting coroutine. A timeout or
cancelled turn cannot release capacity while its HTTP request is still running.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from typing import Callable, TypeVar

T = TypeVar("T")


class ProviderBusyError(RuntimeError):
    """All bounded provider workers are occupied; retry on a later robot turn."""


class ProviderPool:
    def __init__(self, workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="speech-provider")
        self._slots = BoundedSemaphore(workers)

    async def run(self, function: Callable[..., T], *args, timeout: float) -> T:
        if timeout <= 0:
            raise TimeoutError("Provider deadline exceeded")
        if not self._slots.acquire(blocking=False):
            raise ProviderBusyError("Provider capacity exhausted")

        def invoke() -> T:
            try:
                return function(*args)
            finally:
                self._slots.release()

        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, invoke)
        except BaseException:
            self._slots.release()
            raise
        # Consume late exceptions even when the caller no longer awaits them.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)


provider_pool = ProviderPool()
