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
- Python dependency `bleak-retry-connector`, installed automatically by Home Assistant.

## Installation

### HACS

1. In HACS, add this repository as a custom repository with the **Integration** category, then download it.
2. Restart Home Assistant.
3. In **Settings → Devices & Services → Add Integration**, search for **BWT Water Softener BLE** and enter the Bluetooth MAC address.

[Add this repository to HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=SoaR245&repository=bwt_ble&category=integration)

### Manual installation

Copy `custom_components/bwt_ble` into the `custom_components` directory of your Home Assistant configuration, then restart Home Assistant and add the integration from **Settings → Devices & Services**.

## Notes

- The integration connects to the softener during each coordinator update (default every 5 minutes). Adjust `DEFAULT_SCAN_INTERVAL` in `custom_components/bwt_ble/const.py` if you need faster polling.
- Consumption counters are persisted across Home Assistant restarts. The monthly and yearly counters begin accumulating from the time this version is installed; earlier consumption in the current period is not backfilled.
- Enable Bluetooth access for Home Assistant (host mode or container) so Bleak can open the adapter (usually `/org/bluez/hci0`).

## Troubleshooting

- **Permission errors**: ensure the HA container has `--net=host` and access to DBus/BlueZ.
- **Timeouts**: the coordinator logs warnings when quarter-hour reads idle out; move the antenna closer to the softener or increase `overall_timeout`/`idle_timeout` in `ble.py` if necessary.
- **Duplicate devices**: the config flow prevents adding the same MAC twice. Remove the existing entry first.
