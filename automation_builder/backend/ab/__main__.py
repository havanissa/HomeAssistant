"""Entry point: ``python3 -m ab``."""

from __future__ import annotations

import logging

from aiohttp import web

from .core import AppCore
from .logbuffer import setup_logging
from .settings import load_settings
from .web import build_app


def main() -> None:
    settings = load_settings()
    buffer = setup_logging(settings.log_level, [settings.token or ""])
    logger = logging.getLogger("ab")
    if settings.dev_mode:
        logger.warning("Development mode: %s", settings.public())
    core = AppCore(settings, buffer)
    app = build_app(settings, core)

    async def start_core(_app: web.Application) -> None:
        await core.start()

    async def stop_core(_app: web.Application) -> None:
        await core.stop()

    app.on_startup.append(start_core)
    app.on_cleanup.append(stop_core)
    web.run_app(app, host=settings.host, port=settings.port, access_log=None, shutdown_timeout=5,
                print=lambda *_: logger.info("Web interface listening on %s:%s", settings.host, settings.port))


if __name__ == "__main__":
    main()
