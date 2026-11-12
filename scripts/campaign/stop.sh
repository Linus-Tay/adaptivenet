#!/bin/bash
C=/Users/my/Project/adaptivenet-ripe-campaign
touch "$C/STOP"; pkill -f "ripe_trial_campaign.py run"; pkill -f "continuous_probe.py --run"; pkill -f "$C/status.py"
echo "Stopped. No new faults will start. Any fault already running removes itself within 16 minutes (automatic rollback on the server)."
