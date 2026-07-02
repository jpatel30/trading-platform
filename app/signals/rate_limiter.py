"""
Token bucket rate limiter for Unusual Whales API.
Applied globally in _get() — one change fixes all 429s across the entire codebase.

Target: 120 calls/min = 2 calls/second
Buffer: 110 calls/min used (keeps headroom for bursts)
"""
import threading
import time


class TokenBucket:
    """
    Thread-safe token bucket for rate limiting.
    Refills at a constant rate, blocks when empty.
    """
    def __init__(self, rate: float, capacity: int):
        """
        rate:     tokens per second (120/min = 2.0/sec)
        capacity: max burst size (allow burst of up to N calls)
        """
        self.rate     = rate
        self.capacity = capacity
        self.tokens   = float(capacity)
        self.last_refill = time.monotonic()
        self._lock    = threading.Lock()

    def acquire(self):
        """Block until a token is available, then consume it."""
        with self._lock:
            self._refill()
            if self.tokens < 1:
                # Calculate exact wait time
                wait = (1 - self.tokens) / self.rate
                time.sleep(wait)
                self._refill()
            self.tokens -= 1

    def _refill(self):
        now     = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now


# Global singleton — shared across all threads, all UW calls
# 110/min used (buffer below 120 hard limit)
_uw_bucket = TokenBucket(rate=110/60, capacity=10)


def acquire_uw_token():
    """Call before every UW API request."""
    _uw_bucket.acquire()
