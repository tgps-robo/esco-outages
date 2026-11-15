"""Decode, normalize and evaluate the ESCO daily outage schedule."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Kyiv")


def _parse_clock(value, day):
    """Convert an HH:MM value into a timezone-aware Kyiv datetime."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid outage time")
    parsed = datetime.strptime(value, "%H:%M").time()
    return datetime.combine(day, parsed, TZ)


def _parse_timestamp(value):
    """Parse the API's last-update timestamp."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid update timestamp")
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    raise ValueError("Invalid update timestamp")


def _empty_schedule(line, last_updated=None):
    """Return a valid API response when ESCO has no schedule for the line."""
    return {
        "line": line,
        "date": None,
        "last_updated": last_updated,
        "schedule_available": False,
        "next_outage_time": None,
        "next_outage_minutes": None,
        "current_outage_time": None,
        "current_outage_minutes": None,
        "total_outage_minutes": None,
        "outages": [],
    }


def _add_minutes(start, minutes):
    """Add elapsed minutes in UTC to handle DST transitions correctly."""
    return (start.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(TZ)


def decode(payload, line):
    """Validate the ESCO response and normalize all schedule times."""
    if not isinstance(payload, dict) or payload.get("line") != line:
        raise ValueError("Unexpected line")

    last_updated = _parse_timestamp(payload.get("dt"))
    raw_date = payload.get("date")
    if raw_date is None:
        return _empty_schedule(line, last_updated)
    if not isinstance(raw_date, str):
        raise ValueError("Invalid schedule date")

    day = datetime.strptime(raw_date, "%d.%m.%Y").date()
    next_clock = payload.get("next_outage_time")
    next_minutes = payload.get("next_outage_minutes")
    current_clock = payload.get("current_outage_time")
    current_minutes = payload.get("current_outage_minutes")
    total = payload.get("total_outage_minutes")

    if (next_clock is None) != (next_minutes is None):
        raise ValueError("Incomplete next outage")
    if (current_clock is None) != (current_minutes is None):
        raise ValueError("Incomplete current outage")
    if type(total) is not int or not 0 <= total <= 1440:
        raise ValueError("Invalid daily duration")

    for label, minutes in (("next", next_minutes), ("current", current_minutes)):
        if minutes is not None and (type(minutes) is not int or not 0 < minutes <= total):
            raise ValueError(f"Invalid {label} outage duration")

    next_start = _parse_clock(next_clock, day)
    current_start = _parse_clock(current_clock, day)

    # `outages` is present in the newer API. Older responses may contain only
    # current/next outage fields, so a missing or null array is accepted and
    # outage_intervals() will use those fields as a fallback.
    raw_outages = payload.get("outages")
    if raw_outages is None:
        raw_outages = []
    if not isinstance(raw_outages, list):
        raise ValueError("Invalid outage list")

    outages = []
    seen_starts = set()
    for item in raw_outages:
        if not isinstance(item, dict):
            raise ValueError("Invalid outage item")
        # Newer servers also list the intervals of tomorrow, each with its own date.
        # An item without a date belongs to the date of the response.
        item_date = item.get("date")
        if item_date is not None and not isinstance(item_date, str):
            raise ValueError("Invalid outage date")
        item_day = datetime.strptime(item_date, "%d.%m.%Y").date() if item_date else day
        start = _parse_clock(item.get("start"), item_day)
        duration = item.get("duration_minutes")
        if start is None or type(duration) is not int or not 0 < duration <= 1440:
            raise ValueError("Invalid outage interval")
        if start in seen_starts:
            continue
        seen_starts.add(start)
        outages.append({
            "start": start.isoformat(),
            "duration_minutes": duration,
        })

    return {
        "line": line,
        "date": day,
        "last_updated": last_updated,
        "schedule_available": True,
        "next_outage_time": next_start,
        "next_outage_minutes": next_minutes,
        "current_outage_time": current_start,
        "current_outage_minutes": current_minutes,
        "total_outage_minutes": total,
        "outages": outages,
    }


def _as_datetime(value):
    """Accept normalized ISO strings and datetime values."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed
    raise ValueError("Invalid outage start")


def outage_intervals(data):
    """Return sorted (start, end, minutes) for all planned outages.

    Prefer the full `outages` array, but supplement it with current/next fields
    for compatibility with older ESCO responses that do not provide the array.
    """
    if not data or data.get("date") is None:
        return []

    found = {}
    for item in data.get("outages") or []:
        start = _as_datetime(item["start"]).astimezone(TZ)
        minutes = item["duration_minutes"]
        found.setdefault(start, minutes)

    for start, minutes in (
        (data.get("current_outage_time"), data.get("current_outage_minutes")),
        (data.get("next_outage_time"), data.get("next_outage_minutes")),
    ):
        if start is not None and minutes is not None:
            start = _as_datetime(start).astimezone(TZ)
            found.setdefault(start, minutes)

    return sorted(
        (start, _add_minutes(start, minutes), minutes)
        for start, minutes in found.items()
    )


