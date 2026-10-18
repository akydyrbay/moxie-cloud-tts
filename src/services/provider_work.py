"""Bound synchronous provider work without letting late workers publish.

Slots belong to the actual worker, not its awaiting coroutine. A timeout or
cancelled turn cannot release capacity while its HTTP request is still running.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Callable, TypeVar

T = TypeVar("T")


class ProviderBusyError(RuntimeError):
    """All bounded provider workers are occupied; retry on a later robot turn."""


class ProviderPool:
    def __init__(self, workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="speech-provider")
        self._lock = Lock()
        self._free = workers

    def _acquire(self, spare: int) -> bool:
        with self._lock:
            if self._free <= spare:
                return False
            self._free -= 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._free += 1

    async def run(self, function: Callable[..., T], *args, timeout: float, spare: int = 0) -> T:
        """Run `function(*args)` on a worker, failing fast when capacity is exhausted.

        `spare` keeps that many slots free for other work: optional extra work
        (a hedged TTS attempt) must not take the last slot needed for recognition.
        """
        if timeout <= 0:
            raise TimeoutError("Provider deadline exceeded")
        if not self._acquire(spare):
            raise ProviderBusyError("Provider capacity exhausted")

        def invoke() -> T:
            try:
                return function(*args)
            finally:
                self._release()

        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, invoke)
        except BaseException:
            self._release()
            raise
        # Consume late exceptions even when the caller no longer awaits them.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)


provider_pool = ProviderPool()
