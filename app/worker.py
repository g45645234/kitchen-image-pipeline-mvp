from __future__ import annotations

import asyncio
import logging
import signal

from app.config import settings
from app.services.job_runner import fetch_and_run_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    stop_event = stop_event or asyncio.Event()
    logger.info("Job worker started")
    while not stop_event.is_set():
        try:
            await fetch_and_run_jobs()
        except Exception:
            logger.exception("Error in job worker loop")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_poll_interval_seconds)
        except TimeoutError:
            pass
    logger.info("Job worker stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)


async def main() -> None:
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    await run_worker(stop_event)


if __name__ == "__main__":
    asyncio.run(main())
