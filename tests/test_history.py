"""Focused tests using doubles for the Home Assistant runtime interfaces."""

import ast
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone(timedelta(hours=2)))
SOURCE = "sensor.renamed_water_consumption"


class CoordinatorDouble:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, *args, **kwargs):
        self.hass = hass
        self.data = {}


def load_coordinator():
    modules = {}
    for name in (
        "homeassistant", "homeassistant.core", "homeassistant.helpers",
        "homeassistant.helpers.storage", "homeassistant.helpers.update_coordinator",
        "homeassistant.util", "homeassistant.util.dt", "history_test_package",
        "history_test_package.ble", "history_test_package.const",
    ):
        modules[name] = ModuleType(name)
    modules["homeassistant.core"].HomeAssistant = object
    modules["homeassistant.helpers.storage"].Store = Mock
    update = modules["homeassistant.helpers.update_coordinator"]
    update.DataUpdateCoordinator = CoordinatorDouble
    update.UpdateFailed = RuntimeError
    dates = modules["homeassistant.util.dt"]
    dates.now = lambda: NOW
    dates.utcnow = lambda: NOW.astimezone(timezone.utc)
    dates.as_utc = lambda value: value.astimezone(timezone.utc)
    dates.as_local = lambda value: value.astimezone(NOW.tzinfo)
    modules["history_test_package.ble"].BroadcastFrame = SimpleNamespace
    modules["history_test_package.ble"].async_fetch_snapshot = AsyncMock()
    constants = modules["history_test_package.const"]
    constants.DEFAULT_SCAN_INTERVAL = 300
    constants.DOMAIN = "bwt_ble"
    path = Path(__file__).parents[1] / "coordinator.py"
    spec = importlib.util.spec_from_file_location("history_test_package.coordinator", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


coordinator_module = load_coordinator()


def row(end, state, total_sum):
    return {"start": (end - timedelta(hours=1)).timestamp(), "state": state, "sum": total_sum}


class ConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.start = NOW.replace(day=1, hour=0, minute=0)

    def test_boundary_state_and_uncompiled_live_delta(self):
        rows = [row(self.start, 90000, 80000), row(NOW, 96000, 86000)]
        self.assertEqual(coordinator_module._historical_consumption(rows, self.start, 96323), (6323, False))

    def test_reset_uses_statistical_sum_not_state_difference(self):
        rows = [row(self.start, 90000, 80000), row(NOW, 1000, 85000)]
        self.assertEqual(coordinator_module._historical_consumption(rows, self.start, 1200), (5200, False))

    def test_partial_history_is_reported(self):
        rows = [row(self.start + timedelta(days=2), 100, 0), row(NOW, 500, 400)]
        self.assertEqual(coordinator_module._historical_consumption(rows, self.start, 520), (420, True))

    def test_gap_before_boundary_is_reported(self):
        rows = [row(self.start - timedelta(hours=2), 100, 0), row(NOW, 500, 400)]
        self.assertEqual(coordinator_module._historical_consumption(rows, self.start, 520), (420, True))

    def test_empty_or_invalid_statistics_are_not_used(self):
        for rows in ([], [{"start": 0, "state": None, "sum": 0}], [{"start": 0, "state": float("nan"), "sum": 0}]):
            with self.subTest(rows=rows):
                self.assertIsNone(coordinator_module._historical_consumption(rows, self.start, 100))

    def test_recorded_state_ahead_of_storage_is_not_used(self):
        self.assertIsNone(coordinator_module._historical_consumption([row(NOW, 200, 100)], self.start, 150))


class InitializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
        self.coordinator = coordinator_module.BwtBleCoordinator(self.hass, address="AA:BB")
        self.coordinator._store = SimpleNamespace(async_load=AsyncMock(), async_save=AsyncMock())
        self.coordinator._storage_loaded = True
        self.coordinator._consumption_total = 96323
        self.coordinator.data = {"water_total": 96323}
        self.recorder = SimpleNamespace(async_add_executor_job=AsyncMock(side_effect=self.query))
        recorder_module = ModuleType("homeassistant.components.recorder")
        recorder_module.get_instance = lambda hass: self.recorder
        statistics_module = ModuleType("homeassistant.components.recorder.statistics")
        statistics_module.statistics_during_period = Mock()
        self.modules = patch.dict(sys.modules, {
            "homeassistant.components.recorder": recorder_module,
            "homeassistant.components.recorder.statistics": statistics_module,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def query(self, function, hass, start, end, ids, period, units, types):
        boundary = start + timedelta(days=1)
        baseline = 90000 if boundary.astimezone(NOW.tzinfo).month == 10 else 10000
        return {SOURCE: [row(boundary, baseline, baseline - 1000), row(NOW, 96000, 95000)]}

    async def test_new_counters_are_replaced_saved_and_published(self):
        self.coordinator._consumption_monthly = 50
        await self.coordinator.async_initialize_history(SOURCE, {"monthly", "yearly"})
        self.assertEqual(self.coordinator.data["water_monthly"], 6323)
        self.assertEqual(self.coordinator.data["water_yearly"], 86323)
        saved = self.coordinator._store.async_save.call_args.args[0]
        self.assertEqual(saved["history_initialized"], ["monthly", "yearly"])
        for call in self.recorder.async_add_executor_job.call_args_list:
            self.assertEqual(call.args[4:], ({SOURCE}, "hour", {"volume": "L"}, {"state", "sum"}))
            self.assertEqual(call.args[2].tzinfo, timezone.utc)
        calls = self.recorder.async_add_executor_job.call_count
        await self.coordinator.async_initialize_history(SOURCE, set())
        self.assertEqual(self.recorder.async_add_executor_job.call_count, calls)

    async def test_deleting_only_monthly_reinitializes_only_monthly(self):
        self.coordinator._history_initialized = {"monthly", "yearly"}
        self.coordinator._consumption_yearly = 123
        await self.coordinator.async_initialize_history(SOURCE, {"monthly"})
        self.assertEqual(self.coordinator._consumption_monthly, 6323)
        self.assertEqual(self.coordinator._consumption_yearly, 123)
        self.recorder.async_add_executor_job.assert_awaited_once()

    async def test_sensor_setup_detects_deleted_entity_and_renamed_source(self):
        path = Path(__file__).parents[1] / "sensor.py"
        setup = next(
            node for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
        )
        registry = Mock()
        registry.async_get_entity_id.side_effect = lambda domain, platform, unique_id: {
            "entry_water_consumption": SOURCE,
            "entry_water_consumption_yearly": "sensor.yearly",
        }.get(unique_id)
        self.coordinator.async_initialize_history = AsyncMock()
        self.hass.data = {"bwt_ble": {"entry": {"coordinator": self.coordinator}}}
        namespace = {
            "er": SimpleNamespace(async_get=lambda hass: registry),
            "DOMAIN": "bwt_ble", "DATA_COORDINATOR": "coordinator",
            "SENSORS": (), "BINARY_SENSORS": (),
        }
        code = "from __future__ import annotations\n" + ast.unparse(setup)
        exec(compile(code, str(path), "exec"), namespace)
        add_entities = Mock()
        await namespace["async_setup_entry"](self.hass, SimpleNamespace(entry_id="entry"), add_entities)
        self.coordinator.async_initialize_history.assert_awaited_once_with(SOURCE, {"monthly"})
        add_entities.assert_called_once_with([])

    async def test_existing_unmigrated_counters_are_initialized(self):
        await self.coordinator.async_initialize_history(SOURCE, set())
        self.assertEqual(self.coordinator._history_initialized, {"monthly", "yearly"})

    async def test_missing_statistics_are_retried(self):
        self.recorder.async_add_executor_job.side_effect = None
        self.recorder.async_add_executor_job.return_value = {}
        await self.coordinator.async_initialize_history(SOURCE, {"monthly"})
        self.assertEqual(self.coordinator._history_initialized, set())
        self.coordinator._store.async_save.assert_not_awaited()
        self.recorder.async_add_executor_job.side_effect = self.query
        await self.coordinator._async_backfill_history()
        self.assertEqual(self.coordinator._consumption_monthly, 6323)

    async def test_recorder_failure_does_not_break_setup(self):
        self.recorder.async_add_executor_job.side_effect = RuntimeError("Database unavailable")
        await self.coordinator.async_initialize_history(SOURCE, set())
        self.assertEqual(self.coordinator._history_pending, {"monthly", "yearly"})

    async def test_recorder_disabled_or_missing_source_does_not_query(self):
        await self.coordinator.async_initialize_history(None, {"monthly"})
        self.hass.config.components.clear()
        await self.coordinator.async_initialize_history(SOURCE, {"monthly"})
        self.recorder.async_add_executor_job.assert_not_awaited()

    async def test_markers_are_restored_from_storage(self):
        self.coordinator._storage_loaded = False
        self.coordinator._store.async_load.return_value = {
            "water_total": 96323, "water_monthly": 6323, "water_yearly": 86323,
            "history_initialized": ["monthly", "yearly"],
        }
        await self.coordinator._async_load_storage()
        await self.coordinator.async_initialize_history(SOURCE, set())
        self.recorder.async_add_executor_job.assert_not_awaited()
        self.assertEqual(self.coordinator._consumption_monthly, 6323)

    async def test_new_period_resets_without_repeating_backfill(self):
        self.coordinator._history_initialized = {"monthly", "yearly"}
        self.coordinator._history_source = SOURCE
        self.coordinator._month_key = "2025-12"
        self.coordinator._year_key = "2025"
        self.coordinator._consumption_monthly = 500
        self.coordinator._consumption_yearly = 5000
        broadcast = SimpleNamespace(quarter_hours_idx=1, regen_count=0, remaining=1000)
        snapshot = AsyncMock(return_value={"broadcast": broadcast})
        with patch.object(coordinator_module, "async_fetch_snapshot", snapshot):
            data = await self.coordinator._async_update_data()
        self.assertEqual(data["water_monthly"], 0)
        self.assertEqual(data["water_yearly"], 0)
        self.assertEqual(data["water_total"], 96323)
        self.recorder.async_add_executor_job.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()