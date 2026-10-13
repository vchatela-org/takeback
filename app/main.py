import logging

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app import i18n, scheduler
from app.__version__ import __version__
from app.config import settings
from app.database import init_db, imports_dir
from app.routers import web, platform, imports_api, items_api, sync_api

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

init_db()
# Sinks (InfluxDB) run in a background thread, only when configured.
scheduler.start()

app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Import, parse and browse your personal data exports (Facebook, Instagram, Google Takeout, Amazon, LinkedIn, Seazon...).",
)


@app.middleware("http")
async def _resolve_locale(request: Request, call_next):
    """Pick the language for this request once, before any handler renders.

    `?lang=` wins for a single request, then the `tb-lang` cookie a language
    switch would set, then the browser's `Accept-Language`, then
    `TAKEBACK_LANG`. See app/i18n.py."""
    i18n.set_locale(i18n.negotiate(
        accept_language=request.headers.get("accept-language"),
        explicit=(request.query_params.get(i18n.LANG_PARAM)
                  or request.cookies.get(i18n.LANG_COOKIE)),
        default=settings.lang,
    ))
    return await call_next(request)


app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/media", StaticFiles(directory=str(imports_dir())), name="media")

app.include_router(web.router)
app.include_router(platform.router)
app.include_router(imports_api.router)
app.include_router(items_api.router)
app.include_router(sync_api.router)
