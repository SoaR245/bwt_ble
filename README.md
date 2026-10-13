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
- Consumption counters are persisted across Home Assistant restarts. The monthly and yearly counters begin accumulating from the time this version is installed; earlier consumption in the current period is not backfilled.
- Enable Bluetooth access for Home Assistant (host mode or container) so Bleak can open the adapter (usually `/org/bluez/hci0`).

## Troubleshooting

- **Permission errors**: ensure the HA container has `--net=host` and access to DBus/BlueZ.
- **Timeouts**: the coordinator logs warnings when quarter-hour reads idle out; move the antenna closer to the softener or increase `overall_timeout`/`idle_timeout` in `ble.py` if necessary.
- **Duplicate devices**: the config flow prevents adding the same MAC twice. Remove the existing entry first.
