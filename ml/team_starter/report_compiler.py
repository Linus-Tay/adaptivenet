"""ML-only, dependency-free report compiler for existing AdaptiveNet evidence.

It does not train models, infer incident labels, connect to cloud services, or
write to the underlying evidence files. A native comparison is only reported
when every arm has actual trained held-out results and paired evaluation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from typing import Any

GROUPS = ("aws_only", "aws_ripe", "aws_ripe_http")
LABELS = {"aws_only": "AWS-only", "aws_ripe": "AWS + RIPE", "aws_ripe_http": "AWS + RIPE + HTTP"}


def _load(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _markdown(value: Any) -> str:
    if value is None:
        return "Not available"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _nonnegative_count(obj: dict, key: str) -> int | None:
    val = obj.get(key)
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise ValueError(f"Invalid count: {key}")
    return val


def _valid_fraction(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    return float(value)


def _availability_svg(audit: dict, destination: Path) -> bool:
    """One factual bar chart of journal statuses; no model-quality inference."""
    counts = audit.get("journal_status_counts") or {}
    if not isinstance(counts, dict) or not counts:
        return False
    data = []
    for k, v in sorted(counts.items()):
        if not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError("Invalid journal status distribution")
        data.append((k, v))
    total = sum(v for _, v in data)
    if not total:
        return False
    width, height = 780, 90 + 46 * len(data)
    maxval = max(v for _, v in data)
    elems = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#ffffff"/>',
             '<text x="20" y="32" font-size="20" font-family="Arial,sans-serif" font-weight="bold">Ingestion journal statuses</text>',
             '<text x="20" y="53" font-size="13" font-family="Arial,sans-serif" fill="#444">Counts in the supplied availability-audit file; not ML detection results</text>']
    for i, (name, num) in enumerate(data):
        y = 78 + i * 46
        bar = max(1, round(480 * num / maxval)) if num else 0
        elems.append(f'<text x="20" y="{y+19}" font-size="15" font-family="Arial,sans-serif">{html.escape(name)}</text>')
        elems.append(f'<rect x="175" y="{y}" width="{bar}" height="25" fill="#365a82"/>')
        elems.append(f'<text x="{188+bar}" y="{y+19}" font-size="14" font-family="Arial,sans-serif">{num}</text>')
    elems.append('</svg>')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(elems) + "\n", encoding="utf-8")
    return True


def _f1_svg(models: dict, destination: Path) -> bool:
    vals = []
    for g in GROUPS:
        model = models.get(g)
        if not isinstance(model, dict) or model.get("status") != "trained" or not model.get("model_artifact"):
            return False
        test = model.get("test") or {}
        if test.get("status") != "evaluated":
            return False
        v = _valid_fraction(test.get("macro_f1"))
        if v is None:
            return False
        vals.append((LABELS[g], v))
    width, height = 780, 250
    elems = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             '<text x="20" y="29" font-size="20" font-family="Arial,sans-serif" font-weight="bold">Native held-out macro F1</text>',
             '<text x="20" y="50" font-size="13" font-family="Arial,sans-serif">Only from trained native classifiers with evaluated held-out results</text>']
    for i, (name, val) in enumerate(vals):
        y = 72 + i*54
        elems += [f'<text x="20" y="{y+18}" font-size="14" font-family="Arial,sans-serif">{html.escape(name)}</text>',
                  f'<rect x="210" y="{y}" width="450" height="25" fill="#e9eef5"/>',
                  f'<rect x="210" y="{y}" width="{round(450*val)}" height="25" fill="#1d6675"/>',
                  f'<text x="673" y="{y+18}" font-size="14" font-family="Arial,sans-serif">{val:.3f}</text>']
    elems.append('</svg>')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(elems) + "\n", encoding="utf-8")
    return True


def compile_report(audit: dict | None, evaluation: dict | None, paired: dict | None,
                   shadow: dict | None, *, created_at: str, figures_dir: Path | None = None,
                   out_dir: Path | None = None) -> str:
    """Create a cautious Markdown report. All figures are evidence-derived."""
    evaluation = evaluation or {}
    paired = paired or {}
    audit = audit or {}
    shadow = shadow or {}
    models = {g: evaluation.get(g) for g in GROUPS}
    all_trained = all(isinstance(models[g], dict) and models[g].get("status") == "trained" and
                      bool(models[g].get("model_artifact")) and
                      (models[g].get("test") or {}).get("status") == "evaluated" and
                      _valid_fraction((models[g].get("test") or {}).get("macro_f1")) is not None
                      for g in GROUPS)
    paired_valid = paired.get("status") == "evaluated" and paired.get("report_version") == "adaptivenet.paired_incident_evaluation.v1" and all_trained
    lines = ["# AdaptiveNet — ML evidence and evaluation report", "",
             f"Generated: {_markdown(created_at)}", "",
             "> **Scope:** condition detection (forecast horizon: 0 seconds), not advance outage prediction.",
             "> This document summarizes provided local artifacts. It does not independently verify human-reviewed labels, AWS availability, or exact per-metric receipt timestamps.",
             "", "## 1. Evidence readiness", ""]
    if audit:
        counts = audit.get("counts") or {}
        if not isinstance(counts, dict):
            raise ValueError("Invalid audit counts object")
        snap = _nonnegative_count(counts, "archive_snapshots")
        matched = _nonnegative_count(counts, "matched_current_ingestion_runs")
        sql = _nonnegative_count(counts, "matched_current_sqlite_snapshots")
        nulls = _nonnegative_count(counts, "null_feature_values")
        prov = _nonnegative_count(counts, "timestamped_source_provenance_entries")
        issues = _nonnegative_count(audit, "total_issue_count")
        lines += ["| Source audit item | Reported result |", "|---|---:|",
                  f"| Archived snapshots | {_markdown(snap)} |",
                  f"| Matched ingestion runs | {_markdown(matched)} |",
                  f"| Matched persisted snapshots | {_markdown(sql)} |",
                  f"| Source provenance records with timestamps | {_markdown(prov)} |",
                  f"| Null feature entries | {_markdown(nulls)} |",
                  f"| Reported audit issues | {_markdown(issues)} |",
                  "", f"Audit status: `{_markdown(audit.get('status', 'not_available'))}`.", ""]
        if audit.get("strict_historical_raw_replay_certified") is True:
            lines.append("The audit claims strict replay certification; this report does **not** independently endorse that claim.")
        else:
            lines.append("**Strict historical raw replay is not certified.** Exact per-metric result-receipt times are absent from the upstream snapshots.")
        if issues is not None and issues > 0:
            lines.append("**Action required:** The audit flagged issues; inspect its JSON before interpreting any model results.")
        lines.append("")
    else:
        lines += ["No availability-audit artifact supplied. Data readiness cannot be checked here.", ""]
    if figures_dir is not None and out_dir is not None and audit:
        if _availability_svg(audit, figures_dir / "ingestion-status-counts.svg"):
            lines += ["![Ingestion journal status counts](" + (figures_dir / "ingestion-status-counts.svg").relative_to(out_dir).as_posix() + ")", ""]
    lines += ["## 2. Native three-arm comparison", "", "| Feature arm | Model status | Held-out samples | Held-out macro F1 | Abstentions |", "|---|---|---:|---:|---:|"]
    for g in GROUPS:
        m = models[g]
        if not isinstance(m, dict):
            lines.append(f"| {LABELS[g]} | report missing | — | — | — |")
            continue
        status = m.get("status", "unknown")
        test = m.get("test") or {}
        trained_test = status == "trained" and bool(m.get("model_artifact")) and test.get("status") == "evaluated"
        f1 = _valid_fraction(test.get("macro_f1")) if trained_test else None
        samples = test.get("samples") if trained_test else None
        abst = test.get("abstentions") if trained_test else None
        f1str = f"{f1:.4f}" if f1 is not None else "Not available"
        lines.append(f"| {LABELS[g]} | {_markdown(status)} | {_markdown(samples)} | {f1str} | {_markdown(abst)} |")
    lines += ["", "**Primary comparison requirement:** identical independently reviewed held-out incidents, whole-experiment separation and no test-set tuning.", ""]
    if paired_valid:
        cnt = paired.get("verified_held_out_incidents")
        lines += [f"Paired evaluator: `evaluated` on {_markdown(cnt)} reported held-out incidents.", "",
                  "| Pair | Both detected | Median AWS-only minus augmented delay (s) | Augmented-only detected | AWS-only-only detected |",
                  "|---|---:|---:|---:|---:|"]
        for g in GROUPS[1:]:
            p = (paired.get("paired") or {}).get("aws_only_vs_" + g) or {}
            lines.append("| AWS-only vs " + LABELS[g] + " | " + " | ".join(_markdown(p.get(k)) for k in
                         ("both_detected", "median_aws_only_minus_external_delay_seconds_when_both_detected", "external_only_detected", "aws_only_only_detected")) + " |")
        lines += ["", "Delay differences are calculated **only among incidents detected by both arms**. Misses must be assessed separately; a lower median delay alone does not prove superiority.", ""]
    else:
        blockers = paired.get("blockers") or []
        if not isinstance(blockers, list):
            blockers = []
        lines += ["**Native paired result: NOT EVALUABLE.** The necessary all-arm trained held-out evidence and successful paired comparison are not present.", "",
                  "Reported blockers: " + ("; ".join(_markdown(x) for x in blockers) if blockers else "required native results unavailable or inconsistent") + ".", ""]
    if figures_dir is not None and out_dir is not None and paired_valid and _f1_svg(models, figures_dir / "native-heldout-macro-f1.svg"):
        lines += ["![Native held-out macro F1](" + (figures_dir / "native-heldout-macro-f1.svg").relative_to(out_dir).as_posix() + ")", ""]
    lines += ["## 3. Shadow-only safety and operational boundary", ""]
    review = shadow.get("decision_review") or shadow.get("independent_review") or {}
    proposal = shadow.get("proposal") or {}
    if not isinstance(review, dict) or not isinstance(proposal, dict):
        review, proposal = {}, {}
    if review:
        lines += ["| Reviewer field | Recorded value |", "|---|---|",
                  f"| Proposal model | `{_markdown(proposal.get('model_id'))}` |",
                  f"| Proposal action | `{_markdown(proposal.get('action'))}` |",
                  f"| Policy valid | `{_markdown(review.get('policy_valid'))}` |",
                  f"| Approved for execution | `{_markdown(review.get('approved_for_execution'))}` |",
                  f"| Executed | `{_markdown(review.get('executed'))}` |",
                  f"| Effective action | `{_markdown(review.get('effective_action'))}` |", ""]
        if review.get("executed") is True or review.get("approved_for_execution") is True or review.get("effective_action") != "hold":
            lines.append("**SAFETY WARNING:** Reviewer artifact does not show a non-executing hold; investigate before making safety claims.")
        else:
            lines.append("The *provided* review artifact documents a non-executing hold; it does not demonstrate deployed mitigation or prove all potential execution paths safe.")
    else:
        lines.append("No independent shadow-review result supplied.")
    lines += ["", "## 4. Public datasets — separate research evidence", "",
              "Edge Fault and SOFI published-data benchmarks can be documented in a separate section with their own splits, methods and labels. **Do not copy their scores into the native comparison table.**", "",
              "## 5. Scientific limitations and remaining work", "",
              "- Collect and independently review real fault incidents; a requested fault action or a missing source alone is not an accepted incident label.",
              "- Archive genuine time-aligned snapshots of both healthy and fault periods, across multiple independent experiments.",
              "- RIPE ICMP sampling and local HTTP witness intervals differ; one local witness cannot establish Internet-wide impact.",
              "- Source event-time provenance does not establish exact receipt time for each metric; avoid claiming strict raw historical replay.",
              "- Classifier training, held-out detection delays, false alarms/day, model confidence and paired scores remain unavailable unless real native held-out evidence exists.",
              "- No autonomous route, scale, SSM or other AWS write action is authorized by these ML reports.",
              "", "## 6. Reproduction commands (run from repository root)", "",
              "```powershell",
              "python -m pytest -q ml/team_starter/tests",
              "python -m pytest -q",
              "python -m ml.team_starter.availability_audit --snapshots data/ml-archive/feature_snapshots.jsonl --db data/adaptivenet-ingestion.db --out data/ml-results/native/availability-audit.json",
              "python -m ml.team_starter.offline_cli train --dataset data/ml-archive/verified-dataset.json --group all --out data/ml-results/native",
              "python -m ml.team_starter.offline_cli compare --dataset data/ml-archive/verified-dataset.json --predictions-dir data/ml-results/native --out data/ml-results/native/paired-incident-report.json",
              "```", "",
              "The last two commands correctly return `not_trained` or `not_evaluable` until sufficient accepted native incidents exist. Re-run the report compiler after the underlying artifacts change.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--audit", required=True, type=Path)
    p.add_argument("--evaluation", required=True, type=Path)
    p.add_argument("--paired", required=True, type=Path)
    p.add_argument("--shadow", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args(argv)
    if any(args.out.resolve() == input_path.resolve() for input_path in
           (args.audit, args.evaluation, args.paired, args.shadow)):
        p.error("refusing to overwrite input evidence")
    audit, evaluation, paired, shadow = map(_load, (args.audit, args.evaluation, args.paired, args.shadow))
    figures = args.out.parent / "figures"
    report = compile_report(audit, evaluation, paired, shadow,
                            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            figures_dir=figures, out_dir=args.out.parent)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print("Report:", args.out)
    print("Native three-arm comparison:", "evaluable" if all((evaluation or {}).get(g, {}).get("status") == "trained" for g in GROUPS) and (paired or {}).get("status") == "evaluated" else "NOT EVALUABLE")
    print("Figures:", figures if figures.exists() else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
