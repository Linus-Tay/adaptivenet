"""Writes STATUS.txt and status.html (auto-refresh) for the RIPE trial campaign."""
import json, time, datetime as d, html, pathlib
C = pathlib.Path(__file__).parent
SGT = d.timezone(d.timedelta(hours=8))
def fmt(t): return t.astimezone(SGT).strftime('%a %I:%M %p SGT')
while True:
    now = d.datetime.now(d.timezone.utc)
    plan = json.loads((C / 'campaign-plan.json').read_text())
    log = {}
    if (C / 'campaign-log.jsonl').exists():
        for l in (C / 'campaign-log.jsonl').read_text().splitlines():
            if l.strip():
                r = json.loads(l); log[r['slot']] = r
    rows, done, failed, current, nxt = [], 0, 0, None, None
    for s in plan['slots']:
        st = d.datetime.fromisoformat(s['planned_start_utc']); end = st + d.timedelta(seconds=s['duration_seconds'])
        r = log.get(s['slot'])
        if r and r.get('status') == 'submitted' and r.get('check_returncode') == 0:
            state = 'running' if now < end + d.timedelta(minutes=2) else 'done'
            done += state == 'done'
            if state == 'running': current = s
        elif r:
            state = 'failed (' + r.get('status', '?') + ')'; failed += 1
        else:
            state = 'waiting'
            if nxt is None: nxt = s
        rows.append((s['slot'], 'Singapore' if s['region'] == 'region-a' else 'Virginia', s['loss_percent'], fmt(st), state))
    target = len(plan['slots'])
    last_end = d.datetime.fromisoformat(plan['slots'][-1]['planned_start_utc']) + d.timedelta(seconds=960)
    alive = (now - d.datetime.fromtimestamp((C / 'campaign.stdout').stat().st_mtime, d.timezone.utc)).total_seconds() < 4000
    bar = '#' * done + '-' * (target - done)
    head = (f"RIPE trial campaign: {done}/{target} trials completed   [{bar}]\n"
            f"Failed/skipped slots: {failed}\n"
            f"Now: {'TRIAL RUNNING - ' + ('Singapore' if current['region']=='region-a' else 'Virginia') + f' {current['loss_percent']}% loss' if current else 'between trials'}\n"
            f"Next trial: {(('Singapore' if nxt['region']=='region-a' else 'Virginia') + ' at ' + fmt(d.datetime.fromisoformat(nxt['planned_start_utc']))) if nxt else 'none'}\n"
            f"Expected finish: {fmt(last_end)}\n"
            f"Runner process alive: {'yes' if alive else 'CHECK'}   (updated {fmt(now)})\n")
    table = '\n'.join(f"{a:>2}  {b:<9} {c:>2}%  {e_:<26} {f}" for a, b, c, e_, f in rows)
    (C / 'STATUS.txt').write_text(head + '\n' + table + '\n')
    pct = int(100 * done / target)
    (C / 'status.html').write_text(f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=30>
<title>RIPE trial campaign</title><body style="font-family:-apple-system,sans-serif;max-width:760px;margin:24px auto;padding:0 16px">
<h2>RIPE trial campaign: {done}/{target} done</h2>
<div style="background:#ddd;border-radius:6px;height:22px"><div style="background:#2f7d4f;width:{pct}%;height:22px;border-radius:6px"></div></div>
<pre style="font-size:14px">{html.escape(head)}</pre><pre style="font-size:13px">{html.escape(table)}</pre>
<p style="color:#666">Refreshes every 30 s. Each trial: 16 min of packet loss, then automatic rollback.</p></body>""")
    time.sleep(30)
