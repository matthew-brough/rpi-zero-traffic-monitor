# Based off implementation in Rapptz/discord.py
from __future__ import annotations

import asyncio
import datetime
import inspect
import random
import time
from collections.abc import Sequence
from typing import Any, Callable, Coroutine, Literal, Optional, overload

from ._types import MISSING


class ExponentialBackoff[T: bool | Literal[True] | Literal[False]]:
    """An implementation of the exponential backoff algorithm

    Provides a convenient interface to implement an exponential backoff
    for reconnecting or retrying transmissions in a distributed network.

    Once instantiated, the delay method will return the next interval to
    wait for when retrying a connection or transmission.  The maximum
    delay increases exponentially with each retry up to a maximum of
    2^10 * base, and is reset if no more attempts are needed in a period
    of 2^11 * base seconds.

    Parameters
    ----------
    base: :class:`int`
        The base delay in seconds. The first retry-delay will be up to
        this many seconds.
    integral: :class:`bool`
        Set to ``True`` if whole periods of base is desirable, otherwise any
        number in between may be returned.
    """

    def __init__(self, base: int = 1, integral: T = False) -> None:
        self._base = base

        self._exp: int = 0
        self._max: int = 10
        self._reset_time: int = base * 2**11
        self._last_invocation: float = time.monotonic()

        rand = random.Random()
        rand.seed()

        self._randfunc: Callable[..., float | int] = rand.randrange if integral else rand.uniform

    @overload
    def delay(self: ExponentialBackoff[Literal[True]]) -> int: ...

    @overload
    def delay(self: ExponentialBackoff[Literal[False]]) -> float: ...

    @overload
    def delay(self: ExponentialBackoff[bool]) -> float | int: ...

    def delay(self) -> float | int:
        """Compute the next delay

        Returns the next delay to wait according to the exponential
        backoff algorithm.  This is a value between 0 and base * 2^exp
        where exponent starts off at 1 and is incremented at every
        invocation of this method up to a maximum of 10.

        If a period of more than base * 2^11 has passed since the last
        retry, the exponent is reset to 1.
        """
        invocation = time.monotonic()
        interval = invocation - self._last_invocation
        self._last_invocation = invocation

        if interval > self._reset_time:
            self._exp = 0

        self._exp = min(self._exp + 1, self._max)
        return self._randfunc(0, self._base * 2**self._exp)

    def reset(self) -> None:
        """Reset the backoff to its initial state."""
        self._exp = 0
        self._last_invocation = time.monotonic()


class SleepHandle:
    __slots__ = ("future", "loop", "handle")

    def __init__(self, dt: datetime.datetime, *, loop: asyncio.AbstractEventLoop) -> None:
        self.loop: asyncio.AbstractEventLoop = loop
        self.future: asyncio.Future[None] = loop.create_future()
        relative_delta = self.compute_timedelta(dt)
        self.handle = loop.call_later(relative_delta, self._wrapped_set_result, self.future)

    @staticmethod
    def compute_timedelta(dt: datetime.datetime) -> float:
        if dt.tzinfo is None:
            dt = dt.astimezone()
        now = datetime.datetime.now(datetime.timezone.utc)
        return max((dt - now).total_seconds(), 0)

    @staticmethod
    def _wrapped_set_result(future: asyncio.Future) -> None:
        if not future.done():
            future.set_result(None)

    def recalculate(self, dt: datetime.datetime) -> None:
        self.handle.cancel()
        relative_delta = self.compute_timedelta(dt)
        self.handle = self.loop.call_later(relative_delta, self._wrapped_set_result, self.future)

    def wait(self) -> asyncio.Future[Any]:
        return self.future

    def done(self) -> bool:
        return self.future.done()

    def cancel(self) -> None:
        self.handle.cancel()
        self.future.cancel()


