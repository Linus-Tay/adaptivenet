import {execFile} from "node:child_process";
import {promisify} from "node:util";
import {resolve} from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const exec = promisify(execFile);
const enabled = () => process.env.ADAPTIVENET_MODE === "aws" &&
  process.env.ENABLE_REAL_AWS_FAULT_UI === "true";

let requestInProgress = false;
let lastSubmittedAt = 0;

function localOriginOnly(request: Request): boolean {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  const site = request.headers.get("sec-fetch-site");
  return host === "127.0.0.1:3000" && origin === "http://127.0.0.1:3000" &&
    (site === "same-origin" || site === "none" || site === null);
}
function jsonError(message: string, code: number) {
  return Response.json({error: message}, {status: code, headers: {"Cache-Control": "no-store"}});
}

export async function GET() {
  return Response.json({
    mode: "aws", enabled: enabled(), duration_options: [10, 20, 30, 45, 60],
    fault_type: "application_stop", scope: "pre-approved AdaptiveNet demo EC2 only",
    note: "SSM command acceptance is not execution or recovery confirmation."
  }, {headers: {"Cache-Control": "no-store"}});
}

export async function POST(request: Request) {
  if (!enabled()) return jsonError("Real AWS fault UI disabled. Start live_monitor.py with --enable-real-fault-ui.", 403);
  if (!localOriginOnly(request)) return jsonError("Only same-origin local operator actions are accepted.", 403);
  if (!request.headers.get("content-type")?.startsWith("application/json"))
    return jsonError("JSON required", 415);
  let body: unknown;
  try {body = await request.json();} catch {return jsonError("Invalid JSON", 400);}
  if (typeof body !== "object" || body === null || Array.isArray(body))
    return jsonError("Invalid fault request", 400);
  const item = body as Record<string, unknown>;
  const region = item.region;
  const duration = item.duration;
  if ((region !== "region-a" && region !== "region-b") ||
      ![10, 20, 30, 45, 60].includes(duration as number) ||
      item.confirm !== (region === "region-a" ? "STOP-REGION-A" : "STOP-REGION-B"))
    return jsonError("Invalid region, duration, or explicit confirmation.", 400);
  if (requestInProgress) return jsonError("Another SSM fault request is running.", 409);
  if (Date.now() - lastSubmittedAt < 90_000)
    return jsonError("90-second safety cooldown after the previous request.", 429);

  const root = process.env.ADAPTIVENET_REPO_ROOT;
  const python = process.env.ADAPTIVENET_PYTHON;
  const expected = process.env.ADAPTIVENET_EXPECTED_AWS_ACCOUNT;
  if (!root || !python || !expected || !/^\d{12}$/.test(expected))
    return jsonError("Server-side Python, repository, or expected AWS account not configured.", 503);
  requestInProgress = true;
  try {
    const script = resolve(root, "scripts", "aws_fault.py");
    const args = [script, "--region", region as string, "--duration", String(duration),
      "--profile", "adaptivenet-admin", "--expected-account", expected,
      "--execute", "--confirm", item.confirm as string];
    const {stdout} = await exec(python, args, {cwd: root, timeout: 90_000, maxBuffer: 16384,
      shell: false, env: {...process.env, AWS_PROFILE: "adaptivenet-admin"}});
    lastSubmittedAt = Date.now();
    const id = stdout.match(/SSM accepted request:\s*([a-f0-9-]{36})/i)?.[1] ?? null;
    return Response.json({submitted: true, region, duration_seconds: duration,
      command_id: id, status: "submitted_not_verified",
      note: "Check SSM invocation status and independently verify restoration."},
      {status: 202, headers: {"Cache-Control": "no-store"}});
  } catch {
    return jsonError("SSM fault not submitted or failed. Check terminal/SSM permissions and instance status.", 502);
  } finally {
    requestInProgress = false;
  }
}
