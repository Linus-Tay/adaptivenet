from ml.team_starter import live_shadow as ls


def test_persistence_needs_contiguous_rows():
    a = {("region-a", 0.0): True, ("region-a", 30.0): True, ("region-a", 60.0): False,
         ("region-a", 90.0): True, ("region-a", 180.0): True, ("region-b", 210.0): True}
    p = ls.persist(a, 2)
    assert p[("region-a", 30.0)] is True
    assert p[("region-a", 0.0)] is False
    assert p[("region-a", 180.0)] is False  # 90 s gap breaks the run
    assert p[("region-b", 210.0)] is False  # region change breaks the run
    assert ls.persist(a, 1) == a


def test_exclusion_is_padded_and_region_scoped():
    ex = [{"region": "region-a", "start_utc": "2026-11-15T00:00:00+00:00", "end_utc": "2026-11-15T00:10:00+00:00"}]
    t0 = ls.ts("2026-11-15T00:00:00+00:00")
    assert ls.in_exclusion("region-a", t0 - 60, ex)
    assert ls.in_exclusion("region-a", t0 + 600 + 600, ex)
    assert not ls.in_exclusion("region-a", t0 + 600 + 601, ex)
    assert not ls.in_exclusion("region-b", t0, ex)


def test_protocol_held_out_is_last_36h_and_after_live_training():
    p = ls.LIVE_PROTOCOL
    assert ls.ts(p["live_stop_utc"]) - ls.ts(p["held_out_start_utc"]) == 36 * 3600
    assert p["embargo_seconds"] >= 900


def test_sgt_day_boundary():
    assert ls.sgt_day(ls.ts("2026-11-14T15:59:59+00:00")) == "2026-11-14"
    assert ls.sgt_day(ls.ts("2026-11-14T16:00:00+00:00")) == "2026-11-15"


def test_witness_gap_drops_rows():
    times = [100.0, 115.0, 300.0]
    assert ls.witness_ok(times, 120.0)
    assert not ls.witness_ok(times, 200.0)
    assert ls.witness_ok(times, 300.0)
    assert ls.witness_ok(None, 5.0)
