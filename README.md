# BWT Water Softener BLE

Home Assistant custom integration (HACS compatible) that connects to BWT water softeners over Bluetooth Low Energy using the same protocol as the official mobile app. The integration exposes three sensors:

1. **Remaining Capacity (%)** – percentage of the salt tank capacity.
2. **Remaining Water (L)** – absolute litres remaining before regeneration.
3. **Water Consumption (L)** – continuously increasing counter that rolls over only when Home Assistant restarts.

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
- The water consumption sensor accumulates usage across regeneration cycles by tracking the drop in remaining capacity; it resets only when Home Assistant restarts or the integration is removed.
- Enable Bluetooth access for Home Assistant (host mode or container) so Bleak can open the adapter (usually `/org/bluez/hci0`).

## Troubleshooting

- **Permission errors**: ensure the HA container has `--net=host` and access to DBus/BlueZ.
- **Timeouts**: the coordinator logs warnings when quarter-hour reads idle out; move the antenna closer to the softener or increase `overall_timeout`/`idle_timeout` in `ble.py` if necessary.
- **Duplicate devices**: the config flow prevents adding the same MAC twice. Remove the existing entry first.
