"""Read-only sensors backed by the shared ESCO schedule coordinator."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfTime
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .schedule import power_state

PARALLEL_UPDATES = 0
FIELDS = {
    "line": None,
    "date": SensorDeviceClass.DATE,
    "next_outage_time": SensorDeviceClass.TIMESTAMP,
    "next_outage_minutes": SensorDeviceClass.DURATION,
    "current_outage_time": SensorDeviceClass.TIMESTAMP,
    "current_outage_minutes": SensorDeviceClass.DURATION,
    "total_outage_minutes": SensorDeviceClass.DURATION,
    "power_restore": SensorDeviceClass.TIMESTAMP,
}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(ScheduleSensor(entry, key, kind) for key, kind in FIELDS.items())


class ScheduleSensor(CoordinatorEntity, SensorEntity):
    """Expose API fields plus the calculated next restoration timestamp."""

    _attr_has_entity_name = True

    def __init__(self, entry, key, kind):
        super().__init__(entry.runtime_data)
        self.key = key
        self._attr_translation_key = key
        self._attr_translation_placeholders = {"line": entry.runtime_data.line}
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_class = kind
        if kind == SensorDeviceClass.DURATION:
            self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
        if key == "power_restore":
            self._attr_suggested_object_id = "esco_power_restore"
            self._attr_icon = "mdi:calendar-clock"

    @property
    def available(self):
        if not super().available:
            return False
        if self.key == "line":
            return True
        if not self.coordinator.data.get("schedule_available"):
            return False
        if self.key == "power_restore":
            return power_state(self.coordinator.data, dt_util.now())["power_available"] is not None
        return True

    @property
    def native_value(self):
        data = self.coordinator.data
        if self.key == "power_restore":
            return power_state(data, dt_util.now())["power_restore"]
        return data.get(self.key)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        # `esco_field` and `line` let a card find the sensors of one queue even when the
        # entity IDs have been renamed.
        identity = {"esco_field": self.key, "line": self.coordinator.line}
        if self.key == "current_outage_time":
            return {**identity, "duration_minutes": data.get("current_outage_minutes")}
        if self.key != "next_outage_time":
            return identity
        if not data.get("schedule_available"):
            return {**identity, "schedule_status": "missing", "last_poll": data.get("last_poll")}
        return {
            **identity,
            "schedule_status": "on" if data.get("next_outage_time") else "off",
            "next_update": data.get("next_update"),
            "last_poll": data.get("last_poll"),
            "schedule_updated": data["last_updated"].isoformat() if data.get("last_updated") else None,
            "outages": data.get("outages"),
        }
