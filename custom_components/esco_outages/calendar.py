"""Calendar of scheduled outages, checked against what really happened."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MIN_LEFTOVER
from .schedule import plan_status, unplanned_segments

PARALLEL_UPDATES = 0

# Icon in front of a scheduled outage, by how it compares with reality.
ICONS = {"planned": "⚡", "active": "🔴", "present": "🟡", "done": "✅", "missed": "❌"}
UNPLANNED_ICON = "🔴"

TEXT = {
    "uk": {"planned": "Відключення світла", "unplanned": "Позапланове відключення",
           "hours": "год", "minutes": "хв",
           "active": "триває", "present": "світло є", "done": "було", "missed": "не було"},
    "ru": {"planned": "Отключение света", "unplanned": "Внеплановое отключение",
           "hours": "ч", "minutes": "мин",
           "active": "идёт", "present": "свет есть", "done": "было", "missed": "не было"},
    "en": {"planned": "Power outage", "unplanned": "Unscheduled outage",
           "hours": "h", "minutes": "min",
           "active": "ongoing", "present": "power on", "done": "happened", "missed": "didn't happen"},
}


def texts(language):
    return TEXT.get((language or "en").split("-")[0], TEXT["en"])


def duration(text, minutes):
    hours, rest = divmod(max(1, minutes), 60)
    parts = []
    if hours:
        parts.append(f"{hours} {text['hours']}")
    if rest:
        parts.append(f"{rest} {text['minutes']}")
    return " ".join(parts)


def planned_title(text, status, minutes):
    title = f"{ICONS[status]} {text['planned']} · {duration(text, minutes)}"
    # A plain planned outage needs no word; every verified status gets one next to its icon.
    return title if status == "planned" else f"{title} · {text[status]}"


def unplanned_title(text, minutes, ongoing):
    title = f"{UNPLANNED_ICON} {text['unplanned']} · {duration(text, minutes)}"
    return f"{title} · {text['active']}" if ongoing else title


async def async_setup_entry(hass, entry, async_add_entities):
    # Version 1.4.0 had a second calendar for real outages; they now live in this one.
    registry = er.async_get(hass)
    leftover = registry.async_get_entity_id("calendar", DOMAIN, f"{entry.entry_id}_actual_calendar")
    if leftover:
        registry.async_remove(leftover)
    async_add_entities([OutageCalendar(entry)])


class OutageCalendar(CoordinatorEntity, CalendarEntity):
    """The ESCO schedule. With a power entity configured, every planned outage also says
    whether it really happened, and outages nobody planned are added as their own events."""

    _attr_has_entity_name = True
    _attr_translation_key = "outages"
    _attr_icon = "mdi:transmission-tower-off"

    def __init__(self, entry):
        super().__init__(entry.runtime_data)
        self._attr_translation_placeholders = {"line": entry.runtime_data.line}
        self._attr_unique_id = f"{entry.entry_id}_calendar"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self.coordinator.tracker is not None:
            self.async_on_remove(self.coordinator.tracker.add_listener(self.async_write_ha_state))

    def _events(self):
        coordinator = self.coordinator
        tracker = coordinator.tracker
        text = texts(self.hass.config.language)
        now = dt_util.utcnow()
        actual = tracker.actual() if tracker else []
        planned = coordinator.plan_events()

        events = []
        for start, end, minutes in planned:
            status = plan_status(start, end, now, actual, tracker.tracked(start, end)) if tracker else "planned"
            events.append(CalendarEvent(
                start=start,
                end=end,
                summary=planned_title(text, status, minutes),
                uid=f"{coordinator.line}-{start.isoformat()}",
            ))
        if tracker is None:
            return events

        # An outage that is still going on ends at the next full minute, so its event
        # grows with time and the calendar stays "on" while the power is off.
        open_end = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        real = [(start, end or open_end, end is None) for start, end in actual]
        # The schedule only knows its own day: older real outages cannot be told from planned ones.
        day = coordinator.plan_date
        floor = (datetime.combine(day, time.min, ZoneInfo("Europe/Kyiv")) if day is not None
                 else dt_util.start_of_local_day())
        for start, end, ongoing in unplanned_segments(
            real, [(start, end) for start, end, _ in planned], floor, timedelta(seconds=MIN_LEFTOVER)
        ):
            minutes = int(((now if ongoing else end) - start).total_seconds() // 60)
            events.append(CalendarEvent(
                start=dt_util.as_local(start),
                end=dt_util.as_local(max(end, start + timedelta(minutes=1))),
                summary=unplanned_title(text, minutes, ongoing),
                uid=f"unplanned-{start.isoformat()}",
            ))
        return sorted(events, key=lambda event: event.start)

    @property
    def event(self):
        """The outage in progress, otherwise the next one."""
        now = dt_util.now()
        return next((event for event in self._events() if event.end > now), None)

    async def async_get_events(self, hass, start_date, end_date):
        return [event for event in self._events() if event.start < end_date and event.end > start_date]