class Loop[LF: Callable[..., Coroutine[Any, Any, Any]]]:
    """A background task helper that abstracts the loop and reconnection logic for you.

    The main interface to create this is through :func:`loop`.
    """

    def __init__(
        self,
        coro: LF,
        seconds: float,
        hours: float,
        minutes: float,
        time: datetime.time | Sequence[datetime.time],
        count: Optional[int],
        reconnect: bool,
        name: Optional[str],
    ) -> None:
        self.coro: LF = coro
        self.reconnect: bool = reconnect
        self.count: Optional[int] = count
        self._current_loop = 0
        self._handle: Optional[SleepHandle] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._injected = None
        self._valid_exception = (
            OSError,
            asyncio.TimeoutError,
            asyncio.CancelledError,
        )

        self._before_loop = None
        self._after_loop = None
        self._is_being_cancelled = False
        self._has_failed = False
        self._stop_next_iteration = False
        self._name: str = f"tasks: {coro.__qualname__}" if name is None else name

        if self.count is not None and self.count <= 0:
            raise ValueError("Count must be greater than 0 or None.")

        self.change_interval(seconds=seconds, minutes=minutes, hours=hours, time=time)
        self._last_iteration_failed = False
        self._last_iteration: datetime.datetime = MISSING
        self._next_iteration = None

        if not inspect.iscoroutinefunction(self.coro):
            raise TypeError("The function passed must be a coroutine function.")

    async def _call_loop_function(self, name: str, *args: Any, **kwargs: Any):
        coro = getattr(self, "_" + name)
        if coro is None:
            return

        if self._injected is not None:
            await coro(self._injected, *args, **kwargs)
        else:
            await coro(*args, **kwargs)

    def _try_sleep_until(self, dt: datetime.datetime):
        self._handle = SleepHandle(dt, loop=asyncio.get_event_loop())
        return self._handle.wait()

    def _is_relative_time(self) -> bool:
        return self._time is MISSING

    def _is_explicit_time(self) -> bool:
        return self._time is not MISSING

    async def _loop(self, *args: Any, **kwargs: Any) -> None:
        backoff = ExponentialBackoff()
        await self._call_loop_function("before_loop")
        self._last_iteration_failed = False
        if self._is_explicit_time():
            self._next_iteration = self._get_next_sleep_time()
        else:
            self._next_iteration = datetime.datetime.now(datetime.timezone.utc)
            await asyncio.sleep(0)
        try:
            if self._stop_next_iteration:
                return
            while True:
                if self._is_explicit_time():
                    await self._try_sleep_until(self._next_iteration)
                if not self._last_iteration_failed:
                    self._last_iteration = self._next_iteration
                    self._next_iteration = self._get_next_sleep_time()

                    while self._is_explicit_time() and self._next_iteration <= self._last_iteration:
                        # todo: log warning
                        await self._try_sleep_until(self._next_iteration)
                        self._next_iteration = self._get_next_sleep_time()
                try:
                    await self.coro(*args, **kwargs)
                    self._last_iteration_failed = False
                    backoff.reset()
                except self._valid_exception as exc:
                    self._last_iteration_failed = True
                    if not self.reconnect:
                        raise exc from exc

                    retry_after = backoff.delay()
                    # todo: log exception
                    await asyncio.sleep(retry_after)
                else:
                    if self._stop_next_iteration:
                        return

                    if self._is_relative_time():
                        await self._try_sleep_until(self._next_iteration)

                    self._current_loop += 1
                    if self._current_loop == self.count:
                        break

        except asyncio.CancelledError:
            self._is_being_cancelled = True
            raise
        except Exception as exc:
            self._has_failed = True
            await self._call_loop_function("error", exc)
            raise exc
        finally:
            await self._call_loop_function("after_loop")
            if self._handle:
                self._handle.cancel()
            self._is_being_cancelled = False
            self._current_loop = 0

    def __get__[T](self, obj: T, objtype: type[T]) -> Loop[LF]:
        if obj is None:
            return self

        copy: Loop[LF] = Loop(
            self.coro,
            seconds=self._seconds,
            hours=self._hours,
            minutes=self._minutes,
            time=self._time,
            count=self.count,
            reconnect=self.reconnect,
            name=self._name,
        )
        copy._injected = obj
        copy._before_loop = self._before_loop
        copy._after_loop = self._after_loop
        setattr(obj, self.coro.__name__, copy)
        return copy

    @property
    def seconds(self) -> Optional[float]:
        if self._seconds is not MISSING:
            return self._seconds

    @property
    def minutes(self) -> Optional[float]:
        if self._minutes is not MISSING:
            return self._minutes

    @property
    def hours(self) -> Optional[float]:
        if self._hours is not MISSING:
            return self._hours

    @property
    def time(self) -> Optional[list[datetime.time]]:
        if self._time is not MISSING:
            return self._time.copy()

    @property
    def current_loop(self) -> int:
        return self._current_loop

    @property
    def next_iteration(self) -> Optional[datetime.datetime]:
        if self._task is MISSING:
            return None
        elif self._task and self._task.done() or self._stop_next_iteration:
            return None
        return self._next_iteration

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._injected is not None:
            args = (self._injected, *args)

        return await self.coro(*args, **kwargs)

    def start(self, *args: Any, **kwargs: Any) -> asyncio.Task[None]:
        if self._task and not self._task.done():
            raise RuntimeError("Task is already launched and is not completed")

        if self._injected is not None:
            args = (self._injected, *args)

        self._has_failed = False
        self._task = asyncio.create_task(self._loop(*args, **kwargs), name=self._name)
        return self._task

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._stop_next_iteration = True

    def _can_be_cancelled(self) -> bool:
        return bool(not self._is_being_cancelled and self._task and not self._task.done())

    def cancel(self) -> None:
        if self._can_be_cancelled() and self._task:
            self._task.cancel()

    def restart(self, *args: Any, **kwargs: Any) -> None:
        def restart_when_over(fut: Any, *, args: Any = args, kwargs: Any = kwargs) -> None:
            if self._task:
                self._task.remove_done_callback(restart_when_over)
            self.start(*args, **kwargs)

        if self._can_be_cancelled() and self._task:
            self._task.add_done_callback(restart_when_over)
            self._task.cancel()

    def add_exception_type(self, *exceptions: type[BaseException]) -> None:
        for exc in exceptions:
            if not inspect.isclass(exc):
                raise TypeError(f"{exc!r} must be a class.")
            if not issubclass(exc, BaseException):
                raise TypeError(f"{exc!r} must inherit from BaseException.")

        self._valid_exception = (*self._valid_exception, *exceptions)

    def clear_exception_types(self) -> None:
        self._valid_exception = ()

    def remove_exception_type(self, *exceptions: type[BaseException]) -> bool:
        old_length = len(self._valid_exception)
        self._valid_exception = tuple(x for x in self._valid_exception if x not in exceptions)
        return len(self._valid_exception) == old_length - len(exceptions)

    def get_task(self) -> Optional[asyncio.Task[None]]:
        return self._task if self._task is not MISSING else None

    def is_being_cancelled(self) -> bool:
        return self._is_being_cancelled

    def failed(self) -> bool:
        return self._has_failed

    def is_running(self) -> bool:
        return not bool(self._task.done()) if self._task else False

    async def _error(self, *args: Any) -> None:
        exception: Exception = args[-1]
        # todo: log error

    def before_loop[FT: Callable[..., Coroutine[Any, Any, Any]]](self, coro: FT) -> FT:
        if not inspect.iscoroutinefunction(coro):
            raise TypeError(f"Expected coroutine function, received {coro.__class__.__name__}.")

        self._before_loop = coro
        return coro

    def after_loop[FT: Callable[..., Coroutine[Any, Any, Any]]](self, coro: FT) -> FT:
        if not inspect.iscoroutinefunction(coro):
            raise TypeError(f"Expected coroutine function, received {coro.__class__.__name__}.")

        self._after_loop = coro
        return coro

    def error[ET: Callable[[Any, BaseException], Coroutine[Any, Any, Any]]](self, coro: ET) -> ET:
        if not inspect.iscoroutinefunction(coro):
            raise TypeError(f"Expected coroutine function, received {coro.__class__.__name__}.")

        self._error = coro  # type: ignore
        return coro

    def is_ambiguous(self, dt: datetime.datetime) -> bool:
        if dt.tzinfo is None or isinstance(dt.tzinfo, datetime.timezone):
            # Naive or fixed timezones are never ambiguous
            return False

        before = dt.replace(fold=0)
        after = dt.replace(fold=1)

        same_offset = before.utcoffset() == after.utcoffset()
        same_dst = before.dst() == after.dst()
        return not (same_offset and same_dst)

    def is_imaginary(self, dt: datetime.datetime) -> bool:
        if dt.tzinfo is None or isinstance(dt.tzinfo, datetime.timezone):
            # Naive or fixed timezones are never imaginary
            return False

        tz = dt.tzinfo
        dt = dt.replace(tzinfo=None)
        roundtrip = dt.replace(tzinfo=tz).astimezone(datetime.timezone.utc).astimezone(tz).replace(tzinfo=None)
        return dt != roundtrip

    def resolve_datetime(self, dt: datetime.datetime) -> datetime.datetime:
        if dt.tzinfo is None or isinstance(dt.tzinfo, datetime.timezone):
            # Naive or fixed requires no resolution
            return dt

        if self.is_imaginary(dt):
            # Largest gap is probably 24 hours
            tomorrow = dt + datetime.timedelta(days=1)
            yesterday = dt - datetime.timedelta(days=1)
            # utcoffset shouldn't return None since these are aware instances
            # If it returns None then the timezone implementation was broken from the get go
            return dt + (tomorrow.utcoffset() - yesterday.utcoffset())  # type: ignore
        elif self.is_ambiguous(dt):
            return dt.replace(fold=1)
        else:
            return dt

    def _get_next_sleep_time(self, now: datetime.datetime = MISSING) -> datetime.datetime:
        if self._sleep is not MISSING:
            return self._last_iteration + datetime.timedelta(seconds=self._sleep)

        if now is MISSING:
            now = datetime.datetime.now(datetime.timezone.utc)

        index = self._start_time_relative_to(now)

        if index is None:
            time = self._time[0]
            tomorrow = now.astimezone(time.tzinfo) + datetime.timedelta(days=1)
            date = tomorrow.date()
        else:
            time = self._time[index]
            date = now.astimezone(time.tzinfo).date()

        dt = datetime.datetime.combine(date, time, tzinfo=time.tzinfo)
        return self.resolve_datetime(dt)

    def _start_time_relative_to(self, now: datetime.datetime) -> Optional[int]:
        for idx, time in enumerate(self._time):
            start = now.astimezone(time.tzinfo)
            if time >= start.timetz():
                return idx
        else:
            return None

    def _get_time_parameter(
        self,
        time: datetime.time | Sequence[datetime.time],
        *,
        dt: type[datetime.time] = datetime.time,
        utc: datetime.timezone = datetime.timezone.utc,
    ) -> list[datetime.time]:
        if isinstance(time, dt):
            inner = time if time.tzinfo is not None else time.replace(tzinfo=utc)
            return [inner]
        if not isinstance(time, Sequence):
            raise TypeError(
                f"Expected datetime.time or a sequence of datetime.time for ``time``, received {type(time)!r} instead."
            )
        if not time:
            raise ValueError("time paramteter must not be an empty sequence.")

        ret: list[datetime.time] = []
        for index, t in enumerate(time):
            if not isinstance(t, dt):
                raise TypeError(
                    f"Expected a sequence of {dt!r} for ``time``, received {type(t).__name__!r} at index {index} instead."
                )
            ret.append(t if t.tzinfo is not None else t.replace(tzinfo=utc))

        ret = sorted(set(ret))
        return ret

    def change_interval(
        self,
        *,
        seconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        time: datetime.time | Sequence[datetime.time] = MISSING,
    ) -> None:
        if time is MISSING:
            seconds = seconds or 0
            minutes = minutes or 0
            hours = hours or 0
            sleep = seconds + (minutes * 60) + (hours * 3600)
            if sleep <= 0:
                raise ValueError("Total number of seconds cannot be less than 0.")

            self._sleep = sleep
            self._seconds = float(seconds)
            self._minutes = float(minutes)
            self._hours = float(hours)
            self._time: list[datetime.time] = MISSING
        else:
            if any((seconds, minutes, hours)):
                raise ValueError("Cannot mix explicit and relative time intervals.")
            self._time = self._get_time_parameter(time)
            self._sleep = self._seconds = self._minutes = self._hours = MISSING

        if self.is_running() and self._last_iteration is not MISSING:
            self._next_iteration = self._get_next_sleep_time()
            if self._handle and not self._handle.done():
                self._handle.recalculate(self._next_iteration)


