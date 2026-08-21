from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import time as dtime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adafruit_ssd1305 import SSD1305

from ._types.enums import Units

DEFAULTS_PATH = Path(os.environ.get("TM_DEFAULTS", "/opt/traffic-monitor/defaults.json"))
OVERRIDE_PATH = Path(os.environ.get("TM_OVERRIDE", "/var/lib/traffic-monitor/config.json"))
HEADERS_PATH = Path(os.environ.get("TM_HEADERS", "/etc/traffic-monitor/headers.json"))

MAX_INTERMEDIATES = 25


class ValidationError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


@dataclass(frozen=True)
class Point:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Intermediate:
    location: Point
    via: bool = True


@dataclass(frozen=True)
class Route:
    origin: Point
    destination: Point
    intermediates: list[Intermediate] = field(default_factory=list)


@dataclass(frozen=True)
class Schedule:
    start: dtime
    end: dtime
    weekdays: list[int]


@dataclass(frozen=True)
class Display:
    font: str = "DejaVuSansMono.ttf"
    font_size: int = 8


@dataclass(frozen=True)
class Config:
    timezone: str
    schedule: Schedule
    poll_interval_minutes: float
    eta_offset_minutes: float
    route: Route | None
    avoid_tolls: bool
    language_code: str
    units: str
    display: Display

    @property
    def is_configured(self) -> bool:
        return self.route is not None

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


