"""The config staff change while the service runs (config_changes in app/db/models.py): whether
new calls are taken, and whether the office follows its hours or is forced open or closed.

A call never waits for configuration, and a configuration fault never stops a call. The voice
service holds the values in memory (`LiveConfig`), and call handling reads only that memory:
`/offer` (app/api/offer.py) and the start of a call (app/agent/endings.py). One background task
(`ConfigPoll`) reads the table at start-up and then every POLL_SECONDS; each read is given up
after READ_SECONDS, its clean-up included. A read that fails or is given up keeps the last good
values, and the first failure of an outage is logged; nothing is raised. Until the first read
succeeds the values are unknown, and no new call is taken. A change made through the admin API
therefore reaches the calls within POLL_SECONDS of being written.

The admin process runs no poll: it reads the table when asked (`read_config_bounded`).

Logs carry event names, the closed set of values and exception class names.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    CALLS,
    CALLS_ON,
    CONFIG_DEFAULTS,
    CONFIG_VALUES,
    OFFICE,
    ConfigChange,
)
from app.domain.office_hours import SCHEDULE, office_open

logger = logging.getLogger(__name__)

POLL_SECONDS = 5
READ_SECONDS = 1.0
# A read given up is cancelled, then cancelled again every CANCEL_INTERVAL_SECONDS until it has
# ended (the driver, interrupted a second time while it waits on the server, closes the
# connection), as the turn recorder's writes are (app/agent/turn_record.py).
CANCEL_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True, slots=True, kw_only=True)
class Setting:
    """A key's value, and who changed it last and when; neither for a default."""

    value: str
    changed_by: str | None
    changed_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Values:
    """What call handling runs on: each one of its key's values, or it is refused."""

    calls: str
    office: str

    def __post_init__(self) -> None:
        if self.calls not in CONFIG_VALUES[CALLS] or self.office not in CONFIG_VALUES[OFFICE]:
            raise ValueError("a config value is not one of its key's values")


async def read_config(session: AsyncSession) -> dict[str, Setting]:
    """Every key's setting: its newest change, or its default. A stored value outside its
    key's values is refused."""
    newest = (
        select(
            ConfigChange.key, ConfigChange.value, ConfigChange.changed_by, ConfigChange.changed_at
        )
        .ext(distinct_on(ConfigChange.key))
        .order_by(ConfigChange.key, ConfigChange.id.desc())
    )
    found = {
        row.key: Setting(value=row.value, changed_by=row.changed_by, changed_at=row.changed_at)
        for row in await session.execute(newest)
    }
    settings = {}
    for key, values in CONFIG_VALUES.items():
        setting = found.get(
            key, Setting(value=CONFIG_DEFAULTS[key], changed_by=None, changed_at=None)
        )
        if setting.value not in values:
            raise ValueError("a stored config value is not one of its key's values")
        settings[key] = setting
    return settings


async def read_config_bounded(
    sessions: async_sessionmaker[AsyncSession], seconds: float = READ_SECONDS
) -> dict[str, Setting]:
    """`read_config` in a transaction of its own, given up after `seconds` (TimeoutError), its
    clean-up included."""

    async def read() -> dict[str, Setting]:
        async with sessions() as session:
            return await read_config(session)

    reading = asyncio.ensure_future(read())
    try:
        await asyncio.wait({reading}, timeout=seconds)
    finally:
        given_up = not reading.done()
        while not reading.done():
            reading.cancel()
            await asyncio.wait({reading}, timeout=CANCEL_INTERVAL_SECONDS)
    if given_up:
        raise TimeoutError
    return reading.result()


class LiveConfig:
    """The values in memory, which call handling reads; None until the first good read."""

    def __init__(self, values: Values | None = None) -> None:
        self.values = values

    def takes_calls(self) -> bool:
        """Whether a new call is taken: only with calls on, and never before the values are
        known."""
        return self.values is not None and self.values.calls == CALLS_ON

    def office_open(self, at: datetime) -> bool:
        """The office's answer for a call starting at `at` (`office_open` of
        app/domain/office_hours.py). Before the first read the clock decides."""
        return office_open(at, SCHEDULE if self.values is None else self.values.office)


class ConfigPoll:
    """The one task that keeps a LiveConfig to the table: `start` reads once and then every
    `every` seconds until `stop`."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        live: LiveConfig,
        *,
        every: float = POLL_SECONDS,
        read_seconds: float = READ_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessions = sessions
        self._live = live
        self._every = every
        self._read_seconds = read_seconds
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._failing = False

    async def start(self) -> None:
        """Read the values, then go on reading them in the background."""
        await self._read()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.wait({self._task})
        if not self._task.cancelled():
            self._task.result()

    async def _run(self) -> None:
        while True:
            await self._sleep(self._every)
            await self._read()

    async def _read(self) -> None:
        """One bounded read. The values on success; on any failure the last good values stay
        and the first failure of an outage is logged. Never raises but on cancellation."""
        try:
            found = await read_config_bounded(self._sessions, self._read_seconds)
            values = Values(calls=found[CALLS].value, office=found[OFFICE].value)
        except Exception as error:
            if not self._failing:
                self._failing = True
                logger.warning("runtime_config_read_failed error=%s", type(error).__name__)
            return
        if self._failing:
            self._failing = False
            logger.info("runtime_config_read_recovered")
        if values != self._live.values:
            logger.info("runtime_config_applied calls=%s office=%s", values.calls, values.office)
        self._live.values = values
