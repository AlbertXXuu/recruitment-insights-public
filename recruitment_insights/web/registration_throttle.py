"""Small process-wide attempt budget for explicitly enabled external sign-ups."""
from collections import deque
import math
import threading
import time


class RegistrationThrottle:
    """Count all admitted attempts before password work, including invalid ones.

    A fixed-size shared budget cannot be bypassed by rotating usernames or
    forged proxy addresses. It limits this process, not a distributed deployment.
    """

    ATTEMPT_LIMIT = 10
    WINDOW_SECONDS = 60

    def __init__(self, *, clock=None):
        self._clock = time.monotonic if clock is None else clock
        self._attempts = deque()
        self._lock = threading.Lock()

    def reserve(self):
        """Return 0 when admitted, otherwise a positive Retry-After in seconds."""
        with self._lock:
            now = self._clock()
            while self._attempts and self._attempts[0] <= now - self.WINDOW_SECONDS:
                self._attempts.popleft()
            if len(self._attempts) >= self.ATTEMPT_LIMIT:
                return max(1, math.ceil(self._attempts[0] + self.WINDOW_SECONDS - now))
            self._attempts.append(now)
            return 0
