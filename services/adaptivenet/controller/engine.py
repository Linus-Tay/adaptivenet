"""Conservative two-region resilience decision engine.

Only acts on explicit, recent, repeated observations. Automation defaults off.
The local gateway is the only supported actuator; AWS routing is NOT modified.
"""
from dataclasses import dataclass, field
import time

@dataclass
class Controller:
    consecutive_required: int = 3
    cooldown_seconds: int = 120
    degraded_latency_ms: float = 800
    degraded_error_ratio: float = .20
    consecutive_a: int = 0
    consecutive_b: int = 0
    last_action: float = field(default=float('-inf'))

    def evaluate(self, a: dict, b: dict, current_a_weight: int, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        def classify(s):
            if not s or s.get('status') != 'ok' or s.get('age_seconds', 10**9) > 90:
                return 'unknown'
            if s.get('samples', 0) < 3:
                return 'unknown'
            if s.get('error_ratio', 1) >= self.degraded_error_ratio or s.get('p95_ms', float('inf')) >= self.degraded_latency_ms:
                return 'degraded'
            return 'healthy'
        x, y = classify(a), classify(b)
        self.consecutive_a = self.consecutive_a + 1 if x == 'degraded' and y == 'healthy' else 0
        self.consecutive_b = self.consecutive_b + 1 if y == 'degraded' and x == 'healthy' else 0
        result = {'region_a': x, 'region_b': y, 'action': 'hold', 'reason': 'insufficient evidence or healthy', 'target_region_a_percent': current_a_weight}
        if now - self.last_action < self.cooldown_seconds:
            result['reason'] = 'cooldown'; return result
        if self.consecutive_a >= self.consecutive_required and current_a_weight != 0:
            result.update(action='propose_shift', target_region_a_percent=0, reason='region-a degraded repeatedly; region-b healthy')
        elif self.consecutive_b >= self.consecutive_required and current_a_weight != 100:
            result.update(action='propose_shift', target_region_a_percent=100, reason='region-b degraded repeatedly; region-a healthy')
        return result

    def mark_applied(self, now: float | None = None):
        self.last_action = time.monotonic() if now is None else now
        self.consecutive_a = self.consecutive_b = 0
