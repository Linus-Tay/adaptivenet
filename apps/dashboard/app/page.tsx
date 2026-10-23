"use client";
import {useEffect,useState} from "react";

type State = {mode?:string;scope?:string;error?:string;fault_ui_enabled?:boolean;[key:string]:unknown};
const commands = [
  {id:"route-a",title:"Route to Singapore",help:"LOCAL SIMULATOR ONLY. Sets all simulated requests to region A. Does not alter CloudFront, EC2 or Route 53."},
  {id:"latency",title:"Inject +1.5 s latency",help:"LOCAL SIMULATOR ONLY. Delays region A simulated requests by 1500 ms for 30 seconds. It does not affect AWS Prometheus or RIPE probes."},
  {id:"failure",title:"Inject HTTP 503",help:"LOCAL SIMULATOR ONLY. Returns HTTP 503 for region A simulated traffic for 30 seconds. CloudFront is unaffected."},
  {id:"route-b",title:"Route to Virginia",help:"LOCAL SIMULATOR ONLY. Sends simulated traffic to region B. Does not modify real CloudFront origins."},
  {id:"reset",title:"Clear simulated faults",help:"LOCAL SIMULATOR ONLY. Clears injected local faults immediately. The real AWS instance is not touched."},
];
const faultDescriptions:Record<string,string> = {
  "region-a":"Stops the allowlisted Singapore EC2 catalogue HTTP service for a bounded interval. A separate systemd recovery timer is created first. CloudFront may fail over to Virginia.",
  "region-b":"Stops the allowlisted Virginia EC2 catalogue HTTP service for a bounded interval. CloudFront normally uses Singapore, so CDN responses may not change.",
};

function Info({description}:{description:string}){
  return <span className="operator-info" tabIndex={0} role="note" aria-label={description} title={description}>
    <span aria-hidden="true">ⓘ</span><span className="operator-tooltip">{description}</span>
  </span>;
}

