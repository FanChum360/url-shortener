"""
Snowflake ID Generator
======================
Generates 64-bit unique IDs inspired by Twitter's Snowflake algorithm.

Bit layout (64 bits total):
  - 1  bit  : sign (always 0, keeps IDs positive)
  - 41 bits : timestamp (ms since epoch) — good for ~69 years
  - 10 bits : machine ID (supports 1024 machines)
  - 12 bits : sequence number (4096 IDs per ms per machine)

Key properties:
  - Time-ordered (sortable)
  - No coordination between nodes needed
  - ~4 million IDs/sec per machine
  - Zero collision guarantee within constraints
"""

import time
import threading
from app.config import get_settings

settings = get_settings()

# Custom epoch: 2024-01-01 00:00:00 UTC (reduces ID size)
CUSTOM_EPOCH = 1704067200000  # milliseconds

# Bit allocations
MACHINE_ID_BITS = 10
SEQUENCE_BITS = 12

# Max values
MAX_MACHINE_ID = (1 << MACHINE_ID_BITS) - 1   # 1023
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1        # 4095

# Bit shifts
MACHINE_SHIFT = SEQUENCE_BITS                   # 12
TIMESTAMP_SHIFT = MACHINE_ID_BITS + SEQUENCE_BITS  # 22


class SnowflakeGenerator:
    """
    Thread-safe Snowflake ID generator.
    
    Each instance is tied to a machine_id (0-1023).
    Multiple threads can safely call next_id() concurrently.
    """

    def __init__(self, machine_id: int):
        if not 0 <= machine_id <= MAX_MACHINE_ID:
            raise ValueError(f"machine_id must be between 0 and {MAX_MACHINE_ID}")

        self.machine_id = machine_id
        self._sequence = 0
        self._last_timestamp = -1
        self._lock = threading.Lock()

    def _current_millis(self) -> int:
        return int(time.time() * 1000) - CUSTOM_EPOCH

    def _wait_next_millis(self, last_ts: int) -> int:
        ts = self._current_millis()
        while ts <= last_ts:
            ts = self._current_millis()
        return ts

    def next_id(self) -> int:
        """Generate next unique Snowflake ID. Thread-safe."""
        with self._lock:
            ts = self._current_millis()

            if ts < self._last_timestamp:
                # Clock moved backwards — wait for recovery
                ts = self._wait_next_millis(self._last_timestamp)

            if ts == self._last_timestamp:
                self._sequence = (self._sequence + 1) & MAX_SEQUENCE
                if self._sequence == 0:
                    # Sequence exhausted for this millisecond — wait
                    ts = self._wait_next_millis(self._last_timestamp)
            else:
                self._sequence = 0

            self._last_timestamp = ts

            return (
                (ts << TIMESTAMP_SHIFT) |
                (self.machine_id << MACHINE_SHIFT) |
                self._sequence
            )

    @staticmethod
    def decode(snowflake_id: int) -> dict:
        """Decode a Snowflake ID into its components (useful for debugging)."""
        sequence = snowflake_id & MAX_SEQUENCE
        machine_id = (snowflake_id >> MACHINE_SHIFT) & MAX_MACHINE_ID
        timestamp = (snowflake_id >> TIMESTAMP_SHIFT) + CUSTOM_EPOCH
        return {
            "id": snowflake_id,
            "timestamp_ms": timestamp,
            "machine_id": machine_id,
            "sequence": sequence,
        }


# Module-level singleton
_generator = SnowflakeGenerator(machine_id=settings.machine_id)


def generate_id() -> int:
    """Generate a unique Snowflake ID."""
    return _generator.next_id()


# ─── Base62 Encoding ────────────────────────────────────────────────────────

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def to_base62(num: int) -> str:
    """
    Convert integer to Base62 string.
    
    Why Base62?
      - URL-safe (no special chars like + / = from Base64)
      - Case-sensitive → more values per character
      - 6 chars = 62^6 = ~56 billion combinations
    
    Example: 125 → "2b"
    """
    if num == 0:
        return BASE62_ALPHABET[0]

    result = []
    while num > 0:
        result.append(BASE62_ALPHABET[num % 62])
        num //= 62

    return "".join(reversed(result))


def from_base62(s: str) -> int:
    """Convert Base62 string back to integer."""
    result = 0
    for char in s:
        result = result * 62 + BASE62_ALPHABET.index(char)
    return result


def generate_short_code() -> str:
    """
    Generate a short URL code using Snowflake ID → Base62.
    
    Flow: Snowflake ID (unique 64-bit int) → Base62 string (~11 chars)
    We take the last 6 chars for brevity since Snowflake IDs are time-ordered.
    """
    snowflake = generate_id()
    return to_base62(snowflake)[-8:]  # Last 8 chars = still billions of unique codes
