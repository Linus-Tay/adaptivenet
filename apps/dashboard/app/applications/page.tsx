"use client";
import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";

type RegisteredApp = {
  id: string; name: string; mode: string; registration_status: string;
  endpoints: { endpoint_id: string; region: string; url: string }[];
  connections: Record<string, string>;
};
export default function Applications() {
  const [items, setItems] = useState<RegisteredApp[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("local");
  async function refresh() {
    const response = await fetch("/api/applications", { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error ?? "Cannot load applications");
    setItems(body.items);
  }
  useEffect(() => { refresh().catch(error => setMessage(String(error))); }, []);
  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setMessage("");
    const data = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/applications", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: data.get("name"), mode, endpoints: [
          { endpoint_id: "region-a", region: data.get("regionA"), url: data.get("urlA") },
          { endpoint_id: "region-b", region: data.get("regionB"), url: data.get("urlB") },
        ] }) });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : body.detail ? body.detail.map((d: {msg:string}) => d.msg).join("; ") : body.error ?? "Registration failed");
      await refresh(); setMessage("Application registered. Connect monitoring and routing before enabling protection.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Registration failed"); }
    finally { setBusy(false); }
  }
  return <main>
    <nav className="nav"><Link href="/">AdaptiveNet</Link><span>Applications</span><Link href="/">Experiment controls ↗</Link></nav>
    <small>APPLICATION REGISTRY</small><h1>Connect the application.<br/>Keep the regions in view.</h1>
    <p>Register your regional endpoints now. Monitoring and routing connections can be added as your deployment is ready.</p>
    <div className="app-grid">
      <section className="panel"><h2>Register an application</h2>
        <form onSubmit={register}>
          <label>Application name<input name="name" required maxLength={80} defaultValue="Coffee catalogue" /></label>
          <label>Deployment<select value={mode} onChange={event => setMode(event.target.value)}><option value="local">Local development</option><option value="aws">AWS deployment</option></select></label>
          <div key={mode}>
            <label>Region A<input name="regionA" required defaultValue={mode === "local" ? "region-a" : "ap-southeast-1"} /></label>
            <label>Endpoint A<input name="urlA" type="url" required defaultValue={mode === "local" ? "http://localhost:8101" : ""} placeholder="https://region-a.yourapp.com" /></label>
            <label>Region B<input name="regionB" required defaultValue={mode === "local" ? "region-b" : "us-east-1"} /></label>
            <label>Endpoint B<input name="urlB" type="url" required defaultValue={mode === "local" ? "http://localhost:8102" : ""} placeholder="https://region-b.yourapp.com" /></label>
          </div>
          <p className="muted">Registration saves configuration. It does not yet verify ownership, run network checks or enable mitigation.</p>
          <button disabled={busy} type="submit">{busy ? "Registering…" : "Register application"}</button>
        </form><p role="status">{message}</p>
      </section>
      <section><h2>Your applications <span className="badge">{items.length}</span></h2>
        {items.length === 0 && <div className="panel"><p>No applications registered yet. Start with your two local demo endpoints.</p></div>}
        {items.map(app => <article key={app.id} className="panel application-card">
          <div className="card-title"><h2>{app.name}</h2><span className="badge">{app.mode}</span></div>
          <p className="muted">Registered · verification pending</p>
          {app.endpoints.map(endpoint => <div key={endpoint.endpoint_id} className="endpoint"><strong>{endpoint.region}</strong><code>{endpoint.url}</code></div>)}
          <div className="connections">{Object.keys(app.connections).map(key => <span key={key}>{key.replaceAll("_", " ")} <b>Not connected</b></span>)}</div>
          <p className="muted">Automatic mitigation disabled</p>
        </article>)}
      </section>
    </div>
  </main>;
}
