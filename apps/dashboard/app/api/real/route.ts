// Read-only proxy to the real-telemetry overview (live ingestion API, or a recorded snapshot).
export const dynamic = "force-dynamic";
export async function GET(request: Request) {
  const ml = process.env.LAB_ML_URL;
  if (!ml) return Response.json({source: "none", message: "LAB_ML_URL not configured (start with demo/run.sh lab-up)"}, {status: 503});
  const at = new URL(request.url).searchParams.get("at");
  const query = at && /^\d{9,11}(\.\d+)?$/.test(at) ? `?at=${at}` : "";
  try {
    const r = await fetch(`${ml}/v1/real/overview${query}`, {cache: "no-store", signal: AbortSignal.timeout(10000)});
    return Response.json(await r.json(), {status: r.status});
  } catch {
    return Response.json({source: "none", message: "ML service unreachable"}, {status: 502});
  }
}
