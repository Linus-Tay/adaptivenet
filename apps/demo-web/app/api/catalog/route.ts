/** Server-only proxy to fixed CloudFront or regional EC2 targets.
 * The source query is a closed enum; callers cannot choose arbitrary URLs.
 */
const targets: Record<string, string | undefined> = {
  cloudfront: process.env.DEMO_TARGET_URL ?? process.env.GATEWAY_URL ?? "http://127.0.0.1:8080",
  "region-a": process.env.DEMO_REGION_A_URL,
  "region-b": process.env.DEMO_REGION_B_URL,
};

export async function GET(request: Request) {
  const source = new URL(request.url).searchParams.get("source") ?? "cloudfront";
  if (!Object.prototype.hasOwnProperty.call(targets, source))
    return Response.json({error: "Invalid target"}, {status: 400});
  const target = targets[source];
  if (!target) return Response.json({error: source + " endpoint is not configured"}, {status: 503});
  let url: URL;
  try {
    url = new URL("/api/catalog", target);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password)
      throw new Error("Unsupported configured endpoint");
  } catch {
    return Response.json({error: "Invalid server-side target configuration"}, {status: 503});
  }
  const started = Date.now();
  try {
    const upstream = await fetch(url, {cache: "no-store", signal: AbortSignal.timeout(8000)});
    // CloudFront 502/503 error pages can be HTML. Preserve their real HTTP status.
    const body = await upstream.json().catch(() => ({}));
    return Response.json({source, status: upstream.status, elapsed_ms: Date.now()-started,
      origin: typeof body?.region === "string" ? body.region : null, response: body},
    {status: upstream.status, headers: {"Cache-Control": "no-store"}});
  } catch {
    return Response.json({source, status: 504, elapsed_ms: Date.now()-started,
      origin: null, error: "Upstream unavailable or timed out"},
    {status: 504, headers: {"Cache-Control": "no-store"}});
  }
}
