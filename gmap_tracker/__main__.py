import asyncio
import signal
import socket

from . import config, web
from .tracking import Tracker


async def main() -> None:
    cfg, degraded = config.load()
    app = web.build_app(web.load_token())
    runner = await web.start(app)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        async with Tracker(cfg, config.load_headers()) as tracker:
            web.attach(app, tracker, degraded)
            if degraded:
                print(f"Config degraded: {degraded}")
            if not cfg.is_configured:
                tracker.show_lines("unconfigured", f"{socket.gethostname()}.local:8080")
            await stop.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
