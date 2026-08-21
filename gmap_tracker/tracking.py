import asyncio
from datetime import datetime, timedelta
from enum import Enum
from types import TracebackType
from typing import Any, Mapping, Self, Sequence
from zoneinfo import ZoneInfo

import aiohttp
from adafruit_ssd1305 import SSD1305, SSD1305_128x32, constants

from ._types.enums import RouteTravelMode, RoutingPreference
from ._types.models import LatLng, Location, Request, RouteModifiers, Waypoint
from .config import Config, Display
from .tasks import loop

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


class Tracker:
    _session: aiohttp.ClientSession
    _request_body: Request | None
    _display: SSD1305
    headers: dict[str, str]
    task_running: asyncio.Event

    def __init__(self, config: Config, headers: dict[str, str]) -> None:
        self.config = config
        self.headers = headers
        self.lock = asyncio.Lock()
        self.last_error: str | None = None
        self._tz = ZoneInfo(config.timezone)

    async def __aenter__(self) -> Self:
        self._session = aiohttp.ClientSession()
        self.task_running = asyncio.Event()
        self._display = SSD1305_128x32().__enter__()
        self._apply_font(self.config.display)

        self._build_request_body()
        self.task.change_interval(minutes=self.config.poll_interval_minutes)
        self.task.start()
        self.task_running.set()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        self.task.cancel()
        await self._session.close()
        self._display.__exit__(None, None, None)
        if exc_type in (KeyboardInterrupt, asyncio.CancelledError):
            return True
        return False

    def _apply_font(self, display: Display) -> None:
        path = SSD1305.available_fonts()[display.font]
        if path.suffix.lower() in (".ttf", ".otf"):
            # The font setter reads _font_size, so size has to land first.
            self._display._font_size = display.font_size
        self._display.font = path

    async def apply_config(self, new: Config) -> None:
        async with self.lock:
            if new.display != self.config.display:
                self._apply_font(new.display)
            self.config = new
            self._tz = ZoneInfo(new.timezone)
            self._build_request_body()
            self.task.change_interval(minutes=new.poll_interval_minutes)
            self.task.restart()

    def _build_request_body(self) -> Request | None:
        route = self.config.route
        if route is None:
            self._request_body = None
            return None

        def location(point: Any) -> Location:
            latlng: LatLng = {"latitude": point.latitude, "longitude": point.longitude}
            return {"latLng": latlng}

        origin: Waypoint = {"location": location(route.origin)}
        destination: Waypoint = {"location": location(route.destination)}
        intermediates: list[Waypoint] = [
            {"via": wp.via, "location": location(wp.location)} for wp in route.intermediates
        ]
        route_modifiers: RouteModifiers = {"avoidTolls": self.config.avoid_tolls}

        self._request_body = {
            "origin": origin,
            "intermediates": intermediates,
            "destination": destination,
            "travelMode": RouteTravelMode.DRIVE,
            "routingPreference": RoutingPreference.TRAFFIC_AWARE,
            "computeAlternativeRoutes": False,
            "routeModifiers": route_modifiers,
            "languageCode": self.config.language_code,
            "units": self.config.units,
            "extraComputations": [],
        }
        return self._request_body

    @property
    def stringified_request_body(self) -> dict[str, Any]:
        def stringify(body: Any) -> Any:
            if isinstance(body, Enum):
                return body.value
            elif isinstance(body, Mapping):
                return {str(key): stringify(value) for key, value in body.items()}
            elif isinstance(body, Sequence) and not isinstance(body, (str, bytes, bytearray)):
                return [stringify(item) for item in body]
            else:
                return body

        result = stringify(self._request_body)
        assert isinstance(result, dict)
        return result

    @property
    def route(self) -> str:
        return ROUTES_URL

    @property
    def is_active(self) -> bool:
        now = datetime.now(self._tz)
        schedule = self.config.schedule
        return now.weekday() in schedule.weekdays and schedule.start <= now.time() < schedule.end

    @property
    def display(self) -> SSD1305:
        return self._display

    @property
    def offset(self) -> timedelta:
        return timedelta(minutes=self.config.eta_offset_minutes)

    @property
    def line_height(self) -> int:
        height = getattr(self._display.font, "height", self._display.font_size)
        return min(height + 2, self._display.height // 3)

    async def fetch(self) -> dict[str, Any]:
        result = await self._session.post(self.route, json=self.stringified_request_body, headers=self.headers)
        return await result.json()

    def show_lines(self, *lines: str) -> None:
        self.display.fill(constants.Colour.BLACK)
        for index, line in enumerate(lines):
            self.display.text(line, 0, index * self.line_height)

    def update_display(self, data: dict[str, Any]) -> None:
        routes = data.get("routes") or [{}]
        travel_seconds = int(routes[0].get("duration", "0").removesuffix("s")) + int(self.offset.total_seconds())
        eta = datetime.now(tz=self._tz) + timedelta(seconds=travel_seconds)

        speeds: dict[str, int] = {}
        for advisory in routes[0].get("travelAdvisory", {}).get("speedReadingIntervals", []):
            speed = advisory.get("speed", "SPEED_UNSPECIFIED")
            speeds[speed] = speeds.get(speed, 0) + 1

        if speeds.get("TRAFFIC_JAM", 0) > 0:
            traffic_condition = f"{speeds['TRAFFIC_JAM']}x Jam"
        elif speeds.get("SLOW", 0) > 0:
            traffic_condition = f"{speeds['SLOW']}x Slow"
        else:
            traffic_condition = "Normal"

        self.show_lines(
            f"ETA:         {eta.strftime('%H:%M')}",
            f"Travel time: {travel_seconds // 60} min",
            f"Traffic:     {traffic_condition}",
        )

    @loop(minutes=3)
    async def task(self) -> None:
        async with self.lock:
            if self._request_body is None:
                return
            if not self.is_active:
                self.display.fill(constants.Colour.BLACK)
                return

            try:
                data = await self.fetch()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                print(f"Error during tracking task: {exc}")
            else:
                self.last_error = None
                self.update_display(data)

    @task.before_loop
    async def before_task(self) -> None:
        print("Tracking task running...")

    @task.after_loop
    async def after_task(self) -> None:
        self.task_running.clear()

    @task.error
    async def on_task_error(self, exc: BaseException) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"
        print(f"Tracking loop failed: {exc}")
