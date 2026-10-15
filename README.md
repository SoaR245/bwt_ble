# BWT Water Softener BLE

Home Assistant custom integration (HACS compatible) that connects to BWT water softeners over Bluetooth Low Energy using the same protocol as the official mobile app. The integration exposes sensors for:

1. **Remaining Capacity (%)** – percentage of the salt tank capacity.
2. **Remaining Water (L)** – absolute litres remaining before regeneration.
3. **Water Consumption (L)** – persisted, continuously increasing counter.
4. **Monthly Water Consumption (L)** – consumption for the current calendar month, reset at the start of each month.
5. **Yearly Water Consumption (L)** – consumption for the current calendar year, reset at the start of each year.
6. **Last Quarter Hour Consumption (L)** – consumption during the last completed 15-minute period.
7. **Regeneration Count** – total number of detected regeneration cycles.

## Requirements

- Home Assistant host with Bluetooth adapter supported by [Bleak](https://github.com/hbldh/bleak).
- Device MAC address (e.g. `03:12:00:1D:00:39`).
- Python dependency downloaded automatically (`bleak==2.0.0`).

## Installation

1. Copy the `bwt_ble` folder into `config/custom_components/` (or add this repository as a custom repository in HACS).
2. Restart Home Assistant to register the integration.
3. In the UI, navigate to **Settings → Devices & Services → Add Integration** and search for **BWT Water Softener BLE**.
4. Enter the Bluetooth MAC address when prompted.

## Notes

- The integration connects to the softener during each coordinator update (default every 5 minutes). Adjust `DEFAULT_SCAN_INTERVAL` in `const.py` if you need faster polling.
- Consumption counters are persisted across Home Assistant restarts. New monthly and yearly counters are initialized from the main Water Consumption sensor's long-term statistics for the current calendar month and year, using the Home Assistant time zone. Existing counters from an earlier version are also initialized once after upgrading.
- Enable Bluetooth access for Home Assistant (host mode or container) so Bleak can open the adapter (usually `/org/bluez/hci0`).

## Historical initialization

Keep the main Water Consumption entity, its recorded statistics, and the integration's stored consumption data. Recorder must be enabled for historical initialization. The source entity is located by its unique ID, so renaming it is supported.

To test reinitialization, delete only the monthly and yearly entities from the entity registry, install the updated integration, and restart Home Assistant. The integration recreates the missing entities and replaces their current values with the reconstructed consumption. Deleting just one period entity only reinitializes that period. Deletion is not required for the first upgrade.

Initialization uses hourly statistics and includes consumption since the latest recorded statistic. Boundary precision is limited by the available readings. If history does not cover the period boundary, the nearest available reference is used and a warning is logged; the result may omit or include consumption around that gap. Missing statistics or database failures are retried on subsequent coordinator updates without interrupting BLE polling. Without Recorder, counters continue accumulating normally.

Successful initialization is saved and is not repeated on normal restarts or period resets. Look for `Initialized monthly consumption` and `Initialized yearly consumption` in the `custom_components.bwt_ble.coordinator` logs. This initializes current values only; it does not insert past records into the new entities' history.

## Development

Run the focused historical initialization tests with Python 3.13 or later:

```sh
python -m unittest discover -s tests -v
```

These tests use Home Assistant interface doubles and do not require a running Home Assistant instance or Bluetooth device.

## Troubleshooting

- **Permission errors**: ensure the HA container has `--net=host` and access to DBus/BlueZ.
- **Timeouts**: the coordinator logs warnings when quarter-hour reads idle out; move the antenna closer to the softener or increase `overall_timeout`/`idle_timeout` in `ble.py` if necessary.
- **Duplicate devices**: the config flow prevents adding the same MAC twice. Remove the existing entry first.
