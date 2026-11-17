"""Entry point: ``python3 -m smart_lighting``."""

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
    logger = logging.getLogger("smart_lighting")
    if settings.dev_mode:
        logger.warning("Development mode: %s", settings.public())
    core = AppCore(settings, buffer)

    async def start_core(app: web.Application) -> None:
        await core.start()

    async def stop_core(app: web.Application) -> None:
        await core.stop()

    app = build_app(settings, core)
    # build_app only starts a core it created itself; this one is ours.
    app.on_startup.insert(0, start_core)
    app.on_cleanup.append(stop_core)
    web.run_app(
        app,
        host=settings.host,
        port=settings.port,
        access_log=logging.getLogger("aiohttp.access"),
        print=lambda *_: logger.info("Web interface listening on %s:%s", settings.host, settings.port),
        shutdown_timeout=5,
    )


if __name__ == "__main__":
    main()
