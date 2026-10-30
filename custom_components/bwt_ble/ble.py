from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from bleak import BleakClient
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache

_LOGGER = logging.getLogger(__name__)

CHAR_BROADCAST = "D973F2E3-B19E-11E2-9E96-0800200C9A66"
CHAR_BUFFER = "D973F2E1-B19E-11E2-9E96-0800200C9A66"
CHAR_BUFFER_TRIGGER = "D973F2E2-B19E-11E2-9E96-0800200C9A66"

HIST_A_START = 0
HIST_A_END = HIST_A_START + 5760
TRIGGER_DELAY_MS = 20


def _clamp_ratio(total_capacity: int, remaining: int) -> float:
    if total_capacity <= 0:
        return 0.0
    ratio = remaining / total_capacity
    if ratio > 500:
        return 0.0
    return max(0.0, min(1.0, ratio))


@dataclass(slots=True)
class BroadcastFrame:
    remaining: int
    quarter_hours_idx: int
    days_idx: int
    regen_count: int
    total_capacity: int
    alarm: bool
    quarter_hours_looped: bool
    days_looped: bool
    percentage: float
    version: str


def decode_broadcast(payload: bytes) -> BroadcastFrame:
    if len(payload) < 15:
        raise ValueError("Broadcast characteristic returned too few bytes")
    remaining = int.from_bytes(payload[0:4], "little")
    q_idx = int.from_bytes(payload[4:6], "little")
    d_idx = int.from_bytes(payload[6:8], "little")
    regen = int.from_bytes(payload[8:10], "little")
    total_capacity = int.from_bytes(payload[10:12], "little") * 1000
    flags = payload[12]
    version = f"{payload[13]}, {payload[14]}"
    return BroadcastFrame(
        remaining=remaining,
        quarter_hours_idx=q_idx,
        days_idx=d_idx,
        regen_count=regen,
        total_capacity=total_capacity,
        alarm=bool(flags & 0x01),
        quarter_hours_looped=bool(flags & 0x02),
        days_looped=bool(flags & 0x04),
        percentage=_clamp_ratio(total_capacity, remaining),
        version=version,
    )


def parse_quarter_word(word: int) -> Dict[str, float]:
    # JS parseHistA: litres = 1023 & e (no scaling)
    # Bit 10 = power_cut, Bit 11 = regen
    return {
        "power_cut": bool(word & (1 << 10)),
        "regen": 1 if (word & (1 << 11)) else 0,
        "litres": word & 0x03FF,  # Direct value, no scaling
    }


async def async_read_broadcast(client: BleakClient) -> BroadcastFrame:
    payload = await client.read_gatt_char(CHAR_BROADCAST)
    frame = decode_broadcast(bytearray(payload))
    _LOGGER.debug(
        "Broadcast: remaining=%d, total=%d, percentage=%.1f%%, regen_count=%d, q_idx=%d",
        frame.remaining,
        frame.total_capacity,
        frame.percentage * 100,
        frame.regen_count,
        frame.quarter_hours_idx,
    )
    return frame


async def async_read_quarter_history(
    client: BleakClient,
    broadcast: BroadcastFrame,
    overall_timeout: float = 180.0,
    idle_timeout: float = 6.0,
) -> List[Dict[str, float]]:
    looped = broadcast.quarter_hours_looped
    last_idx = broadcast.quarter_hours_idx
    expected_bytes = (HIST_A_END - HIST_A_START) if looped else max(0, 2 * last_idx)
    if expected_bytes <= 0:
        return []

    expected_words = expected_bytes // 2
    words_map: Dict[int, int] = {}
    event = asyncio.Event()
    last_received = time.time()

    def notification_handler(_: int, data: bytearray) -> None:
        nonlocal last_received
        last_received = time.time()
        if len(data) < 4:
            return
        header = data[0] | (data[1] << 8)  # header is little-endian
        base_word = header * 9  # each notification covers 9 words (18 bytes)
        payload = data[2:]
        num_words = len(payload) // 2
        for i in range(num_words):
            # Data words are BIG-ENDIAN (high byte first)
            word = (payload[2 * i] << 8) | payload[2 * i + 1]
            word_idx = base_word + i
            if word_idx < expected_words:
                words_map[word_idx] = word
        event.set()

    await client.start_notify(CHAR_BUFFER, notification_handler)
    try:
        trigger = bytearray(
            [
                2,
                HIST_A_START & 0xFF,
                (HIST_A_START >> 8) & 0xFF,
                expected_bytes & 0xFF,
                (expected_bytes >> 8) & 0xFF,
                TRIGGER_DELAY_MS & 0xFF,
                (TRIGGER_DELAY_MS >> 8) & 0xFF,
            ]
        )
        await client.write_gatt_char(CHAR_BUFFER_TRIGGER, trigger)

        start_time = time.time()
        while True:
            if expected_words and len(words_map) >= expected_words:
                break
            remaining = overall_timeout - (time.time() - start_time)
            if remaining <= 0:
                _LOGGER.warning("Quarter-hour read overall timeout reached")
                break
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, idle_timeout))
            except asyncio.TimeoutError:
                if time.time() - last_received >= idle_timeout:
                    _LOGGER.info("Quarter-hour read idle timeout reached")
                    break

        if not words_map:
            return []

        # Build ordered list, using 0 for any missing indices (packet loss)
        all_words: List[int] = [words_map.get(i, 0) for i in range(expected_words)]

        # Rotation logic matching JS: parsed.slice(lastIdx).concat(parsed.slice(0, lastIdx))
        if looped and last_idx > 0 and len(all_words) > 0:
            all_words = all_words[last_idx:] + all_words[:last_idx]
        elif not looped:
            all_words = all_words[:last_idx]

        entries: List[Dict[str, float]] = []
        for seq, word in enumerate(all_words):
            entry = parse_quarter_word(word)
            entry["sequence"] = seq
            entry["device_index"] = seq
            entries.append(entry)

        # Log summary and last few entries
        missing = expected_words - len(words_map)
        if missing > 0:
            _LOGGER.debug(
                "History: %d words received, %d missing (%.1f%%)",
                len(words_map), missing, 100 * missing / expected_words
            )
        if entries:
            last_entries = entries[-min(5, len(entries)):]
            _LOGGER.debug(
                "Last %d quarter-hour entries: %s",
                len(last_entries),
                [{"litres": e["litres"], "regen": e["regen"]} for e in reversed(last_entries)]
            )
        return entries
    finally:
        try:
            await client.stop_notify(CHAR_BUFFER)
        except Exception:
            _LOGGER.debug("Stopping notify failed", exc_info=True)