def loop[LF: Callable[..., Coroutine[Any, Any, Any]]](
    *,
    seconds: float = MISSING,
    minutes: float = MISSING,
    hours: float = MISSING,
    time: datetime.time | Sequence[datetime.time] = MISSING,
    count: Optional[int] = None,
    reconnect: bool = True,
    name: Optional[str] = None,
) -> Callable[[LF], Loop[LF]]:
    """A decorator that transforms a coroutine into a :class:`Loop`.

    This decorator abstracts away the logic needed to create background
    tasks that run at specific intervals or times, handling reconnections
    and exceptions as needed.

    Parameters
    ----------
    seconds: :class:`float`
        The number of seconds to wait between each iteration.
    minutes: :class:`float`
        The number of minutes to wait between each iteration.
    hours: :class:`float`
        The number of hours to wait between each iteration.
    time: Union[:class:`datetime.time`, Sequence[:class:`datetime.time`]]
        A specific time or sequence of times at which to run the task.
    count: Optional[:class:`int`]
        The number of times to repeat the task. If ``None``, it will repeat indefinitely.
    reconnect: :class:`bool`
        Whether to attempt reconnection on failure.
    name: Optional[:class:`str`]
        An optional name for the task.

    Raises
    ------
    ValueError
        If the provided interval parameters are invalid.

    Returns
    -------
    Callable[[LF], Loop[LF]]
        A decorator that converts a coroutine function into a :class:`Loop`.
    """

    def decorator(func: LF) -> Loop[LF]:
        return Loop(
            func,
            seconds=seconds if seconds is not MISSING else 0,
            hours=hours if hours is not MISSING else 0,
            minutes=minutes if minutes is not MISSING else 0,
            time=time,
            count=count,
            reconnect=reconnect,
            name=name,
        )

    return decorator


__all__ = ("ExponentialBackoff", "loop")
