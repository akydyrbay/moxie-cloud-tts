"""App-local dependencies for the migrated function-based protocol engine.

HTTP requests, MQTT callbacks and provider threads explicitly enter the same
scope. A standalone scope preserves direct use by protocol regression tests.
No module reloads or process-global rebinding are needed for isolated apps.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock


class Scope:
    def __init__(self, **components):
        self.components = components
        self.lock = RLock()
        self.tasks = set()
        self.closing = False

    def get(self, name, factory):
        with self.lock:
            if name not in self.components:
                self.components[name] = factory()
            return self.components[name]

    @contextmanager
    def activate(self):
        token = _scope.set(self)
        try:
            yield self
        finally:
            _scope.reset(token)

    def spawn(self, coroutine):
        if self.closing:
            coroutine.close()
            raise RuntimeError("Agent is shutting down")
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


_standalone = Scope()
_scope = ContextVar("moxie_scope", default=_standalone)


def current_scope():
    return _scope.get()


class Local:
    """Compatibility handle resolving a dependency in the current app scope."""

    def __init__(self, name, factory):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_factory", factory)

    def resolve(self):
        return current_scope().get(self._name, self._factory)

    def __getattr__(self, name):
        return getattr(self.resolve(), name)

    def __setattr__(self, name, value):
        setattr(self.resolve(), name, value)

    def __delattr__(self, name):
        delattr(self.resolve(), name)

    def __getitem__(self, key):
        return self.resolve()[key]

    def __setitem__(self, key, value):
        self.resolve()[key] = value

    def __delitem__(self, key):
        del self.resolve()[key]

    def __iter__(self):
        return iter(self.resolve())

    def __len__(self):
        return len(self.resolve())

    def __contains__(self, key):
        return key in self.resolve()

    def __call__(self, *args, **kwargs):
        return self.resolve()(*args, **kwargs)
