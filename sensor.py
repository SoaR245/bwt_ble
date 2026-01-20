from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, UnitOfVolume
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATOR, DEFAULT_NAME, DOMAIN
from .coordinator import BwtBleCoordinator
from .ble import BroadcastFrame


@dataclass(frozen=True, kw_only=True)
class BwtBleSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


@dataclass(frozen=True, kw_only=True)
class BwtBleBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool]


SENSORS: tuple[BwtBleSensorDescription, ...] = (
    BwtBleSensorDescription(
        key="remaining_percentage",
        name="Remaining Capacity",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: round(_ensure_broadcast(data).percentage * 100, 2),
    ),
    BwtBleSensorDescription(
        key="remaining_volume",
        name="Remaining Water",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.TOTAL,  # water device class requires total/total_increasing
        value_fn=lambda data: _ensure_broadcast(data).remaining,
    ),
    BwtBleSensorDescription(
        key="water_consumption",
        name="Water Consumption",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.get("water_total"),
    ),
    BwtBleSensorDescription(
        key="last_quarter_consumption",
        name="Last Quarter Hour Consumption",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("last_quarter_consumption", 0),
    ),
    BwtBleSensorDescription(
        key="regeneration_count",
        name="Regeneration Count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:restart",
        value_fn=lambda data: data.get("regen_total", 0),
    ),
)

BINARY_SENSORS: tuple[BwtBleBinarySensorDescription, ...] = (
    BwtBleBinarySensorDescription(
        key="alarm",
        name="Alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda data: _ensure_broadcast(data).alarm,
    ),
)


def _ensure_broadcast(data: dict[str, Any]) -> BroadcastFrame:
    broadcast = data.get("broadcast")
    if not isinstance(broadcast, BroadcastFrame):
        raise ValueError("Coordinator data missing broadcast frame")
    return broadcast


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    storage = hass.data[DOMAIN][entry.entry_id]
    coordinator: BwtBleCoordinator = storage[DATA_COORDINATOR]
    entities: list = [BwtBleSensor(coordinator, entry.entry_id, description) for description in SENSORS]
    entities.extend([BwtBleBinarySensor(coordinator, entry.entry_id, description) for description in BINARY_SENSORS])
    async_add_entities(entities)


class BwtBleSensor(CoordinatorEntity[BwtBleCoordinator], SensorEntity):
    def __init__(
        self,
        coordinator: BwtBleCoordinator,
        entry_id: str,
        description: BwtBleSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            manufacturer="BWT",
            name=DEFAULT_NAME,
        )

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        return self.entity_description.value_fn(data)


class BwtBleBinarySensor(CoordinatorEntity[BwtBleCoordinator], BinarySensorEntity):
    def __init__(
        self,
        coordinator: BwtBleCoordinator,
        entry_id: str,
        description: BwtBleBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            manufacturer="BWT",
            name=DEFAULT_NAME,
        )

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        try:
            return self.entity_description.value_fn(data)
        except (ValueError, KeyError, TypeError):
            return False
