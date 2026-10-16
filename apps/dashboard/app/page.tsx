"use client";
import {useEffect,useState} from "react";

type State = {mode?:string;scope?:string;error?:string;[key:string]:unknown};
const commands=[
  ["route-a","Route to Singapore","Local gateway, 100% region A"],
  ["latency","Inject +1.5s latency","Local region A for 30 seconds"],
  ["failure","Inject HTTP 503","Local region A for 30 seconds"],
  ["route-b","Route to Virginia","Local gateway, 100% region B"],
  ["reset","Clear simulated faults","Reset local gateway faults"]
];
export default function Page(){
  const [mode,setMode]=useState("checking");
  const [state,setState]=useState<State>({});
  const [message,setMessage]=useState("Connecting…");
  const [busy,setBusy]=useState(false);
  async function refresh(){
    try{
      const r=await fetch("/api/lab",{cache:"no-store"}),data=await r.json();
      if(data.mode==="aws"){setMode("aws");setMessage("Real AWS mode · CLI approval required");}
      else{setMode("local");setMessage(r.ok?"Local simulator connected":data.error??"Local control unavailable");}
      setState(data);
    }catch{setMessage("Control API unreachable");}
  }
  useEffect(()=>{void refresh();const handle=setInterval(()=>void refresh(),5000);return()=>clearInterval(handle)},[]);
  async function action(id:string){
    setBusy(true);
    try{
      const r=await fetch("/api/lab",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:id})});
      const data=await r.json();setMessage(r.ok?"Local simulator updated":data.error??"Action failed");
      await refresh();
    }catch{setMessage("Control request failed")}finally{setBusy(false);}
  }
  const aws=mode==="aws";
  return <main className="operator">
    <nav className="nav"><a href="/applications">Applications & endpoints ↗</a></nav>
    <header className="operator-header"><small>ADAPTIVENET / EXPERIMENT CONTROLS</small><span className={"operator-badge "+(aws?"live":"")}>{aws?"REAL AWS · GUARDED":"LOCAL SIMULATOR"}</span></header>
    <h1>Resilience experiment <span>control room.</span></h1>
    <p className="operator-intro">A separate control surface. This is not the public workload test page or the read-only telemetry dashboard.</p>
    <div className="operator-links"><a href="http://127.0.0.1:8766" target="_blank" rel="noreferrer">Observability dashboard ↗<small>Real CloudFront + AWS Prometheus + RIPE · port 8766</small></a>
    <a href="http://127.0.0.1:3001" target="_blank" rel="noreferrer">Public workload test ↗<small>CloudFront and both origins · port 3001</small></a></div>
    {aws?<section className="operator-panel">
      <div className="operator-panel-head"><h2>Real EC2 fault experiments</h2><span className="operator-badge live">CLI-gated</span></div>
      <p>For live AWS, browser mutation controls are disabled. Use the allowlisted SSM command, which checks the AWS account, exact demo EC2 instance, and tags before running. The EC2 service-restart timer is scheduled <strong>before</strong> the fault.</p>
      <div className="operator-warning">A real application outage can affect CloudFront viewers. Run only during an authorized experiment, and inspect SSM command status plus service recovery afterwards.</div>
      <h3>Preview (no AWS writes)</h3>
      <pre>python scripts/aws_fault.py --region region-a --duration 30 --expected-account YOUR_12_DIGIT_ACCOUNT</pre>
      <h3>Execute with explicit approval</h3>
      <pre>python scripts/aws_fault.py --region region-a --duration 30 --expected-account YOUR_12_DIGIT_ACCOUNT --execute --confirm STOP-REGION-A</pre>
      <p>CloudFront can respond from Virginia when Singapore's HTTP service fails. RIPE ICMP may continue normally because this fault does not disable networking.</p>
    </section>:<section className="operator-panel"><div className="operator-panel-head"><h2>Local simulation only</h2><span className="operator-badge">No AWS effects</span></div>
      <p>These buttons change only the local gateway, not EC2, CloudFront or real Prometheus telemetry.</p>
      <div className="operator-grid">{commands.map(([id,label,description])=><button key={id} disabled={busy} onClick={()=>void action(id)}><b>{label}</b><small>{description}</small></button>)}</div>
    </section>}
    <section className="operator-panel"><h2>Operator status</h2><p role="status">{message}</p><pre>{JSON.stringify(state,null,2)}</pre></section>
    <footer className="operator-footer">Fault control is independent of model prediction and observation. No automatic mitigation is enabled.</footer>
  </main>;
}
