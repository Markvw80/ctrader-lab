"""Run an async job on the Twisted reactor (once per process)."""

from collections.abc import Awaitable, Callable
from typing import Any


def run(main: Callable[[], Awaitable[Any]]) -> None:
    """Run `main()` and exit the process; SystemExit(code) inside the job sets the exit code."""
    from twisted.internet import defer, task

    task.react(lambda _reactor: defer.ensureDeferred(main()))
