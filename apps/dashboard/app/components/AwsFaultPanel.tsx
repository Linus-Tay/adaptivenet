"use client";
// Real AWS SSM service-stop panel, unchanged from the original control room.
// Shown only when the dashboard runs with ADAPTIVENET_MODE=aws (scripts/live_monitor.py --with-ui).
import {useState} from "react";

const faultDescriptions:Record<string,string> = {
  "region-a":"Stops the allowlisted Singapore EC2 catalogue HTTP service for a bounded interval. A separate systemd recovery timer is created first. CloudFront may fail over to Virginia.",
  "region-b":"Stops the allowlisted Virginia EC2 catalogue HTTP service for a bounded interval. CloudFront normally uses Singapore, so CDN responses may not change.",
};

export function Info({description}:{description:string}){
  return <span className="operator-info" tabIndex={0} role="note" aria-label={description} title={description}>
    <span aria-hidden="true">ⓘ</span><span className="operator-tooltip">{description}</span>
  </span>;
}

export default function AwsFaultPanel(){
  const [busy,setBusy]=useState(false);
  const [region,setRegion]=useState<"region-a"|"region-b">("region-a");
  const [duration,setDuration]=useState(30);
  const [faultResult,setFaultResult]=useState("");
  async function injectRealFault(){
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
  return <section className="operator-panel">
    <div className="operator-panel-head"><h2>Real AWS fault injection</h2><span className="operator-badge live">Real AWS experiment</span></div>
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
      <button className="operator-danger" disabled={busy} onClick={()=>void injectRealFault()}>
        {busy?"Submitting…":"Inject HTTP service outage"}
      </button>
      <Info description={faultDescriptions[region]}/>
      <span className="operator-help">{faultDescriptions[region]}</span>
    </div>
    {faultResult&&<p role="status" className="operator-result">{faultResult}</p>}
    <p className="muted">Observe the direct origin, CloudFront&apos;s eventual backup origin, and the independent telemetry dashboard. RIPE ping can remain healthy during HTTP-only faults.</p>
  </section>;
}
