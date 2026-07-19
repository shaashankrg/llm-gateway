import time
from enum import Enum


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, cooldown_seconds: float = 30.0):
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.opened_at = None
        self.cooldown_seconds = cooldown_seconds

    def record_failure(self):
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
            return
        self.failure_count += 1
        if self.failure_count >= 5:
            self.state = CircuitState.OPEN
            self.opened_at = time.time()

    def record_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def can_attempt(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.time() - self.opened_at
            if elapsed >= self.cooldown_seconds:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            return True


circuit_breakers = {
    "openai": CircuitBreaker(),
    "anthropic": CircuitBreaker(),
}
