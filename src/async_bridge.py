"""Run async coroutines from synchronous Flask views.

Flask views in Lyrica are synchronous, while fetchers and metadata extractors
are async (httpx). ``run_async`` bridges the two using one reusable event loop
per worker thread.
"""

import asyncio
import atexit
import threading
import weakref

from src.logger import get_logger

logger = get_logger("async_bridge")

_thread_state = threading.local()
_LOOPS: "weakref.WeakSet[asyncio.AbstractEventLoop]" = weakref.WeakSet()


@atexit.register
def _close_thread_loops() -> None:
    """Close per-thread loops before interpreter teardown.

    Python otherwise destroys the selector's file descriptors first and then
    garbage-collects the loop, which emits a noisy "Invalid file descriptor: -1"
    warning on shutdown.
    """
    for loop in list(_LOOPS):
        if loop.is_closed() or loop.is_running():
            continue
        try:
            loop.close()
        except Exception:  # pragma: no cover - best effort during shutdown
            pass


def _get_thread_loop() -> asyncio.AbstractEventLoop:
    """Return a reusable event loop for the current worker thread.

    ``asyncio.get_event_loop()`` raises in worker threads and the previous
    fallback created (and leaked) a brand-new loop on every request. Reusing a
    loop per thread keeps the shared httpx connection pool usable and avoids
    "Event loop is closed" errors under load.
    """
    loop = getattr(_thread_state, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_state.loop = loop
        _LOOPS.add(loop)
    return loop


def _run_coro_in_new_thread(coro, timeout: int):
    """Run a coroutine in a short-lived thread when this thread already has a
    running loop (e.g. an async view or a framework that manages its own)."""
    outcome: dict = {}

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            outcome["value"] = loop.run_until_complete(
                asyncio.wait_for(coro, timeout=timeout)
            )
        except BaseException as exc:  # propagated to the calling thread
            outcome["error"] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    thread = threading.Thread(target=_runner, name="lyrica-async", daemon=True)
    thread.start()
    thread.join(timeout + 5)
    if thread.is_alive():
        raise TimeoutError("Request timed out - operation took too long")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def run_async(coro, timeout=30):
    """Run an async coroutine safely from a synchronous Flask view.

    Raises :class:`TimeoutError` (HTTP 504 at the route layer) rather than a
    generic exception, so clients receive the correct status code.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is not None:
        return _run_coro_in_new_thread(coro, timeout)

    loop = _get_thread_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
    except (asyncio.TimeoutError, TimeoutError) as exc:
        logger.error("Async operation timed out after %ss", timeout)
        raise TimeoutError("Request timed out - operation took too long") from exc
