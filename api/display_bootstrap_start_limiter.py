"""Fixed-unit startup throttling: restart begins with a full quiet window."""
import math
import os
import time
from collections import deque


class StartLimiter:
    def __init__(self, units):
        if (type(units) is not tuple or not units or len(set(units)) != len(units)
                or any(type(unit) is not str or not unit.endswith('.service')
                       or '/' in unit or '@' in unit for unit in units)):
            raise ValueError('INVALID_INPUT')
        self._pid = os.getpid()
        self._started = time.monotonic()
        self._last = self._started
        self._attempts = {unit: deque(maxlen=5) for unit in units}

    def consume(self, unit):
        """Reserve before the single PID1 call, including failed start attempts."""
        now = time.monotonic()
        if (self._pid != os.getpid() or unit not in self._attempts
                or not math.isfinite(now) or now < self._last):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        self._last = now
        if now - self._started < 60:
            raise ValueError('RESOURCE_LIMIT')
        attempts = self._attempts[unit]
        while attempts and now - attempts[0] >= 60:
            attempts.popleft()
        if len(attempts) >= 5:
            raise ValueError('RESOURCE_LIMIT')
        attempts.append(now)
