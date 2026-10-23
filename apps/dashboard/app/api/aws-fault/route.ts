import {execFile} from "node:child_process";
import {promisify} from "node:util";
import {resolve} from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const exec = promisify(execFile);

// The operator is run locally. There is no separate arming flag, login
// workflow, or confirmation challenge. The actual SSM command still targets
// only the fixed demo regions with a bounded duration and timed restoration.
let requestInProgress = false;

export async function POST(request: Request) {
  if (process.env.ADAPTIVENET_MODE !== "aws") {
    return Response.json({error: "Start the real AWS experiment with scripts/live_monitor.py --with-ui."}, {status: 503});
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json({error: "Invalid JSON"}, {status: 400});
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return Response.json({error: "Invalid experiment options"}, {status: 400});
  }

  const {region, duration} = body as Record<string, unknown>;
  if ((region !== "region-a" && region !== "region-b") ||
      ![10, 20, 30, 45, 60].includes(duration as number)) {
    return Response.json({error: "Choose Singapore or Virginia and a supported duration."}, {status: 400});
  }

  if (requestInProgress) {
    return Response.json({error: "An experiment is already being submitted."}, {status: 409});
  }

  const root = process.env.ADAPTIVENET_REPO_ROOT;
  const python = process.env.ADAPTIVENET_PYTHON;
  const account = process.env.ADAPTIVENET_EXPECTED_AWS_ACCOUNT;
  if (!root || !python || !account) {
    return Response.json({error: "Missing local AWS experiment configuration."}, {status: 503});
  }

  requestInProgress = true;
  try {
    const script = resolve(root, "scripts", "aws_fault.py");
    // CLI flags are internal implementation details. Pressing Inject is the
    // complete user action; there is no extra dialog or opt-in command.
    const confirm = region === "region-a" ? "STOP-REGION-A" : "STOP-REGION-B";
    const args = [
      script, "--region", region, "--duration", String(duration),
      "--profile", process.env.AWS_PROFILE || "adaptivenet-admin",
      "--expected-account", account, "--execute", "--confirm", confirm
    ];
    const {stdout} = await exec(python, args, {
      cwd: root, timeout: 90_000, maxBuffer: 16_384, shell: false,
      env: process.env
    });
    const commandId = stdout.match(/SSM accepted request:\s*([a-f0-9-]{36})/i)?.[1] ?? null;
    return Response.json({
      submitted: true,
      region,
      duration_seconds: duration,
      command_id: commandId,
      status: "submitted"
    }, {status: 202, headers: {"Cache-Control": "no-store"}});
  } catch {
    return Response.json({
      error: "Could not submit the EC2 fault. Check your AWS SSO session, SSM connectivity and target instance."
    }, {status: 502});
  } finally {
    requestInProgress = false;
  }
}
