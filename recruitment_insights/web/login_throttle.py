"""Bounded, process-local failure budget for invitation account logins."""
from collections import deque
from dataclasses import dataclass, field
import math
import threading
import time


@dataclass
class _Entry:
    failures: deque = field(default_factory=deque)
    pending: int = 0
    cooldown_until: float = 0
    generation: int = 0
    serial: int = 0
    reset_serial: int = 0


@dataclass(frozen=True)
class _Attempt:
    username: str
    entry: _Entry
    generation: int
    serial: int


class LoginThrottle:
    """Reserve verification slots without holding the lock during account work.

    Completed failures and pending verifications share a five-attempt budget.
    A success resets its generation only if no newer attempt has failed. Older
    results cannot erase or charge the new budget, while later-started failures
    still count even if their verification overlaps a successful reset.
    """

    FAILURE_LIMIT = 5
    WINDOW_SECONDS = 300
    COOLDOWN_SECONDS = 60

    def __init__(self, *, clock=None, capacity=1024):
        self._clock = time.monotonic if clock is None else clock
        self._capacity = capacity
        self._entries = {}
        self._lock = threading.Lock()

    def _expire(self, entry, now):
        if entry.cooldown_until and now >= entry.cooldown_until:
            entry.failures.clear()
            entry.cooldown_until = 0
            entry.generation += 1
            entry.reset_serial = entry.serial
        while entry.failures and entry.failures[0][0] <= now - self.WINDOW_SECONDS:
            entry.failures.popleft()

    def begin(self, username):
        """Return a reserved attempt or (None, positive Retry-After seconds)."""
        with self._lock:
            now = self._clock()
            entry = self._entries.get(username)
            if entry is None:
                # Never evict live failures, cooldowns, or in-flight attempts.
                # Refuse new names under pressure rather than let name rotation
                # displace the protection of an account already being attacked.
                for name, candidate in list(self._entries.items()):
                    self._expire(candidate, now)
                    if not candidate.pending and not candidate.failures:
                        del self._entries[name]
                if len(self._entries) >= self._capacity:
                    return None, 1
                entry = self._entries[username] = _Entry()
            else:
                self._expire(entry, now)
            if entry.cooldown_until > now:
                return None, max(1, math.ceil(entry.cooldown_until - now))
            if len(entry.failures) + entry.pending >= self.FAILURE_LIMIT:
                return None, 1
            entry.pending += 1
            entry.serial += 1
            return _Attempt(username, entry, entry.generation, entry.serial), 0

    def finish(self, attempt, outcome):
        """Release every reservation; only an actual credential failure counts."""
        with self._lock:
            entry = self._entries.get(attempt.username)
            if entry is not attempt.entry:
                return
            now = self._clock()
            self._expire(entry, now)
            entry.pending -= 1
            if outcome == 'failure' and attempt.serial > entry.reset_serial:
                entry.failures.append((now, attempt.serial))
                if len(entry.failures) >= self.FAILURE_LIMIT:
                    entry.cooldown_until = now + self.COOLDOWN_SECONDS
            elif (outcome == 'success' and attempt.generation == entry.generation
                  and all(serial <= attempt.serial for _, serial in entry.failures)):
                entry.failures.clear()
                entry.cooldown_until = 0
                entry.generation += 1
                entry.reset_serial = attempt.serial
            if not entry.pending and not entry.failures:
                del self._entries[attempt.username]