async def async_read_recent_quarters(
    client: BleakClient,
    broadcast: BroadcastFrame,
    count: int = 8,
    idle_timeout: float = 3.0,
) -> List[Dict[str, float]]:
    """Read only the most recent quarter-hour entries (lightweight read).
    
    The entries returned are the N entries BEFORE the current quarter_hours_idx,
    which represent the consumption data for those time periods.
    """
    last_idx = broadcast.quarter_hours_idx
    if last_idx <= 0:
        return []

    # We want entries from (last_idx - count) to (last_idx - 1)
    # last_idx points to the NEXT slot to be written, so current data is at last_idx-1
    num_entries = min(count, last_idx if not broadcast.quarter_hours_looped else count)
    start_word = max(0, last_idx - num_entries)
    actual_entries = last_idx - start_word

    if actual_entries <= 0:
        return []

    start_byte = start_word * 2
    num_bytes = actual_entries * 2

    _LOGGER.debug(
        "Reading recent quarters: start_word=%d, count=%d, start_byte=%d, num_bytes=%d",
        start_word, actual_entries, start_byte, num_bytes
    )

    words_map: Dict[int, int] = {}
    event = asyncio.Event()
    last_received = time.time()

    def notification_handler(_: int, data: bytearray) -> None:
        nonlocal last_received
        last_received = time.time()
        if len(data) < 4:
            return
        # Header is relative to the start of our requested range
        header = data[0] | (data[1] << 8)
        # Each notification covers up to 9 words (18 bytes of payload)
        relative_word = header * 9
        payload = data[2:]
        num_words = len(payload) // 2
        for i in range(num_words):
            word = (payload[2 * i] << 8) | payload[2 * i + 1]
            # Store with absolute index
            abs_word_idx = start_word + relative_word + i
            if abs_word_idx < last_idx:
                words_map[abs_word_idx] = word
        event.set()

    await client.start_notify(CHAR_BUFFER, notification_handler)
    try:
        trigger = bytearray([
            2,
            start_byte & 0xFF,
            (start_byte >> 8) & 0xFF,
            num_bytes & 0xFF,
            (num_bytes >> 8) & 0xFF,
            TRIGGER_DELAY_MS & 0xFF,
            (TRIGGER_DELAY_MS >> 8) & 0xFF,
        ])
        await client.write_gatt_char(CHAR_BUFFER_TRIGGER, trigger)

        start_time = time.time()
        expected_words = actual_entries
        while True:
            if len(words_map) >= expected_words:
                break
            remaining = 10.0 - (time.time() - start_time)
            if remaining <= 0:
                break
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, idle_timeout))
            except asyncio.TimeoutError:
                if time.time() - last_received >= idle_timeout:
                    break

        if not words_map:
            return []

        # Build result list from received words (sorted by absolute index)
        entries: List[Dict[str, float]] = []
        for word_idx in sorted(words_map.keys()):
            entry = parse_quarter_word(words_map[word_idx])
            entry["device_index"] = word_idx
            entries.append(entry)

        _LOGGER.debug(
            "Recent quarters read: %d entries at indices %s, litres=%s",
            len(entries),
            [e["device_index"] for e in entries],
            [e["litres"] for e in entries]
        )
        return entries
    finally:
        try:
            await client.stop_notify(CHAR_BUFFER)
        except Exception:
            _LOGGER.debug("Stopping notify failed", exc_info=True)


async def async_fetch_snapshot(
    address: str,
    *,
    overall_timeout: float = 180.0,
    idle_timeout: float = 6.0,
    include_history: bool = False,
    recent_quarters: int = 0,
) -> Dict[str, object]:
    broadcast: Optional[BroadcastFrame] = None
    history: List[Dict[str, float]] = []
    recent: List[Dict[str, float]] = []

    # Use bleak-retry-connector for more reliable connections
    from bleak.backends.device import BLEDevice
    from bleak import BleakScanner

    device = await BleakScanner.find_device_by_address(address, timeout=20.0)
    if device is None:
        raise ValueError(f"Device {address} not found")

    client = await establish_connection(
        BleakClientWithServiceCache,
        device,
        address,
        max_attempts=3,
    )
    try:
        _LOGGER.debug("Connected to %s", address)
        broadcast = await async_read_broadcast(client)
        if include_history:
            history = await async_read_quarter_history(
                client,
                broadcast,
                overall_timeout=overall_timeout,
                idle_timeout=idle_timeout,
            )
        elif recent_quarters > 0:
            recent = await async_read_recent_quarters(
                client,
                broadcast,
                count=recent_quarters,
            )
    finally:
        try:
            await client.disconnect()
        except Exception:
            _LOGGER.debug("Disconnect failed", exc_info=True)
    return {"broadcast": broadcast, "history": history, "recent": recent}
