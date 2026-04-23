from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any, Dict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble import BroadcastFrame, async_fetch_snapshot
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.consumption"


class BwtBleCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, *, address: str, scan_interval: int | None = None) -> None:
        update_interval = timedelta(seconds=scan_interval or DEFAULT_SCAN_INTERVAL)
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}-{address}", update_interval=update_interval)
        self._address = address
        self._consumption_total = 0.0
        self._last_quarter_idx: int | None = None
        self._last_regen: int | None = None
        self._last_quarter_consumption = 0.0
        self._regen_total = 0
        # Estimated remaining: the broadcast remaining value updates infrequently
        # (e.g. only after regeneration), so we track consumption since the last
        # broadcast change and subtract it to provide a real-time estimate.
        self._broadcast_remaining_base: int | None = None
        self._consumption_since_base: float = 0.0
        # Storage for persisting consumption across restarts
        storage_key = f"{STORAGE_KEY_PREFIX}.{address.replace(':', '_').lower()}"
        self._store: Store = Store(hass, STORAGE_VERSION, storage_key)
        self._storage_loaded = False

    async def _async_load_storage(self) -> None:
        """Load persisted consumption data from storage."""
        if self._storage_loaded:
            return
        try:
            data = await self._store.async_load()
            if data:
                self._consumption_total = float(data.get("water_total", 0.0))
                self._last_quarter_idx = data.get("last_quarter_idx")
                self._last_regen = data.get("last_regen")
                self._regen_total = int(data.get("regen_total", 0))
                self._broadcast_remaining_base = data.get("broadcast_remaining_base")
                self._consumption_since_base = float(data.get("consumption_since_base", 0.0))
                _LOGGER.debug(
                    "Loaded persisted data: water_total=%.0f, last_q_idx=%s, regen_total=%d",
                    self._consumption_total, self._last_quarter_idx, self._regen_total
                )
        except Exception as err:
            _LOGGER.warning("Failed to load persisted data: %s", err)
        self._storage_loaded = True

    async def _async_save_storage(self) -> None:
        """Save consumption data to persistent storage."""
        try:
            await self._store.async_save({
                "water_total": self._consumption_total,
                "last_quarter_idx": self._last_quarter_idx,
                "last_regen": self._last_regen,
                "regen_total": self._regen_total,
                "broadcast_remaining_base": self._broadcast_remaining_base,
                "consumption_since_base": self._consumption_since_base,
            })
        except Exception as err:
            _LOGGER.warning("Failed to save consumption data: %s", err)

    @property
    def address(self) -> str:
        return self._address

    async def _async_update_data(self) -> Dict[str, Any]:
        # Load persisted data on first run
        await self._async_load_storage()

        quarter_idx_changed = False
        broadcast: BroadcastFrame | None = None

        try:
            # First, get broadcast to check if quarter_idx changed
            snapshot = await async_fetch_snapshot(self._address)
            broadcast = snapshot.get("broadcast")
            if not isinstance(broadcast, BroadcastFrame):
                raise UpdateFailed("No broadcast frame received")

            current_q_idx = broadcast.quarter_hours_idx
            regen = int(broadcast.regen_count)

            # Check if quarter index changed (new consumption data available)
            if self._last_quarter_idx is not None and current_q_idx != self._last_quarter_idx:
                quarter_idx_changed = True
                # Handle wrap-around (buffer is circular, max 2880 entries)
                if current_q_idx > self._last_quarter_idx:
                    entries_to_read = current_q_idx - self._last_quarter_idx
                else:
                    # Wrapped around
                    entries_to_read = current_q_idx + (2880 - self._last_quarter_idx)
                entries_to_read = min(entries_to_read, 96)  # Max 24 hours catch-up

                _LOGGER.debug(
                    "Quarter index changed: %d -> %d, reading %d recent entries",
                    self._last_quarter_idx, current_q_idx, entries_to_read
                )

                # Fetch recent quarter-hour entries to get actual consumption
                snapshot = await async_fetch_snapshot(
                    self._address,
                    recent_quarters=entries_to_read,
                )
                recent = snapshot.get("recent", [])

                # Sum up litres ONLY from entries in the range we need
                # (device_index from self._last_quarter_idx to current_q_idx - 1)
                if recent:
                    regen_changed = self._last_regen is not None and regen != self._last_regen
                    if regen_changed:
                        self._regen_total += 1
                        # Regeneration resets the capacity; trust the new broadcast value
                        self._broadcast_remaining_base = broadcast.remaining
                        self._consumption_since_base = 0.0
                        _LOGGER.debug("Regen detected: %d -> %d, total regens=%d", self._last_regen, regen, self._regen_total)
                    
                    if not regen_changed:
                        # Filter to only include entries in our target range
                        new_entries = [
                            e for e in recent
                            if self._last_quarter_idx <= e.get("device_index", -1) < current_q_idx
                        ]
                        new_litres = sum(entry.get("litres", 0) for entry in new_entries)
                        self._consumption_total += new_litres
                        self._consumption_since_base += new_litres
                        # Store last quarter consumption (most recent entry before current_q_idx)
                        # The entry at (current_q_idx - 1) represents the consumption of the quarter that just ended
                        last_entry = next((e for e in reversed(new_entries) if e.get("device_index") == current_q_idx - 1), None)
                        if last_entry:
                            self._last_quarter_consumption = last_entry.get("litres", 0)
                        elif new_entries:
                            # Fallback: use the most recent entry we have
                            self._last_quarter_consumption = new_entries[-1].get("litres", 0)
                        else:
                            # No new entries means no consumption in the last quarter
                            self._last_quarter_consumption = 0
                        _LOGGER.debug(
                            "Added %d litres from %d entries (indices %s), last_quarter=%d, total=%.0f",
                            new_litres,
                            len(new_entries),
                            [e.get("device_index") for e in new_entries],
                            self._last_quarter_consumption,
                            self._consumption_total
                        )
                        # Save to persistent storage after each update
                        await self._async_save_storage()
                    else:
                        _LOGGER.debug("Regen detected, skipping consumption update")
                else:
                    # No consumption data available for this period
                    self._last_quarter_consumption = 0

            self._last_quarter_idx = current_q_idx
            self._last_regen = regen

            # Update estimated remaining water.
            # The BLE broadcast remaining value updates infrequently (e.g. only
            # after regeneration).  We detect when it changes and reset our
            # running consumption adjustment so that between broadcast updates
            # the sensor decreases in step with observed quarter-hour usage.
            if self._broadcast_remaining_base is None:
                # First read ever
                self._broadcast_remaining_base = broadcast.remaining
                self._consumption_since_base = 0.0
            elif broadcast.remaining != self._broadcast_remaining_base:
                # Broadcast remaining changed (regeneration or device update)
                _LOGGER.debug(
                    "Broadcast remaining changed: %d -> %d, resetting adjustment (was %.0f)",
                    self._broadcast_remaining_base,
                    broadcast.remaining,
                    self._consumption_since_base,
                )
                self._broadcast_remaining_base = broadcast.remaining
                self._consumption_since_base = 0.0

            estimated_remaining = max(
                0, self._broadcast_remaining_base - self._consumption_since_base
            )

            # Save quarter index even if no consumption change (for restart recovery)
            if quarter_idx_changed or not self._storage_loaded:
                await self._async_save_storage()

        except Exception as err:
            raise UpdateFailed(str(err)) from err

        return {
            "broadcast": broadcast,
            "estimated_remaining": estimated_remaining,
            "water_total": self._consumption_total,
            "last_quarter_consumption": self._last_quarter_consumption,
            "regen_total": self._regen_total,
        }
