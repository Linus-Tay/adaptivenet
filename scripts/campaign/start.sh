#!/bin/bash
# One-button start for the 20-trial RIPE campaign. Safe to re-run: refuses if a campaign is already running.
set -u
C=/Users/my/Project/adaptivenet-ripe-campaign
PY=$C/venv/bin/python
REPO=$C/code/repo
export PATH=~/bin:$PATH
export SSL_CERT_FILE=$($PY -c "import certifi;print(certifi.where())")
cd "$REPO"
echo "== AdaptiveNet RIPE campaign start =="
if pgrep -f "ripe_trial_campaign.py run" >/dev/null; then echo "A campaign is already running. Open $C/status.html"; open "$C/status.html"; exit 0; fi
echo "1/6 Checking internet..."; curl -s -m 10 -o /dev/null https://atlas.ripe.net/ || { echo "No internet. Connect and try again."; exit 1; }
echo "2/6 Fresh AWS sign-in (so the full ~8.5 h run fits in one session)."
echo "   A browser window will open: sign in as linus and click Allow."
"$HOME/aws-cli/aws" sso logout >/dev/null 2>&1 || true
"$HOME/aws-cli/aws" sso login --sso-session adaptivenet || { echo "AWS sign-in failed"; exit 1; }
timeout 60 aws sts get-caller-identity --profile adaptivenet-ml --output text >/dev/null || { echo "AWS still not reachable"; exit 1; }
for r in region-a region-b; do $PY scripts/native_network_fault_trial.py preflight --region $r --expected-account 468354413374 --ripe-measurement-id 236682050 >/dev/null 2>&1 || true; done
echo "3/6 Creating fresh RIPE measurements (10 h, every 4 min)..."
IDS=$($PY scripts/ripe_campaign_measurements.py --hours 10.5 --env-file /Users/my/Project/adaptivenet/.env --execute | sed -n 's/.*\[\(.*\)\].*/\1/p')
A=$(echo "$IDS" | cut -d, -f1 | tr -d ' '); B=$(echo "$IDS" | cut -d, -f2 | tr -d ' ')
[ -n "$A" ] && [ -n "$B" ] || { echo "Could not create RIPE measurements"; exit 1; }
echo "   Singapore $A, Virginia $B"
echo "4/6 Starting steady HTTP requests (every 15 s)..."
rm -f "$C/STOP"
CLOUDFRONT_DISTRIBUTION_ID=E3BJ0HVVT3JBMT nohup $PY scripts/continuous_probe.py --run --profile adaptivenet-ml --interval 15 --out "$C/http_witness.jsonl" > "$C/witness.stdout" 2>&1 &
echo "5/6 Planning 20 trials (first in 20 min, so RIPE and AWS metrics warm up)..."
START=$(date -u -v+20M +%Y-%m-%dT%H:%M:00+00:00)
$PY scripts/ripe_trial_campaign.py plan --seed 3104 --start-utc "$START" --cycles 10 --ripe-a $A --ripe-b $B --out "$C/campaign-plan.json"
$PY - <<PYEOF
import json; p=json.load(open("$C/campaign-plan.json")); p["stop_file"]="$C/STOP"; json.dump(p,open("$C/campaign-plan.json","w"),indent=2)
PYEOF
echo "6/6 Starting the trial runner and progress page..."
nohup caffeinate -i -s $PY -u scripts/ripe_trial_campaign.py run --plan "$C/campaign-plan.json" --profile adaptivenet-ml --expected-account 468354413374 --out-dir "$C" > "$C/campaign.stdout" 2>&1 &
nohup /usr/bin/python3 "$C/status.py" > "$C/status.log" 2>&1 &
nohup caffeinate -i -s -w $(pgrep -f "ripe_trial_campaign.py run" | head -1) >/dev/null 2>&1 &
sleep 4; open "$C/status.html"
echo; echo "Started. Keep the Mac plugged in, lid open, on the internet, for about 8.5 hours."
echo "Progress page: $C/status.html   To stop: double-click 'Stop RIPE Tests' on the Desktop."
