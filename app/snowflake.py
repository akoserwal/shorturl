import time
import threading

CUSTOM_EPOCH = 1719792000000  # 2024-07-01T00:00:00Z in ms

TIMESTAMP_BITS = 41
WORKER_BITS = 10
SEQUENCE_BITS = 12

MAX_WORKER_ID = (1 << WORKER_BITS) - 1
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1

WORKER_SHIFT = SEQUENCE_BITS
TIMESTAMP_SHIFT = WORKER_BITS + SEQUENCE_BITS


class SnowflakeGenerator:
    def __init__(self, worker_id: int):
        if not 0 <= worker_id <= MAX_WORKER_ID:
            raise ValueError(f"worker_id must be 0-{MAX_WORKER_ID}")
        self.worker_id = worker_id
        self.sequence = 0
        self.last_timestamp = -1
        self._lock = threading.Lock()

    def _current_ms(self) -> int:
        return int(time.time() * 1000)

    def generate(self) -> int:
        with self._lock:
            ts = self._current_ms()
            if ts == self.last_timestamp:
                self.sequence = (self.sequence + 1) & MAX_SEQUENCE
                if self.sequence == 0:
                    while ts <= self.last_timestamp:
                        ts = self._current_ms()
            else:
                self.sequence = 0
            self.last_timestamp = ts
            return (
                ((ts - CUSTOM_EPOCH) << TIMESTAMP_SHIFT)
                | (self.worker_id << WORKER_SHIFT)
                | self.sequence
            )
