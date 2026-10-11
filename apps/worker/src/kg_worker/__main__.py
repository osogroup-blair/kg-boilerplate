from __future__ import annotations

import asyncio
import sys

from kg_common.settings import get_settings

from kg_worker.worker import Worker, install_signal_handlers


async def _main() -> int:
    stop = asyncio.Event()
    install_signal_handlers(asyncio.get_running_loop(), stop)
    return await Worker(get_settings().model_copy(update={"service_name": "kg-worker"}), serve_metrics=True).run(stop)


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
