"""ESCO outage schedule integration."""
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import ScheduleCoordinator, plan_store_for
from .tracker import PowerTracker, store_for

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CALENDAR]


async def async_setup_entry(hass, entry):
    coordinator = ScheduleCoordinator(hass, entry)
    await coordinator.async_load_plan()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Remove entities of earlier builds that no longer exist. Exact unique IDs owned by
    # this config entry are used, so unrelated entities are never touched.
    registry = er.async_get(hass)
    obsolete = (
        ("sensor", f"{entry.entry_id}_next_outage"),
        ("sensor", f"{entry.entry_id}_outage_schedule"),
        ("calendar", f"{entry.entry_id}_actual_calendar"),
    )
    for platform, unique_id in obsolete:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)

    if coordinator.power_entity:
        tracker = PowerTracker(hass, entry.entry_id, coordinator.power_entity, coordinator.power_mode)
        await tracker.async_start()
        entry.async_on_unload(tracker.stop)
        coordinator.tracker = tracker

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry.runtime_data.cancel_transition_timer()
    return unload_ok


async def async_remove_entry(hass, entry):
    await store_for(hass, entry.entry_id).async_remove()
    await plan_store_for(hass, entry.entry_id).async_remove()
