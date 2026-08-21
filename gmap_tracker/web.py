from __future__ import annotations

import hmac
import os
import secrets
import socket
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable

from adafruit_ssd1305 import SSD1305
from aiohttp import web
from PIL import ImageFont

from . import config
from .config import Config, ValidationError
from .tracking import Tracker

TOKEN_PATH = Path(os.environ.get("TM_TOKEN_FILE", "/etc/traffic-monitor/ui-token"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

TOKEN_KEY: web.AppKey[str] = web.AppKey("token")
TRACKER_KEY: web.AppKey["Tracker"] = web.AppKey("tracker")
DEGRADED_KEY: web.AppKey[str | None] = web.AppKey("degraded")
DEFAULTS_KEY: web.AppKey[Path] = web.AppKey("defaults_path")
OVERRIDE_KEY: web.AppKey[Path] = web.AppKey("override_path")

SAMPLE_LINE = "Travel time: 999 min"
REQUIRED_LINES = 3

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def load_token(path: Path = TOKEN_PATH) -> str:
    try:
        token = path.read_text().strip()
        if token:
            return token
    except OSError:
        pass

    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n")
        path.chmod(0o600)
    except OSError as exc:
        print(f"Could not persist UI token to {path}: {exc}")
    print(f"Generated UI token: {token}")
    return token


@lru_cache(maxsize=1)
def font_catalogue(width: int, height: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name, path in sorted(SSD1305.available_fonts().items()):
        missing = ""
        try:
            if path.suffix.lower() in (".ttf", ".otf"):
                loaded = ImageFont.truetype(path, config.FALLBACK.display.font_size)
                box = loaded.getbbox(SAMPLE_LINE)
                sample_width, cell_height = box[2] - box[0], box[3] - box[1]
                cell_width = sample_width / len(SAMPLE_LINE)
                kind, uppercase_only = "ttf", False
            else:
                from adafruit_ssd1305.bitmap_font import BitmapFont

                loaded = BitmapFont.load(str(path))
                # render_text upper-cases, so measure and check coverage against that.
                rendered = SAMPLE_LINE.upper()
                sample_width, cell_height = loaded.text_size(rendered)
                cell_width = loaded.width
                kind, uppercase_only = "bitmap", True
                absent = sorted(set(rendered) - set(loaded.chars))
                missing = "".join(absent)
        except Exception as exc:
            entries.append({"name": name, "kind": "unknown", "fits": False, "reason": str(exc)})
            continue

        lines = height // (cell_height + 2) if cell_height else 0
        fits = sample_width <= width and lines >= REQUIRED_LINES and not missing
        reason = ""
        if missing:
            reason = f"missing glyphs: {missing!r}"
        elif sample_width > width:
            reason = f"too wide - sample needs {int(sample_width)}px, panel is {width}px"
        elif lines < REQUIRED_LINES:
            reason = f"too tall - fits {lines} lines, needs {REQUIRED_LINES}"

        entries.append(
            {
                "name": name,
                "kind": kind,
                "width": round(cell_width, 1),
                "height": cell_height,
                "uppercase_only": uppercase_only,
                "max_chars": int(width // cell_width) if cell_width else 0,
                "fits": fits,
                "reason": reason,
            }
        )
    entries.sort(key=lambda e: (not e["fits"], e["name"]))
    return entries


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    if not request.path.startswith("/api/"):
        return await handler(request)
    supplied = request.headers.get("X-Auth-Token", "")
    if not hmac.compare_digest(supplied, request.app[TOKEN_KEY]):
        raise web.HTTPUnauthorized(text="bad or missing X-Auth-Token")
    if TRACKER_KEY not in request.app:
        raise web.HTTPServiceUnavailable(text="tracker still starting")
    return await handler(request)


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def get_config(request: web.Request) -> web.Response:
    tracker: Tracker = request.app[TRACKER_KEY]
    override, _ = config._read(request.app[OVERRIDE_KEY])
    return web.json_response({"config": config.to_dict(tracker.config), "overridden": sorted(override)})


async def put_config(request: web.Request) -> web.Response:
    tracker: Tracker = request.app[TRACKER_KEY]
    try:
        patch = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"invalid JSON: {exc}") from exc
    if not isinstance(patch, dict):
        raise web.HTTPBadRequest(text="body must be a JSON object")

    defaults, _ = config._read(request.app[DEFAULTS_KEY])
    try:
        merged = config.parse(config.deep_merge(defaults, patch))
    except ValidationError as exc:
        return web.json_response({"field": exc.field, "error": exc.message}, status=400)

    config.save_override(patch, request.app[OVERRIDE_KEY])
    await tracker.apply_config(merged)
    return web.json_response({"config": config.to_dict(merged)})


async def reset_config(request: web.Request) -> web.Response:
    tracker: Tracker = request.app[TRACKER_KEY]
    config.reset_override(request.app[OVERRIDE_KEY])
    restored, degraded = config.load(request.app[DEFAULTS_KEY], request.app[OVERRIDE_KEY])
    await tracker.apply_config(restored)
    return web.json_response({"config": config.to_dict(restored), "degraded": degraded})


async def status(request: web.Request) -> web.Response:
    tracker: Tracker = request.app[TRACKER_KEY]
    task = tracker.task
    next_iteration = task.next_iteration
    return web.json_response(
        {
            "hostname": socket.gethostname(),
            "configured": tracker.config.is_configured,
            "degraded": request.app.get(DEGRADED_KEY),
            "is_running": task.is_running(),
            "failed": task.failed(),
            "current_loop": task.current_loop,
            "next_iteration": next_iteration.isoformat() if next_iteration else None,
            "last_error": tracker.last_error,
        }
    )


async def fonts(request: web.Request) -> web.Response:
    display = request.app[TRACKER_KEY].display
    return web.json_response({"fonts": font_catalogue(display.width, display.height)})


async def test_route(request: web.Request) -> web.Response:
    tracker: Tracker = request.app[TRACKER_KEY]
    if not tracker.config.is_configured:
        raise web.HTTPBadRequest(text="no route configured")
    try:
        data = await tracker.fetch()
    except Exception as exc:
        return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=502)
    routes = data.get("routes") or [{}]
    return web.json_response(
        {"duration": routes[0].get("duration"), "distanceMeters": routes[0].get("distanceMeters"), "raw": data}
    )


def build_app(
    token: str,
    defaults_path: Path = config.DEFAULTS_PATH,
    override_path: Path = config.OVERRIDE_PATH,
) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app[TOKEN_KEY] = token
    app[DEFAULTS_KEY] = defaults_path
    app[OVERRIDE_KEY] = override_path
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/config", get_config),
            web.put("/api/config", put_config),
            web.post("/api/config/reset", reset_config),
            web.get("/api/status", status),
            web.get("/api/fonts", fonts),
            web.post("/api/route/test", test_route),
        ]
    )
    return app


async def start(app: web.Application, host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner


def attach(app: web.Application, tracker: Tracker, degraded: str | None) -> None:
    app[TRACKER_KEY] = tracker
    app[DEGRADED_KEY] = degraded


__all__ = ("attach", "build_app", "font_catalogue", "load_token", "start")
