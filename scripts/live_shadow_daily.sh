#!/bin/bash
# Daily live-shadow job (SHADOW ONLY, read-only towards AWS). Run by launchd each morning.
#  1. download the PUBLIC RIPE results of the live measurements (no credentials)
#  2. try a READ-ONLY AMP export for the whole run (needs a valid SSO sign-in; skipped if not)
#  3. write a snapshot of alert onsets per Singapore day (current model + frozen rules)
#  4. keep the HTTP witness running until the stop time
#  5. after the stop time, once AMP is complete: read-only SSM exclusions, then the
#     pre-registered retrain (ml/team_starter/live_shadow.py LIVE_PROTOCOL), exactly once
set -u
C=/Users/my/Project/adaptivenet-ripe-campaign
L=$C/live
W=$C/work
PY=$C/venv/bin/python
export PATH=$HOME/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PYTHONPATH=$W/services:$W
export SSL_CERT_FILE=$($PY -c "import certifi;print(certifi.where())")
cd "$W" || exit 1
exec >>"$L/daily.log" 2>&1
echo "=== $(date -u +%FT%TZ) daily job"

meta() { $PY -c "import json,sys;print(json.load(open('$L/live.json'))[sys.argv[1]])" "$1"; }
START=$(meta start_epoch); STOP=$(meta stop_epoch); A=$(meta msm_region_a); B=$(meta msm_region_b)
NOW=$(date -u +%s); END=$(( NOW < STOP ? NOW : STOP ))

# 1. RIPE (public, read-only)
for pair in "region-a:$A" "region-b:$B"; do
  r=${pair%%:*}; m=${pair##*:}
  $PY -c "from ml.team_starter.ripe_value import fetch; import sys; from pathlib import Path; print('$r ripe rows', fetch($m, $START, $END, Path('$L/ripe-$r.json.tmp')))" \
    && mv "$L/ripe-$r.json.tmp" "$L/ripe-$r.json" || echo "RIPE fetch failed for $r (kept previous file)"
done

# 2. AMP (read-only; aligned to the 30 s grid)
S=$(( (START / 30) * 30 )); ok=1
for r in region-a region-b; do
  if timeout 900 $PY -m ml.team_starter.amp_export --region $r --start $S --end $END --step 30 \
       --profile adaptivenet-ml --out "$L/amp-$r.jsonl.tmp"; then
    mv "$L/amp-$r.jsonl.tmp" "$L/amp-$r.jsonl"
  else ok=0; rm -f "$L/amp-$r.jsonl.tmp"; fi
done
if [ $ok = 1 ]; then echo "ok through $(date -u -r $END +%FT%TZ)" > "$L/aws_status.txt"
else echo "sign-in needed (AWS columns kept from the last successful export)" > "$L/aws_status.txt"; fi
cat "$L/aws_status.txt"

# 3. snapshot
$PY -m ml.team_starter.live_shadow snapshot --campaign-dir "$C" --live-dir "$L" > /dev/null && echo "snapshot written" || echo "snapshot FAILED"

# 4. witness
if [ "$NOW" -lt "$STOP" ] && ! pgrep -f "continuous_probe.py --run" >/dev/null; then
  echo "witness not running; restarting"; "$L/start_witness.sh"
fi

# 5. Wednesday: exclusions + retrain, once, only with a complete AMP export
if [ "$NOW" -ge "$STOP" ] && [ $ok = 1 ] && [ ! -f "$L/RETRAIN.md" ]; then
  $PY -m ml.team_starter.live_shadow ssm-exclusions --campaign-dir "$C" --live-dir "$L" --profile adaptivenet-ml \
    && $PY -m ml.team_starter.live_shadow retrain --campaign-dir "$C" --live-dir "$L" > /dev/null \
    && echo "retrain written: $L/RETRAIN.md" || echo "retrain FAILED"
fi
if [ "$NOW" -ge "$STOP" ] && [ -f "$L/RETRAIN.md" ]; then
  launchctl bootout gui/$(id -u)/com.adaptivenet.liveshadow 2>/dev/null; echo "run finished; daily job removed"
fi
