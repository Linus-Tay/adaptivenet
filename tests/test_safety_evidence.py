import math

import pytest

from adaptivenet.controller.evidence import EvidenceStreak


def test_repeated_and_out_of_order_samples_do_not_advance_streak():
    streak = EvidenceStreak()
    results = [
        streak.observe(observed_at=at, qualified_intent="shift:region-b:40")
        for at in (100, 100, 110, 100, 110, 120)
    ]
    assert results == [1, 1, 2, 2, 2, 3]


def test_changed_target_starts_new_streak_without_reusing_latest_sample():
    streak = EvidenceStreak()
    assert streak.observe(observed_at=100, qualified_intent="shift:region-b:40") == 1
    assert streak.observe(observed_at=110, qualified_intent="shift:region-b:40") == 2
    assert streak.observe(observed_at=110, qualified_intent="shift:region-b:35") == 0
    assert streak.observe(observed_at=120, qualified_intent="shift:region-b:35") == 1


def test_failed_policy_check_breaks_streak_and_consumes_sample():
    streak = EvidenceStreak()
    intent = "shift:region-b:40"
    assert streak.observe(observed_at=100, qualified_intent=intent) == 1
    assert streak.observe(observed_at=110, qualified_intent=None) == 0
    assert streak.observe(observed_at=110, qualified_intent=intent) == 0
    assert streak.observe(observed_at=120, qualified_intent=intent) == 1


@pytest.mark.parametrize("invalid_time", [None, math.nan, math.inf, "120", True])
def test_invalid_timestamp_resets_streak_without_allowing_replay(invalid_time):
    streak = EvidenceStreak()
    intent = "shift:region-b:40"
    assert streak.observe(observed_at=100, qualified_intent=intent) == 1
    assert streak.observe(observed_at=invalid_time, qualified_intent=intent) == 0
    assert streak.observe(observed_at=100, qualified_intent=intent) == 0
