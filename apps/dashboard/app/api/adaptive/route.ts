// Read-only proxy to the AdaptiveNet lab decision service (ML -> safety review -> decision).
// POST only supports the local demo reset: clear lab faults, route 100% to Singapore,
// and ask the lab controller for a fresh action journal. Never touches AWS.
export const dynamic = "force-dynamic";
const view = process.env.LAB_VIEW_URL;
const control = process.env.CONTROL_API_URL ?? "http://127.0.0.1:8000";
const runner = process.env.EXPERIMENT_URL ?? "http://127.0.0.1:8001";

async function call(url: string, method = "GET", body?: unknown) {
  const token = process.env.LAB_TOKEN ?? "";
  const r = await fetch(url, {method, cache: "no-store", signal: AbortSignal.timeout(8000),
    headers: {"X-Lab-Token": token, "Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)});
  return {ok: r.ok, status: r.status, data: await r.json().catch(() => ({}))};
}

export async function GET() {
  if (!view) return Response.json({error: "LAB_VIEW_URL not configured (start with demo/run.sh lab-up)"}, {status: 503});
  try {
    const r = await call(`${view}/api/state`);
    return Response.json(r.data, {status: r.status});
  } catch {
    return Response.json({error: "AdaptiveNet decision service unreachable"}, {status: 502});
  }
}

export async function POST(request: Request) {
  if (process.env.ADAPTIVENET_MODE === "aws") return Response.json({error: "Local demo only"}, {status: 403});
  const origin = request.headers.get("origin");
  if (origin && new URL(origin).host !== request.headers.get("host")) return Response.json({error: "Invalid origin"}, {status: 403});
  let action = "";
  try { action = (await request.json()).action; } catch { return Response.json({error: "Invalid JSON"}, {status: 400}); }
  if (action !== "reset-demo" || !view) return Response.json({error: "Unknown action"}, {status: 400});
  try {
    const steps = [
      await call(`${runner}/experiments`, "DELETE"),
      await call(`${control}/state`),
    ];
    const current = (steps[1].data as {region_a_percent?: number}).region_a_percent;
    if (typeof current === "number" && current !== 100) {
      steps.push(await call(`${control}/routes`, "PUT", {region_a_percent: 100, expected_region_a_percent: current}));
    }
    steps.push(await call(`${view}/api/reset`, "POST"));
    const ok = steps.every(s => s.ok);
    return Response.json({status: ok ? "reset" : "partial", steps: steps.map(s => s.status)}, {status: ok ? 200 : 502});
  } catch {
    return Response.json({error: "Reset failed; use demo/run.sh lab-reset"}, {status: 502});
  }
}