export default function Page(){
  const [mode,setMode]=useState("checking");
  const [state,setState]=useState<State>({});
  const [message,setMessage]=useState("Connecting…");
  const [busy,setBusy]=useState(false);
  const [region,setRegion]=useState<"region-a"|"region-b">("region-a");
  const [duration,setDuration]=useState(30);
  const [faultResult,setFaultResult]=useState("");
  const aws=mode==="aws";
  const faultEnabled=aws&&state.fault_ui_enabled===true;

  async function refresh(){
    try{
      const response=await fetch("/api/lab",{cache:"no-store"});
      const data=await response.json() as State;
      setMode(data.mode==="aws"?"aws":"local");
      setState(data);
      if(!response.ok)setMessage(data.error??"Local controller unavailable");
    }catch{setMessage("Control API unreachable");}
  }
  useEffect(()=>{void refresh();const handle=setInterval(()=>void refresh(),5000);return()=>clearInterval(handle)},[]);
  async function localAction(id:string){
    setBusy(true);
    try{
      const response=await fetch("/api/lab",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:id})});
      const data=await response.json();
      await refresh();
      setMessage(response.ok?"Local simulator updated":data.error??"Action failed");
    }catch{setMessage("Control request failed")}finally{setBusy(false);}
  }
  async function injectRealFault(){
    if(!faultEnabled)return;
    const phrase=region==="region-a"?"STOP-REGION-A":"STOP-REGION-B";
    const label=region==="region-a"?"SINGAPORE":"VIRGINIA";
    const confirmed=window.confirm("REAL AWS SERVICE OUTAGE\n\n"+label+" EC2 demo catalogue will be stopped for "+duration+
      " seconds. A recovery timer will be scheduled first.\n\nCloudFront visitors can be affected. Continue only with experiment approval.");
    if(!confirmed)return;
    setBusy(true);setFaultResult("");
    try{
      const response=await fetch("/api/aws-fault",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({region,duration,confirm:phrase}),cache:"no-store"});
      const data=await response.json();
      if(!response.ok){setFaultResult(data.error??"SSM request failed");return;}
      setFaultResult("SSM request submitted (NOT execution/recovery verified). "+
        "Command ID: "+(data.command_id??"check AWS SSM console")+
        ". Confirm origin outage and recovery in the other two windows.");
    }catch{setFaultResult("Network error: SSM request outcome unknown. Check AWS SSM before retrying.");}
    finally{setBusy(false);}
  }

  return <main className="operator">
    <nav className="nav"><a href="/applications">Applications & endpoints ↗</a></nav>
    <header className="operator-header"><small>ADAPTIVENET / EXPERIMENT CONTROLS</small>
      <span className={"operator-badge "+(aws?"live":"")}>{aws?"REAL AWS · GUARDED":"LOCAL SIMULATOR"}</span></header>
    <h1>Resilience experiment <span>control room.</span></h1>
    <p className="operator-intro">Operator controls are separate from the public catalogue and read-only observability. Actions here must never be mistaken for measurements.</p>
    <div className="operator-links">
      <a href="http://127.0.0.1:8766" target="_blank" rel="noreferrer">Observability dashboard ↗<small>Prometheus + RIPE + CloudFront + HTTP witness · :8766</small></a>
      <a href="http://127.0.0.1:3001" target="_blank" rel="noreferrer">Workload test ↗<small>CloudFront and both EC2 origins · :3001</small></a>
    </div>
    {aws?<section className="operator-panel">
      <div className="operator-panel-head"><h2>Real AWS fault injection</h2><span className="operator-badge live">{faultEnabled?"Manual button armed":"Button disabled by default"}</span></div>
      <p>Select an exact approved demo region. Only the <strong>HTTP application-stop fault</strong> is implemented against real EC2. It does not alter Route 53, CloudFront origin configuration or Kubernetes.</p>
      <div className="operator-warning">This can briefly interrupt a real EC2 service. It requires your AWS SSO credentials, exact instance/tag/SSM Online checks, a localhost-only operator, explicit confirmation, and a 90-second request cooldown. SSM acceptance alone does not prove recovery.</div>
      <div className="fault-fields">
        <label>Target region
          <select value={region} onChange={e=>setRegion(e.target.value as "region-a"|"region-b")} disabled={busy}>
            <option value="region-a">Singapore (region-a)</option><option value="region-b">Virginia (region-b)</option>
          </select>
        </label>
        <label>Fault duration
          <select value={duration} onChange={e=>setDuration(Number(e.target.value))} disabled={busy}>
            {[10,20,30,45,60].map(n=><option value={n} key={n}>{n} seconds</option>)}
          </select>
        </label>
      </div>
      <div className="fault-button-row">
        <button className="operator-danger" disabled={busy||!faultEnabled} onClick={()=>void injectRealFault()}>
          {busy?"Submitting…":"Inject HTTP service outage"}
        </button>
        <Info description={faultDescriptions[region]}/>
        <span className="operator-help">{faultDescriptions[region]}</span>
      </div>
      <div className="fault-button-row">
        <button disabled title="Not implemented for real AWS. The existing latency button is a local-only simulator." type="button">Inject network latency (not wired)</button>
        <Info description="No real AWS network-impairment agent is deployed. Local simulator latency does NOT affect CloudFront, Prometheus or RIPE Atlas."/>
        <button disabled title="No real AWS packet-loss fault runner is deployed." type="button">Inject packet loss (not wired)</button>
        <Info description="RIPE Atlas measures packet loss, but no tested bounded network-layer fault injector is currently deployed to EC2."/>
      </div>
      {!faultEnabled&&<p className="muted">To enable the real fault button on this Mac only, restart <code>scripts/live_monitor.py</code> with <code>--enable-real-fault-ui</code>. Monitoring and the workload work without this flag.</p>}
      {faultResult&&<p role="status" className="operator-result">{faultResult}</p>}
      <p className="muted">Observe the direct origin, CloudFront's eventual backup origin, and the independent telemetry dashboard. RIPE ping can remain healthy during HTTP-only faults.</p>
    </section>:<section className="operator-panel">
      <div className="operator-panel-head"><h2>Local simulation only</h2><span className="operator-badge">No AWS effects</span></div>
      <p>These buttons change only the local gateway; they do not affect EC2, CloudFront, AWS Prometheus or RIPE measurements.</p>
      <div className="operator-grid">{commands.map(item=><div className="operator-action" key={item.id}>
        <button disabled={busy} onClick={()=>void localAction(item.id)}><b>{item.title}</b></button>
        <Info description={item.help}/>
      </div>)}</div>
    </section>}
    <section className="operator-panel"><h2>Operator status</h2><p role="status">{message}</p><pre>{JSON.stringify(state,null,2)}</pre></section>
    <footer className="operator-footer">Observability is read-only. No trained model, automatic mitigation, EKS scaling or Route 53 action is enabled by this page.</footer>
  </main>;
}
