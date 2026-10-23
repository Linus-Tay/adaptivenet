"""Cross-platform read-only source collector + persisted feature builder."""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from .config import region_configs
from .features import make_snapshot, AMP_CLOCK_SKEW_SEC
from .sources import collect_all
from .store import Store
from .decisions import review_proposal
from .cloudfront import distribution_id, collect_cloudfront


def collect_source_poll(*, profile="adaptivenet-admin", env_file=None, now=None):
    """Existing live collectors shared by direct SQLite and queued SQS modes."""
    started_at = time.time() if now is None else now
    collected = collect_all(region_configs(env_file), profile, started_at)
    cf_id = distribution_id(env_file)
    if cf_id:
        try:
            collected.observations.extend(collect_cloudfront(cf_id, profile, started_at))
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            collected.errors.append("global:cloudfront:" + type(exc).__name__)
    evaluated_at = max(started_at, time.time()) if now is None else now
    return collected, cf_id, evaluated_at


def cycle(db_path, *, profile="adaptivenet-admin", env_file=None, now=None):
    collected, cf_id, evaluated_at = collect_source_poll(profile=profile, env_file=env_file, now=now)
    store = Store(db_path)
    saved = store.record(collected.observations, collected.errors, collected.status, at=evaluated_at)
    # Source timestamps remain immutable; do not refresh historical Atlas data.
    # Include only bounded future-skewed AMP samples. The feature builder also
    # checks collected_at and excludes future RIPE probe data.
    # Last-known CloudFront values are a separate, display-only history.
    # Live metric freshness and model inputs still use the short event window.
    cf_history = (store.latest_cloudfront_observations(cf_id, at=evaluated_at)
                  if cf_id else [])
    snapshot = make_snapshot(store.events_since(evaluated_at - 2100, end=evaluated_at + AMP_CLOCK_SKEW_SEC),
                             evaluated_at, errors=collected.errors, cloudfront_distribution=cf_id,
                             cloudfront_last_observations=cf_history)
    store.store_snapshot(snapshot)
    store.prune(retain_days=7, now=evaluated_at)
    return snapshot, saved


def main(argv=None):
    parser = argparse.ArgumentParser(description="AdaptiveNet versioned, read-only telemetry ingestion")
    parser.add_argument("command", choices=("once", "watch", "serve", "export", "review"))
    parser.add_argument("--db", default="data/adaptivenet-ingestion.db")
    parser.add_argument("--profile", default="adaptivenet-admin")
    parser.add_argument("--env-file", default=".env", help="Optional local project .env; reads infrastructure IDs only")
    parser.add_argument("--interval", type=int, default=60, help="Minimum 60s (AWS query cost guard)")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    parser.add_argument("--out", help="Export filename; stdout otherwise")
    parser.add_argument("--proposal", help="JSON file with a model's proposal (review only)")
    parser.add_argument("--current-a-percent", type=int, default=50)
    args = parser.parse_args(argv)
    store = Store(args.db)
    if args.command == "serve":
        from .api import serve
        serve(args.db, port=args.port); return 0
    if args.command == "review":
        if not args.proposal:
            parser.error("--proposal path.json required")
        snap = store.latest_snapshot()
        if snap is None:
            parser.error("no persisted snapshot; run once first")
        proposal = json.loads(Path(args.proposal).read_text())
        print(json.dumps(review_proposal(snap,proposal,current_region_a_percent=args.current_a_percent), indent=2)); return 0
    if args.command == "export":
        snapshots = list(reversed(store.recent_snapshots(args.limit)))
        if args.format == "jsonl":
            output = "".join(json.dumps(s,allow_nan=False)+"\n" for s in snapshots)
        else:
            from io import StringIO
            f = StringIO(); w=csv.writer(f)
            w.writerow(["snapshot_id","computed_at","ready_for_model","region","feature","value","quality_internal","quality_external"])
            for s in snapshots:
                for region,data in s["regions"].items():
                    for feature,value in data["features"].items():
                        w.writerow([s["snapshot_id"],s["computed_at"],s["ready_for_model"],region,feature,
                                    "" if value is None else value,data["quality"]["internal"],data["quality"]["external"]])
            output=f.getvalue()
        if args.out:
            Path(args.out).parent.mkdir(parents=True,exist_ok=True)
            Path(args.out).write_text(output,encoding="utf-8")
            print("Exported:",args.out)
        else: sys.stdout.write(output)
        return 0
    if args.interval < 60:
        parser.error("--interval must be >= 60s to limit AWS query load")
    def run_once():
        snap, stats = cycle(args.db, profile=args.profile, env_file=args.env_file)
        print(json.dumps({"snapshot_id":snap["snapshot_id"],"computed_at":snap["computed_at"],
                          "ready_for_model":snap["ready_for_model"],"ready_for_automatic_action":False,
                          "quality_reasons":snap["quality_reasons"],"inserted":stats["inserted"],
                          "duplicates":stats["duplicates"],"source_errors":stats["errors"]},indent=2),flush=True)
    if args.command == "once":
        run_once(); return 0
    print("Watching telemetry (read-only); Ctrl+C to stop", flush=True)
    try:
        while True:
            before = time.monotonic()
            run_once()
            time.sleep(max(0, args.interval-(time.monotonic()-before)))
    except KeyboardInterrupt:
        print("Stopped.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
