# BWT BLE ↔ @Home App Analysis

## Architecture
- **bwt_ble**: Home Assistant custom integration (Python) reading BWT water softener via BLE
- **athomeapp**: Decompiled BWT @Home mobile app (Cordova/React hybrid, JS in `assets/www/static/js/`)

## BLE Protocol — Broadcast Characteristic
UUID: `D973F2E3-B19E-11E2-9E96-0800200C9A66`

| Bytes | Field | Encoding |
|-------|-------|----------|
| 0–3 | remaining | uint32 LE (liters) |
| 4–5 | quarter_hours_idx | uint16 LE |
| 6–7 | days_idx | uint16 LE |
| 8–9 | regen_count | uint16 LE |
| 10–11 | total_capacity_raw | uint16 LE (× 1000 = liters) |
| 12 | flags | bit0=alarm, bit1=quarter_hours_looped, bit2=days_looped |
| 13 | version_major | |
| 14 | version_minor | |

**JS parsing** (ble.js ~L764): `remaining = GetUInt16LE(o,0,true) + GetUInt16LE(o,2,true) * 65536`
**Python parsing** (ble.py): `remaining = int.from_bytes(payload[0:4], "little")` → identical result

## BLE Protocol — Quarter-Hour History (Hist A)
- Buffer char: `D973F2E1-B19E-11E2-9E96-0800200C9A66`
- Trigger char: `D973F2E2-B19E-11E2-9E96-0800200C9A66`
- Each word = 16 bits: bits[0:9] = liters (0–1023), bit10 = power_cut, bit11 = regen
- Notification: header (2B LE) + payload (big-endian 16-bit words); header × 9 = base word index
- Circular buffer of 2880 entries (30 days × 96 quarter-hours/day)

## Key Insight: Remaining Water Discrepancy
The mobile app **does NOT** use `broadcast.remaining` for the main display. Instead:
- `percentageRemaining` comes from cloud API field `RemainingAmmountOfResource`
- `stockRemaining` comes from cloud API field `RefillResourceInDays`
- The cloud API presumably computes a real-time remaining by integrating flow sensor data

Meanwhile, the BLE `broadcast.remaining` value updates infrequently — likely only after:
- Regeneration events (reset to total_capacity)
- Possibly periodic sync intervals on the device
This explains why bwt_ble showed a **fixed** `remaining_volume`: the broadcast byte value was stale.

## Fix Applied (April 2026)
**Estimated remaining** = `broadcast.remaining - Σ(quarter-hour consumption since broadcast remaining last changed)`

Logic in `coordinator.py`:
1. Track `_broadcast_remaining_base` and `_consumption_since_base`
2. When `broadcast.remaining` changes (device update or regeneration) → reset base & adjustment
3. When new quarter-hour consumption entries arrive → add litres to `_consumption_since_base`
4. `estimated_remaining = max(0, base - consumption_since_base)`
5. Both values persisted to storage for restart recovery

Sensor `remaining_volume` and `remaining_percentage` now use `estimated_remaining`.

## Other App Characteristics Noted
- `remainingConsumableCapacity` (char code 0402) — registered in app but no decode logic found; likely for cartridge-based devices
- `configureFlowSensors` (0501), `sensorValues` (0503) — flow sensor characteristics exist but not used in display
- Daily history (Hist B): 11-bit liters × 10, regen count in bits[12:13]
- App has REST APIs for consumption stats (daily, monthly) — chart display only

## File Locations
- App broadcast decode: `athomeapp/assets/www/static/js/ble.js` ~L760–L820
- App helper functions: `athomeapp/assets/www/static/js/main-deob1.js` ~L17583–L17609
- App state store (remaining display): `main-deob1.js` ~L1406–L1551
- Integration BLE: `bwt_ble/ble.py`
- Integration coordinator: `bwt_ble/coordinator.py`
- Integration sensors: `bwt_ble/sensor.py`
