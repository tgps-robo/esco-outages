"""Binary sensors: whether another outage is scheduled, and whether power should be on."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .schedule import power_state

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([NextOutageExists(entry), EscoPowerAvailableSensor(entry)])


class NextOutageExists(CoordinatorEntity, BinarySensorEntity):
    """Whether a successfully retrieved schedule contains another outage."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_outage_exists"
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, entry):
        super().__init__(entry.runtime_data)
        self._attr_translation_placeholders = {"line": entry.runtime_data.line}
        self._attr_unique_id = f"{entry.entry_id}_next_outage_exists"

    @property
    def available(self):
        return super().available and self.coordinator.data.get("date") is not None

    @property
    def is_on(self):
        return self.coordinator.data.get("next_outage_time") is not None


class EscoPowerAvailableSensor(CoordinatorEntity, BinarySensorEntity):
    """Whether electricity should be available according to the ESCO schedule."""

    _attr_has_entity_name = True
    _attr_translation_key = "power_available"
    _attr_device_class = BinarySensorDeviceClass.POWER
    _attr_icon = "mdi:power-plug"

    def __init__(self, entry):
        super().__init__(entry.runtime_data)
        self._attr_translation_placeholders = {"line": entry.runtime_data.line}
        self._attr_unique_id = f"{entry.entry_id}_power_available"
        self._attr_suggested_object_id = "esco_power_available"

    @property
    def available(self):
        return (
            super().available
            and power_state(self.coordinator.data, dt_util.now())["power_available"] is not None
        )

    @property
    def is_on(self):
        return power_state(self.coordinator.data, dt_util.now())["power_available"]
