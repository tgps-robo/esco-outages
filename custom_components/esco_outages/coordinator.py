"""Coordinate ESCO API polling, the day's plan and exact schedule-boundary updates."""
import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from aiohttp import ClientError
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MODE_UNAVAILABLE, POWER_MODES, URL
from .schedule import TZ, decode, merge_plan, next_transition, outage_intervals

_LOGGER = logging.getLogger(__name__)


def plan_store_for(hass, entry_id):
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.plan")


async def fetch(hass, line):
    """Fetch and decode one ESCO schedule."""
    try:
        async with asyncio.timeout(15):
            async with async_get_clientsession(hass).get(URL, params={"line": line}) as response:
                if response.status == 404:
                    # Only a structured schedule miss is a successful API response.
                    body = await response.json()
                    if isinstance(body, dict) and body.get("line") == line and body.get("error"):
                        return decode({"line": line, "date": None}, line)
                response.raise_for_status()
                return decode(await response.json(), line)
    except (ClientError, TimeoutError, ValueError, KeyError, TypeError) as err:
        raise UpdateFailed(f"ESCO schedule request failed: {err}") from err


class ScheduleCoordinator(DataUpdateCoordinator):
    """One HTTP request per refresh for all ESCO entities, plus local updates at boundaries."""

    def __init__(self, hass, entry):
        settings = {**entry.data, **entry.options}
        self.line = settings["line"]
        self.power_entity = settings.get("power_entity") or None
        mode = settings.get("power_mode")
        self.power_mode = mode if mode in POWER_MODES else MODE_UNAVAILABLE
        self.tracker = None
        self._transition_unsub = None
        # The day's plan as the calendar shows it: the latest schedule plus the intervals
        # of earlier versions of it that are already over. Kept across restarts.
        self.plan_date = None
        self.plan = []  # [(start, end)]
        self._plan_store = plan_store_for(hass, entry.entry_id)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            always_update=False,
            update_interval=timedelta(minutes=settings["interval"]),
        )

    # ---- the day's plan -------------------------------------------------------------

    async def async_load_plan(self):
        stored = await self._plan_store.async_load() or {}
        try:
            self.plan_date = date.fromisoformat(stored["date"])
            self.plan = [(dt_util.parse_datetime(start), dt_util.parse_datetime(end))
                         for start, end in stored["plan"]]
            if any(start is None or end is None for start, end in self.plan):
                raise ValueError("bad interval")
        except (KeyError, TypeError, ValueError):
            self.plan_date, self.plan = None, []

    def plan_events(self):
        """(start, end, minutes) of the day's plan, oldest first."""
        return [(start, end, int((end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() // 60))
                for start, end in self.plan]

    def _merge_plan(self, data, now):
        day = data.get("date")
        if day is None:
            return  # no schedule published (yet): keep what is known
        current = [(start, end) for start, end, _ in outage_intervals(data)]
        plan = merge_plan(self.plan, current, now, datetime.combine(day, time.min, TZ))
        if day != self.plan_date or plan != self.plan:
            self.plan_date, self.plan = day, plan
            self.hass.async_create_task(self._plan_store.async_save({
                "date": day.isoformat(),
                "plan": [[start.isoformat(), end.isoformat()] for start, end in plan],
            }))

    # ---- exact updates at the edges of the planned outages ------------------------------

    def _cancel_transition(self):
        if self._transition_unsub is not None:
            self._transition_unsub()
            self._transition_unsub = None

    def _schedule_transition(self, data):
        self._cancel_transition()
        transition = next_transition(data, dt_util.now())
        if transition is not None:
            self._transition_unsub = async_track_point_in_time(
                self.hass, self._handle_transition, transition
            )

    @callback
    def _handle_transition(self, _now):
        self._transition_unsub = None
        today = dt_util.now().astimezone(TZ).date()
        if self.data and self.data.get("date") != today:
            self.hass.async_create_task(self.async_request_refresh())
            return
        self.async_update_listeners()
        self._schedule_transition(self.data)

    def cancel_transition_timer(self):
        """Cancel the scheduled callback when the config entry unloads."""
        self._cancel_transition()

    async def _async_update_data(self):
        data = await fetch(self.hass, self.line)
        polled_at = dt_util.utcnow()
        data["last_poll"] = polled_at.isoformat()
        data["next_update"] = (polled_at + self.update_interval).isoformat()
        self._merge_plan(data, polled_at)
        self._schedule_transition(data)
        return data
