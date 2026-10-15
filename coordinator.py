from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from math import isfinite
from typing import Any, Dict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .ble import BroadcastFrame, async_fetch_snapshot
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}.consumption"
HISTORY_STATISTICS_VERSION = 1
STATISTICS_IMPORT_BATCH_SIZE = 5000


def _historical_consumption(
    rows: list[dict[str, Any]], period_start: datetime, current_total: float
) -> tuple[float, bool] | None:
    valid_rows = sorted(
        (
            row for row in rows
            if all(
                isinstance(row.get(key), (int, float)) and isfinite(row[key])
                for key in ("start", "state", "sum")
            )
        ),
        key=lambda row: row["start"],
    )
    if not valid_rows:
        return None
    boundary = period_start.timestamp()
    preceding = [row for row in valid_rows if row["start"] + 3600 <= boundary]
    baseline = preceding[-1] if preceding else valid_rows[0]
    latest = valid_rows[-1]
    live_delta = current_total - latest["state"]
    if live_delta < 0:
        _LOGGER.warning("Cannot backfill consumption: stored total is behind recorded statistics")
        return None
    consumption = latest["sum"] - baseline["sum"] + live_delta
    if consumption < 0:
        return None
    return consumption, baseline["start"] + 3600 != boundary


def _build_period_statistics(
    rows: list[dict[str, Any]], period: str
) -> list[dict[str, Any]]:
    valid_rows = sorted(
        (
            row for row in rows
            if all(
                isinstance(row.get(key), (int, float)) and isfinite(row[key])
                for key in ("start", "sum")
            )
        ),
        key=lambda row: row["start"],
    )
    if not valid_rows:
        return []

    result = []
    first_sum = valid_rows[0]["sum"]
    previous_sum = first_sum
    period_key = None
    period_start_sum = first_sum
    for row in valid_rows:
        period_end = dt_util.as_local(
            datetime.fromtimestamp(row["start"] + 3600, tz=timezone.utc)
        )
        row_period_key = period_end.strftime("%Y-%m" if period == "monthly" else "%Y")
        if row_period_key != period_key:
            period_key = row_period_key
            period_start_sum = previous_sum
        period_state = row["sum"] - period_start_sum
        total_sum = row["sum"] - first_sum
        if period_state < 0 or total_sum < 0:
            return []
        result.append({
            "start": datetime.fromtimestamp(row["start"], tz=timezone.utc),
            "state": period_state,
            "sum": total_sum,
        })
        previous_sum = row["sum"]
    return result


class BwtBleCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, *, address: str, scan_interval: int | None = None) -> None:
        update_interval = timedelta(seconds=scan_interval or DEFAULT_SCAN_INTERVAL)
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}-{address}", update_interval=update_interval)
        self._address = address
        self._consumption_total = 0.0
        self._last_quarter_idx: int | None = None
        self._last_regen: int | None = None
        self._last_quarter_consumption = 0.0
        self._consumption_monthly = 0.0
        self._consumption_yearly = 0.0
        self._month_key: str | None = None
        self._year_key: str | None = None
        self._history_initialized: set[str] = set()
        self._history_statistics_versions: dict[str, int] = {}
        self._history_pending: set[str] = set()
        self._history_source: str | None = None
        self._history_targets: dict[str, str] = {}
        self._history_backfill_lock = asyncio.Lock()
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
                self._consumption_monthly = float(data.get("water_monthly", 0.0))
                self._consumption_yearly = float(data.get("water_yearly", 0.0))
                self._month_key = data.get("month_key")
                self._year_key = data.get("year_key")
                self._history_initialized = set(data.get("history_initialized", []))
                self._history_statistics_versions = data.get("history_statistics_versions", {})
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
                "water_monthly": self._consumption_monthly,
                "water_yearly": self._consumption_yearly,
                "month_key": self._month_key,
                "year_key": self._year_key,
                "history_initialized": sorted(self._history_initialized),
                "history_statistics_versions": self._history_statistics_versions,
                "broadcast_remaining_base": self._broadcast_remaining_base,
                "consumption_since_base": self._consumption_since_base,
            })
        except Exception as err:
            _LOGGER.warning("Failed to save consumption data: %s", err)

    @property
    def address(self) -> str:
        return self._address

    async def async_initialize_history(
        self,
        source: str | None,
        missing: set[str],
        targets: dict[str, str] | None = None,
    ) -> None:
        """Initialize period counters and their long-term statistics."""
        self._history_source = source
        self._history_targets = targets or {}
        self._history_pending = missing | ({"monthly", "yearly"} - self._history_initialized)
        await self._async_backfill_history()
        if self.data is not None:
            self.data.update(
                water_monthly=self._consumption_monthly,
                water_yearly=self._consumption_yearly,
            )

    async def async_register_history_target(self, period: str, statistic_id: str) -> None:
        self._history_targets[period] = statistic_id
        await self._async_backfill_history()

    async def _async_backfill_history(self) -> None:
        async with self._history_backfill_lock:
            await self._async_run_history_backfill()

    async def _async_run_history_backfill(self) -> None:
        pending_statistics = {
            period for period in ("monthly", "yearly")
            if self._history_statistics_versions.get(period, 0) < HISTORY_STATISTICS_VERSION
        }
        if (
            self._history_source is None
            or (not self._history_pending and not pending_statistics)
        ):
            return
        if "recorder" not in self.hass.config.components:
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            async_import_statistics,
            statistics_during_period,
        )

        now = dt_util.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        starts = {"monthly": month_start, "yearly": month_start.replace(month=1)}
        changed = False
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
        try:
            statistics = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                dt_util.as_utc(now),
                {self._history_source},
                "hour",
                {"volume": "L"},
                {"state", "sum"},
            )
        except Exception as err:
            _LOGGER.warning("Failed to read historical consumption statistics: %s", err)
            return

        rows = statistics.get(self._history_source, [])
        for period in tuple(self._history_pending):
            result = _historical_consumption(
                rows, dt_util.as_utc(starts[period]), self._consumption_total
            )
            if result is None:
                continue
            consumption, incomplete = result
            if period == "monthly":
                self._consumption_monthly = consumption
                self._month_key = now.strftime("%Y-%m")
            else:
                self._consumption_yearly = consumption
                self._year_key = now.strftime("%Y")
            self._history_initialized.add(period)
            self._history_pending.remove(period)
            changed = True
            if incomplete:
                _LOGGER.warning(
                    "%s consumption backfilled from the first available statistic; "
                    "history does not cover the start of the period", period,
                )
            _LOGGER.info("Initialized %s consumption from %s: %.0f L", period, self._history_source, consumption)

        for period in tuple(pending_statistics):
            statistic_id = self._history_targets.get(period)
            if not statistic_id or not rows:
                continue
            period_statistics = _build_period_statistics(rows, period)
            if not period_statistics:
                continue
            metadata = {
                "has_mean": False,
                "has_sum": True,
                "name": f"{period.title()} Water Consumption",
                "source": "recorder",
                "statistic_id": statistic_id,
                "unit_of_measurement": "L",
            }
            try:
                from homeassistant.components.recorder.models import StatisticMeanType
            except ImportError:
                pass
            else:
                metadata["mean_type"] = StatisticMeanType.NONE
                metadata["unit_class"] = "volume"
            try:
                for offset in range(0, len(period_statistics), STATISTICS_IMPORT_BATCH_SIZE):
                    async_import_statistics(
                        self.hass,
                        metadata,
                        period_statistics[offset:offset + STATISTICS_IMPORT_BATCH_SIZE],
                    )
                    await asyncio.sleep(0)
            except Exception as err:
                _LOGGER.warning("Failed to import %s historical statistics: %s", period, err)
                continue
            self._history_statistics_versions[period] = HISTORY_STATISTICS_VERSION
            changed = True
            _LOGGER.info(
                "Queued %d historical statistics for %s (%s)",
                len(period_statistics), period, statistic_id,
            )

        if changed:
            await self._async_save_storage()

    async def _async_update_data(self) -> Dict[str, Any]:
        # Load persisted data on first run
        await self._async_load_storage()

        quarter_idx_changed = False
        now = dt_util.now()
        month_key = now.strftime("%Y-%m")
        year_key = now.strftime("%Y")
        period_changed = month_key != self._month_key or year_key != self._year_key
        if month_key != self._month_key:
            self._consumption_monthly = 0.0
            self._month_key = month_key
        if year_key != self._year_key:
            self._consumption_yearly = 0.0
            self._year_key = year_key
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
                        current_utc = dt_util.utcnow()
                        quarter_end_utc = current_utc.replace(
                            minute=(current_utc.minute // 15) * 15,
                            second=0,
                            microsecond=0,
                        )
                        for entry in new_entries:
                            quarter_offset = (current_q_idx - 1 - entry["device_index"]) % 2880
                            entry_time = dt_util.as_local(
                                quarter_end_utc - timedelta(minutes=15 * quarter_offset)
                            )
                            if entry_time.strftime("%Y-%m") == month_key:
                                self._consumption_monthly += entry.get("litres", 0)
                            if entry_time.strftime("%Y") == year_key:
                                self._consumption_yearly += entry.get("litres", 0)
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
            if quarter_idx_changed or period_changed or not self._storage_loaded:
                await self._async_save_storage()

        except Exception as err:
            raise UpdateFailed(str(err)) from err

        await self._async_backfill_history()
        return {
            "broadcast": broadcast,
            "estimated_remaining": estimated_remaining,
            "water_total": self._consumption_total,
            "water_monthly": self._consumption_monthly,
            "water_yearly": self._consumption_yearly,
            "last_quarter_consumption": self._last_quarter_consumption,
            "regen_total": self._regen_total,
        }
