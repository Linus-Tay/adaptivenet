"""Amendment 10 (POST-HOC, approved by MY 2026-11-18 04:06 UTC): corrected held-out false-alarm counts.

The pre-registered exclusion used each SSM command's own timeout (180 s) as its window, so a Tuesday
trial was excluded only from request - 60 s to request + 780 s, while the fault lasts 960 s and the RIPE
loss window adds 480 s more. Held-out rows inside live faults were therefore counted as healthy.
This recount drops the impaired region from request - 60 s to request + duration + 600 s (the
campaign_ml.build_rows rule) and reports original and corrected counts side by side. Verdicts are NOT
changed; they stay as pre-registered. Also adds the AWS-only view of the frozen rule on the Tuesday trials.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from . import campaign_ml as cm
from . import live_shadow as ls
from . import supervised_live as sl


def main(C: Path, L: Path) -> str:
    rep = json.loads((L / "retrain-report.json").read_text(encoding="utf-8"))
    amp = {r: ls.read_jsonl(L / f"amp-{r}.jsonl") for r in ("region-a", "region-b")}
    idx = ls.load_index(ls._ripe_spec(L))
    wit = ls.witness_times(L / "http_witness.jsonl")
    _, slots = ls.trial_rows_slots(ls.live_rows(amp, idx, [], witness=wit), L)
    fixed = [{"region": s["region"], "start_utc": ls.iso(s["request_unix"]),
              "end_utc": ls.iso(s["request_unix"] + s["duration_seconds"]), "reason": f"trial slot {s['slot']}"}
             for s in slots]          # in_exclusion adds the 60 s pre and 600 s post pads
    hl = ls.ts(ls.LIVE_PROTOCOL["held_out_start_utc"])
    emb = ls.LIVE_PROTOCOL["embargo_seconds"]
    pre_ex = json.loads((L / "exclusions-ssm.json").read_text(encoding="utf-8"))
    # Alerts are computed on the pre-registered held-out rows; the correction then DROPS rows, so
    # persistence state is the same as in the pre-registered run.
    held = [r for r in ls.live_rows(amp, idx, pre_ex, witness=wit) if r["aws"] and r["ts"] >= hl]
    keep = {(r["region"], r["ts"]) for r in held if not ls.in_exclusion(r["region"], r["ts"], fixed)}
    held_c = [r for r in held if (r["region"], r["ts"]) in keep]
    train = [r for r in ls.live_rows(amp, idx, [], witness=wit) if r["aws"] and r["ts"] < hl - emb]
    camp, _ = ls.campaign_rows(C, C / "campaign-plan.json",
                               {"region-a": [C / "ripe/236862553.json"], "region-b": [C / "ripe/236862555.json"]})
    camp_h = [r for r in camp if r["label"] == "healthy"]
    alerts = {}
    alerts["13 Nov model (current/" + ls.PRIMARY + ")"] = (held, ls.predict(camp_h, held, ls.PRIMARY))
    alerts["planned retrain (retrained/" + ls.PRIMARY + ")"] = (held, ls.predict(camp_h + train, held, ls.PRIMARY))
    docs = Path(__file__).resolve().parents[2] / "docs" / "ml" / "live"
    picks = {"Monday pick (failed)": json.loads((docs / "candidate-pick.json").read_text(encoding="utf-8"))["pick"],
             "pick 2 (amendment 7)": json.loads((docs / "candidate-pick-2.json").read_text(encoding="utf-8"))["pick"],
             "early-warning pick (amendment 8)": ls.AMENDMENT_8["pick"]}
    cands = {ls.cand_name(c): c for c in ls.candidates()}
    for label, name in picks.items():
        c = cands[name]
        al = ls.candidate_alerts((camp_h if c["training_data"] == "campaign+live" else []) + train, held, c)
        alerts[f"{label}: {name}"] = (held, ls.persist(al, c["persistence_rows"]))
    names = {"frozen_aws_rule": "AWS-only (aws_view_alert)", "frozen_aws_ripe_rule_loss_only": "frozen loss_only rule (comparator)",
             "frozen_ripe_rule_loss_only": "frozen loss_only rule, RIPE-only", "frozen_aws_ripe_rule": "frozen main rule (primary)",
             "frozen_ripe_rule": "frozen main rule, RIPE-only", "frozen_aws_ripe_rule_loss_or_rtt2": "frozen loss_or_rtt2 rule",
             "frozen_ripe_rule_loss_or_rtt2": "frozen loss_or_rtt2 rule, RIPE-only"}
    for k, al in ls.frozen_rules(held, amp).items():
        alerts[names.get(k, k)] = (held, al)
    spec = json.loads((docs / "supervised-picks.json").read_text(encoding="utf-8"))["frozen"]
    models = sl.frozen_models(C, L, spec)
    w = sl.Witness([L / "http_witness.jsonl", L / "witness.stdout"])
    held_h = sl.with_http(held, w)
    for name, (m, s) in models.items():
        rows = held if name == "S1" else held_h
        alerts[f"supervised {name}: {sl.cname(s)}"] = (rows, sl.alerts_for(m, rows, sl.COLS[name][s["columns"]],
                                                                         s["threshold"], s["persistence"]))
    alerts["lab HTTP rule (context)"] = (held_h, {(r["region"], r["ts"]): sl.lab_http_rule(w, r["region"], r["ts"])
                                                  for r in held_h})
    out, orig = {}, {}
    for k, (rows, al) in alerts.items():
        orig[k] = ls.fa_summary(rows, al)
        out[k] = ls.fa_summary([r for r in rows if (r["region"], r["ts"]) in keep], al)
    # AWS-only frozen view on the Tuesday trials
    trows, tslots = ls.trial_rows_slots(ls.live_rows(amp, idx, [], witness=wit), L)
    sc = cm.score(trows, ls.frozen_rules(trows, amp)["frozen_aws_rule"], tslots)
    f = lambda v: f"{v['false_alarms_per_day']} ({v['false_alarms_per_day_ci95'][0]}-{v['false_alarms_per_day_ci95'][1]}), {v['false_alarm_onsets']} onsets, {v['healthy_hours']} h"
    lines = ["", "## Amendment 10 (POST-HOC, 2026-11-18, approved by MY 04:06 UTC): corrected false-alarm counts", "",
             "The pre-registered exclusion ended each Tuesday trial's window at the SSM command timeout (request + 180 s, "
             "padded to + 780 s). The fault lasts 960 s and the RIPE loss window adds 480 s, so held-out rows inside live "
             "faults were counted as healthy. The corrected count drops the impaired region from request - 60 s to "
             "request + duration + 600 s (the campaign_ml.build_rows rule). This change was made after seeing the data. "
             "**Both counts are shown, and every pre-registered verdict stays as it was.**", "",
             "| detector | held-out FA/day, as pre-registered | held-out FA/day, corrected |", "|---|---|---|"]
    for k, v in out.items():
        lines.append(f"| {k} | {f(orig[k])} | {f(v)} |")
    lines += ["", f"Held-out rows: {len(held)} as pre-registered, {len(held_c)} with amendment 10.", "",
              "| test | pre-registered verdict (stands) | with amendment 10 (post hoc, for information) |", "|---|---|---|",
              "| early-warning pick (amendment 8) | did not pass | did not pass (no extra trial; earlier on 2 of 8; FA above 5/day) |",
              "| S1 (amendment 9) | did not pass | did not pass as pre-registered; meets the limit after the post hoc correction by one alarm |",
              "| S2 (amendment 9) | did not pass | did not pass (FA above 5/day) |", "",
              f"**AWS-only view of the frozen rule on the Tuesday trials:** {sc['trials_detected']}/{sc['trials_evaluated']} "
              f"trials detected, controls alarmed {sc['controls_alarmed']}/{sc['controls_evaluated']}.", ""]
    md = "\n".join(lines)
    (L / "amendment10.json").write_text(json.dumps({"rows": {"pre_registered": len(held), "amendment_10": len(held_c)},
                                                    "corrected": out, "original": orig, "aws_only_tuesday":
                                                    {k: sc[k] for k in ("trials_detected", "trials_evaluated",
                                                                        "controls_alarmed", "controls_evaluated")}},
                                                   indent=2, default=float), encoding="utf-8")
    return md


if __name__ == "__main__":
    print(main(Path(sys.argv[1]), Path(sys.argv[2])))
