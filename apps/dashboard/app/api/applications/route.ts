async function forward(method: string, body?: unknown) {
  const token = process.env.LAB_TOKEN;
  if (!token) return Response.json({ error: "LAB_TOKEN is not configured" }, { status: 503 });
  try {
    const response = await fetch(`${process.env.CONTROL_API_URL ?? "http://127.0.0.1:8000"}/applications`, {
      method, cache: "no-store", signal: AbortSignal.timeout(6000),
      headers: { "X-Lab-Token": token, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return Response.json(await response.json(), { status: response.status });
  } catch { return Response.json({ error: "Control API unreachable" }, { status: 502 }); }
}
export async function GET() { return forward("GET"); }
export async function POST(request: Request) {
  if (!request.headers.get("content-type")?.startsWith("application/json"))
    return Response.json({ error: "JSON required" }, { status: 415 });
  const origin = request.headers.get("origin");
  if (origin && new URL(origin).host !== request.headers.get("host"))
    return Response.json({ error: "Invalid origin" }, { status: 403 });
  try { return forward("POST", await request.json()); }
  catch { return Response.json({ error: "Invalid JSON" }, { status: 400 }); }
}
