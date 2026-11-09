"""Count qualifying samples from one controller-selected measurement stream."""
from __future__ import annotations

from dataclasses import dataclass, field
from math import inf


@dataclass
class EvidenceStreak:
    required: int = 3
    _last_observed_at: float = field(default=0, init=False)
    _intent: str | None = field(default=None, init=False)
    _count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if type(self.required) is not int or self.required < 1:
            raise ValueError("required must be a positive integer")

    def observe(self, *, observed_at: float | None, qualified_intent: str | None) -> int:
        """Return the streak length for a fresh, independently reviewed intent.

        The caller selects the stream and supplies its source timestamp. It
        supplies no intent when freshness or policy checks fail. An intent
        identifies the action and target using controller-owned values.
        """
        if type(observed_at) not in (int, float) or not 0 < observed_at < inf:
            self._intent = None
            self._count = 0
            return 0

        is_new = observed_at > self._last_observed_at
        if is_new:
            self._last_observed_at = observed_at

        if qualified_intent != self._intent:
            self._intent = qualified_intent
            self._count = 0
        if qualified_intent and is_new:
            self._count = min(self._count + 1, self.required)
        return self._count
