"""Record real outages from an unavailable device or an explicit outage flag."""
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    GAP_AFTER,
    HEARTBEAT,
    MODE_SWITCH_ON,
    MODE_UNAVAILABLE,
    OUTAGE_DELAY,
    RETENTION_DAYS,
)

DOWN = ("unavailable", "unknown")


def outage_state(state, mode=MODE_UNAVAILABLE):
    """Return True for an outage, False for power present, or None if it can't be told.

    MODE_UNAVAILABLE: the monitored device has no power when it is down, e.g. a Wi-Fi
    relay. unavailable/unknown (or a missing entity) is an outage, anything else is not.

    MODE_SWITCH_ON: the entity is an explicit outage flag. ``on`` means power is off,
    ``off`` means power is present. unavailable/unknown means the signal itself is lost:
    that is not proof of an outage, so None is returned and the time is recorded as a gap.
    """
    if mode == MODE_SWITCH_ON:
        if state is None or state.state in DOWN:
            return None
        if state.state == "on":
            return True
        if state.state == "off":
            return False
        return None
    return state is None or state.state in DOWN


def store_for(hass, entry_id):
    return Store(hass, 1, f"{DOMAIN}.{entry_id}")


def _parse(value):
    return dt_util.parse_datetime(value) if value else None


class PowerTracker:
    """Turn a sustained signal into persistent real-outage records.

    In MODE_SWITCH_ON, ``on`` means power outage and ``off`` means power present, and
    unknown/unavailable is recorded as a coverage gap, not as an outage. In
    MODE_UNAVAILABLE, unavailable/unknown means the monitored device went down.

    The outage start is the original state-change time, not the time when the
    confirmation delay expires. Coverage gaps are kept separately so the calendar
    never treats missing observations as proof that a planned outage did not happen.
    """

    def __init__(self, hass, entry_id, entity_id, mode=MODE_UNAVAILABLE):
        self.hass = hass
        self.entity_id = entity_id
        self.mode = mode
        self._store = store_for(hass, entry_id)
        self._listeners = []
        self._unsub = []
        self._timer = None
        self._pending = None
        self._since = None
        self._alive = None
        self._outages = []
        self._gaps = []
        self._gap_open = None

    async def async_start(self):
        now = dt_util.utcnow()
        stored = await self._store.async_load() or {}
        if stored.get("entity") != self.entity_id:
            stored = {}

        self._since = _parse(stored.get("since")) or now
        self._outages = [
            [_parse(item["start"]), _parse(item.get("end"))]
            for item in stored.get("outages", [])
        ]
        self._gaps = [(_parse(start), _parse(end)) for start, end in stored.get("gaps", [])]
        self._gap_open = _parse(stored.get("gap_open"))
        alive = _parse(stored.get("alive"))
        if alive is not None and (now - alive).total_seconds() > GAP_AFTER:
            gap_start = self._gap_open or alive
            if gap_start < now:
                self._gaps.append((gap_start, now))
            self._gap_open = None
        self._prune(now)

        status = outage_state(self.hass.states.get(self.entity_id), self.mode)
        open_outage = self._open()
        if status is None:
            # For an explicit switch flag, unknown/unavailable means that the
            # observation source is unavailable. Do not mislabel it as an outage.
            self._cancel_timer()
            if open_outage is not None:
                open_outage[1] = self._gap_open or now
            if self._gap_open is None:
                self._gap_open = now
        else:
            if self._gap_open is not None:
                if self._gap_open < now:
                    self._gaps.append((self._gap_open, now))
                self._gap_open = None
            if open_outage is not None and not status:
                open_outage[1] = now
            elif open_outage is None and status:
                self._begin_pending(now)

        self._alive = now
        await self._save()
        self._unsub.append(
            async_track_state_change_event(self.hass, [self.entity_id], self._on_state)
        )
        self._unsub.append(
            async_track_time_interval(self.hass, self._beat, timedelta(seconds=HEARTBEAT))
        )

    @callback
    def stop(self):
        for unsubscribe in self._unsub:
            unsubscribe()
        self._unsub = []
        self._cancel_timer()
        self._alive = dt_util.utcnow()
        self.hass.async_create_task(self._save())

    def add_listener(self, callback_):
        self._listeners.append(callback_)
        return lambda: self._listeners.remove(callback_)

    def actual(self):
        """Return actual outages as (start, end_or_None), oldest first."""
        return [(start, end) for start, end in self._outages]

    def tracked(self, start, end):
        """Whether the full [start, end) window has continuous tracker coverage."""
        if start < self._since:
            return False
        if self._gap_open is not None and self._gap_open < end:
            return False
        # While an explicit switch has just turned on, wait for the 180-second
        # debounce before claiming that a planned outage was missed/present.
        if (
            self._pending is not None
            and self._pending < end
            and dt_util.utcnow() < self._pending + timedelta(seconds=OUTAGE_DELAY)
        ):
            return False
        return not any(gap_start < end and gap_end > start for gap_start, gap_end in self._gaps)

    def _open(self):
        return next((outage for outage in reversed(self._outages) if outage[1] is None), None)

    def _begin_pending(self, now):
        self._pending = now
        self._timer = async_call_later(self.hass, OUTAGE_DELAY, self._confirm)

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer()
            self._timer = None
        self._pending = None

    @callback
    def _on_state(self, event):
        now = dt_util.utcnow()
        status = outage_state(event.data["new_state"], self.mode)

        if status is None:
            # The switch itself is temporarily unavailable: close any known
            # outage at the last observable moment and mark the interval as a
            # coverage gap until a valid on/off state returns.
            self._cancel_timer()
            open_outage = self._open()
            if open_outage is not None:
                open_outage[1] = now
            if self._gap_open is None:
                self._gap_open = now
            self._changed()
            return

        if self._gap_open is not None:
            if self._gap_open < now:
                self._gaps.append((self._gap_open, now))
            self._gap_open = None

        if status:
            if self._open() is None and self._pending is None:
                self._begin_pending(now)
            self._changed()
            return

        self._cancel_timer()
        open_outage = self._open()
        if open_outage is not None:
            open_outage[1] = now
        self._changed()

    @callback
    def _confirm(self, _now):
        self._timer = None
        pending, self._pending = self._pending, None
        if pending is None or outage_state(self.hass.states.get(self.entity_id), self.mode) is not True:
            return
        self._outages.append([pending, None])
        self._changed()

    @callback
    def _beat(self, now):
        self._alive = now
        self._prune(now)
        self.hass.async_create_task(self._save())
        # Refresh the calendar periodically while an outage is still open, since
        # its displayed end is extended to the next full minute.
        if self._open() is not None:
            self._notify_listeners()

    def _prune(self, now):
        cutoff = now - timedelta(days=RETENTION_DAYS)
        self._outages = [item for item in self._outages if item[1] is None or item[1] >= cutoff]
        self._gaps = [gap for gap in self._gaps if gap[1] >= cutoff]

    @callback
    def _notify_listeners(self):
        for listener in list(self._listeners):
            listener()

    @callback
    def _changed(self):
        self.hass.async_create_task(self._save())
        self._notify_listeners()

    async def _save(self):
        await self._store.async_save({
            "entity": self.entity_id,
            "since": self._since.isoformat(),
            "alive": self._alive.isoformat() if self._alive else None,
            "gaps": [[start.isoformat(), end.isoformat()] for start, end in self._gaps],
            "gap_open": self._gap_open.isoformat() if self._gap_open else None,
            "outages": [
                {"start": start.isoformat(), "end": end.isoformat() if end else None}
                for start, end in self._outages
            ],
        })