FALLBACK = Config(
    timezone="Europe/London",
    schedule=Schedule(start=dtime(7, 0), end=dtime(16, 0), weekdays=[0, 1, 2, 3, 4]),
    poll_interval_minutes=3,
    eta_offset_minutes=10,
    route=None,
    avoid_tolls=True,
    language_code="en-GB",
    units="IMPERIAL",
    display=Display(),
)


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _num(raw: Any, path: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValidationError(path, "must be a number")
    if not math.isfinite(raw):
        raise ValidationError(path, "must be finite")
    return float(raw)


def _point(raw: Any, path: str) -> Point:
    if not isinstance(raw, dict):
        raise ValidationError(path, "must be an object")
    lat = _num(raw.get("latitude"), f"{path}.latitude")
    lng = _num(raw.get("longitude"), f"{path}.longitude")
    if not -90 <= lat <= 90:
        raise ValidationError(f"{path}.latitude", "must be between -90 and 90")
    if not -180 <= lng <= 180:
        raise ValidationError(f"{path}.longitude", "must be between -180 and 180")
    return Point(lat, lng)


def _time(raw: Any, path: str) -> dtime:
    if not isinstance(raw, str):
        raise ValidationError(path, "must be a HH:MM string")
    try:
        hour, _, minute = raw.partition(":")
        return dtime(int(hour), int(minute))
    except ValueError as exc:
        raise ValidationError(path, f"not a valid HH:MM time ({exc})") from exc


def _route(raw: Any) -> Route | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationError("route", "must be an object")
    if raw.get("origin") is None or raw.get("destination") is None:
        return None

    origin = _point(raw["origin"], "route.origin")
    destination = _point(raw["destination"], "route.destination")
    if origin == destination:
        raise ValidationError("route.destination", "must differ from the origin")

    raw_intermediates = raw.get("intermediates") or []
    if not isinstance(raw_intermediates, list):
        raise ValidationError("route.intermediates", "must be a list")
    if len(raw_intermediates) > MAX_INTERMEDIATES:
        raise ValidationError("route.intermediates", f"at most {MAX_INTERMEDIATES} allowed")

    intermediates = []
    for index, item in enumerate(raw_intermediates):
        path = f"route.intermediates[{index}]"
        if not isinstance(item, dict):
            raise ValidationError(path, "must be an object")
        intermediates.append(
            Intermediate(location=_point(item.get("location"), f"{path}.location"), via=bool(item.get("via", True)))
        )
    return Route(origin=origin, destination=destination, intermediates=intermediates)


def _display(raw: Any, allowed: Iterable[str]) -> Display:
    if raw is None:
        return Display()
    if not isinstance(raw, dict):
        raise ValidationError("display", "must be an object")

    font = raw.get("font", Display.font)
    # Allowlist lookup, never a path join: a crafted name would read any file.
    if font not in set(allowed):
        raise ValidationError("display.font", f"unknown font {font!r}")

    size = int(_num(raw.get("font_size", Display.font_size), "display.font_size"))
    if not 6 <= size <= 24:
        raise ValidationError("display.font_size", "must be between 6 and 24")
    return Display(font=font, font_size=size)


def parse(raw: dict[str, Any], allowed_fonts: Iterable[str] | None = None) -> Config:
    allowed = allowed_fonts if allowed_fonts is not None else SSD1305.available_fonts().keys()

    timezone = raw.get("timezone", FALLBACK.timezone)
    if not isinstance(timezone, str):
        raise ValidationError("timezone", "must be a string")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("timezone", f"unknown timezone ({exc})") from exc

    raw_schedule = raw.get("schedule") or {}
    if not isinstance(raw_schedule, dict):
        raise ValidationError("schedule", "must be an object")
    start = _time(raw_schedule.get("start", "07:00"), "schedule.start")
    end = _time(raw_schedule.get("end", "16:00"), "schedule.end")
    weekdays = raw_schedule.get("weekdays", list(FALLBACK.schedule.weekdays))
    if not isinstance(weekdays, list) or not weekdays:
        raise ValidationError("schedule.weekdays", "must be a non-empty list")
    if any(not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in weekdays):
        raise ValidationError("schedule.weekdays", "entries must be integers 0-6")

    poll = _num(raw.get("poll_interval_minutes", FALLBACK.poll_interval_minutes), "poll_interval_minutes")
    if not 1 <= poll <= 60:
        raise ValidationError("poll_interval_minutes", "must be between 1 and 60")

    offset = _num(raw.get("eta_offset_minutes", FALLBACK.eta_offset_minutes), "eta_offset_minutes")
    if not 0 <= offset <= 120:
        raise ValidationError("eta_offset_minutes", "must be between 0 and 120")

    units = raw.get("units", FALLBACK.units)
    if units not in (Units.METRIC.value, Units.IMPERIAL.value):
        raise ValidationError("units", "must be METRIC or IMPERIAL")

    language = raw.get("language_code", FALLBACK.language_code)
    if not isinstance(language, str) or not 2 <= len(language) <= 16:
        raise ValidationError("language_code", "must be a BCP-47 language tag")

    modifiers = raw.get("route_modifiers") or {}
    if not isinstance(modifiers, dict):
        raise ValidationError("route_modifiers", "must be an object")

    return Config(
        timezone=timezone,
        schedule=Schedule(start=start, end=end, weekdays=sorted(set(weekdays))),
        poll_interval_minutes=poll,
        eta_offset_minutes=offset,
        route=_route(raw.get("route")),
        avoid_tolls=bool(modifiers.get("avoid_tolls", FALLBACK.avoid_tolls)),
        language_code=language,
        units=units,
        display=_display(raw.get("display"), allowed),
    )


def to_dict(cfg: Config) -> dict[str, Any]:
    data = asdict(cfg)
    data["schedule"]["start"] = cfg.schedule.start.strftime("%H:%M")
    data["schedule"]["end"] = cfg.schedule.end.strftime("%H:%M")
    data["route_modifiers"] = {"avoid_tolls": data.pop("avoid_tolls")}
    return data


def _read(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return {}, None
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"{path.name}: {exc}"


def load(
    defaults_path: Path = DEFAULTS_PATH,
    override_path: Path = OVERRIDE_PATH,
    allowed_fonts: Iterable[str] | None = None,
) -> tuple[Config, str | None]:
    """Never raises; degrades to FALLBACK with a reason."""
    defaults, defaults_err = _read(defaults_path)
    override, override_err = _read(override_path)
    degraded = defaults_err or override_err

    try:
        return parse(deep_merge(defaults, override), allowed_fonts), degraded
    except ValidationError as exc:
        if not override_err and override:
            try:
                return parse(defaults, allowed_fonts), f"override rejected ({exc})"
            except ValidationError:
                pass
        return FALLBACK, f"{degraded + '; ' if degraded else ''}{exc}"


def save_override(patch: dict[str, Any], override_path: Path = OVERRIDE_PATH) -> None:
    """Atomic replace. Caller validates first."""
    override_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = override_path.with_suffix(".json.tmp")
    with open(tmp, "w") as handle:
        json.dump(patch, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, override_path)
    # Without fsyncing the directory the rename itself can be lost on an SD card.
    fd = os.open(override_path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def reset_override(override_path: Path = OVERRIDE_PATH) -> None:
    override_path.unlink(missing_ok=True)


def load_headers(path: Path = HEADERS_PATH) -> dict[str, str]:
    return json.loads(path.read_text())


__all__ = (
    "Config",
    "Display",
    "Intermediate",
    "Point",
    "Route",
    "Schedule",
    "ValidationError",
    "load",
    "load_headers",
    "parse",
    "replace",
    "reset_override",
    "save_override",
    "to_dict",
)