def _merged_intervals(data):
    """Merge overlapping or directly adjacent intervals for state calculations."""
    merged = []
    for start, end, _minutes in outage_intervals(data):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def power_state(data, now):
    """Return scheduled power state and the next outage/restoration timestamps."""
    if not data or not data.get("schedule_available"):
        return {"power_available": None, "next_outage": None, "power_restore": None}

    now = now.astimezone(TZ)
    schedule_day = data.get("date")
    if schedule_day not in (now.date(), now.date() - timedelta(days=1)):
        return {"power_available": None, "next_outage": None, "power_restore": None}

    intervals = _merged_intervals(data)
    if not intervals and data.get("total_outage_minutes", 0) > 0:
        # The API claims outages exist but supplied no usable intervals. Do not
        # incorrectly tell automations that power is definitely available.
        return {"power_available": None, "next_outage": None, "power_restore": None}

    for start, end in intervals:
        if start <= now < end:
            return {"power_available": False, "next_outage": None, "power_restore": end}

    # A previous-day schedule is useful only for an outage crossing midnight.
    if schedule_day != now.date():
        return {"power_available": None, "next_outage": None, "power_restore": None}

    for start, end in intervals:
        if start > now:
            return {"power_available": True, "next_outage": start, "power_restore": end}

    return {"power_available": True, "next_outage": None, "power_restore": None}


def next_transition(data, now):
    """Return the next schedule boundary that should refresh entity states."""
    if not data or not data.get("schedule_available"):
        return None

    now = now.astimezone(TZ)
    schedule_day = data.get("date")
    if schedule_day not in (now.date(), now.date() - timedelta(days=1)):
        return None

    intervals = _merged_intervals(data)
    if schedule_day != now.date():
        active_ends = [end for start, end in intervals if start <= now < end]
        return max(active_ends) if active_ends else None

    boundaries = [point for interval in intervals for point in interval]
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, TZ)
    future = [point for point in boundaries if point > now]
    future.append(midnight)
    return min(future)


def merge_plan(previous, current, now, day_start=None):
    """Fold a freshly fetched day plan into what was known earlier the same day.

    The server may rewrite the schedule several times a day and stop listing intervals
    that are already over. Those are kept, so the calendar still shows (and can still
    verify) what was planned this morning. Intervals still ahead are not kept: if the
    new schedule drops or moves one, the plan was changed. A past interval the schedule
    no longer lists is cut at `now`, since that is as far as it is known to have stood.

    day_start: start of the schedule's day. An interval that ended before it belongs to
    an earlier day and is dropped; one that began before it and runs into the day (an
    outage over midnight) is kept as it was announced, because no schedule of this day
    will ever list it again.

    previous, current: [(start, end)]. Returns sorted [(start, end)].
    """
    merged = dict(current)
    for start, end in previous:
        if start in merged:
            continue
        if day_start is not None:
            if end <= day_start:
                continue
            if start < day_start:
                merged[start] = end
                continue
        if start >= now:
            continue
        merged[start] = min(end, now)
    return sorted(merged.items())


def plan_status(start, end, now, actual, tracked):
    """Compare a planned outage with recorded real outages."""
    if now < start:
        return "planned"
    overlapped = any(a < end and (b is None or b > start) for a, b in actual)
    if start <= now < end:
        if any(a <= now and (b is None or b > now) for a, b in actual):
            return "active"
        if overlapped:
            return "done"
        return "present" if tracked else "planned"
    if overlapped:
        return "done"
    return "missed" if tracked else "planned"


def unplanned_segments(actual, planned, floor, minimum):
    """Return portions of actual outages not covered by planned windows."""
    result = []
    for start, end, ongoing in actual:
        first = max(start, floor)
        if end <= first:
            continue
        pieces = [(first, end)]
        for plan_start, plan_end in planned:
            cut = []
            for piece_start, piece_end in pieces:
                if plan_end <= piece_start or plan_start >= piece_end:
                    cut.append((piece_start, piece_end))
                    continue
                if piece_start < plan_start:
                    cut.append((piece_start, plan_start))
                if plan_end < piece_end:
                    cut.append((plan_end, piece_end))
            pieces = cut
        whole = pieces == [(start, end)]
        for piece_start, piece_end in pieces:
            if whole or piece_end - piece_start >= minimum:
                result.append((piece_start, piece_end, ongoing and piece_end == end))
    return sorted(result)
