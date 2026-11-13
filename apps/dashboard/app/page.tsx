"use client";
import {useEffect,useRef,useState} from "react";
import AwsFaultPanel from "./components/AwsFaultPanel";
import RealTelemetry from "./components/RealTelemetry";

/* ---------- types (subset of the decision service /api/state) ---------- */
type RegionView={state:string;reasons:string[]};
type Detector={condition:string;affected_region?:string|null;regions:Record<string,RegionView>};
type Step={state?:string;reasons?:string[];qualifying_count?:number};
type Decision={at:number;state?:string;condition?:string;decision?:string;reasons?:string[];region_a_percent?:number;
  all_groups?:Record<string,string>;proposal?:{action?:string;target_region_a_percent?:number;affected_region?:string};
  plan?:Record<string,unknown>|null;executed_locally?:{step?:Step;verify?:Step;state?:string;reason?:string}|null};
type Features=Record<string,number|null>;
type Snapshot={regions:Record<string,{features:Features;quality:{internal:string;external:string}}>};
type Adaptive={error?:string;assessment?:{detectors:Record<string,Detector>;snapshot_age_seconds?:number;ready_for_shadow_assessment?:boolean}|null;
  snapshot?:Snapshot|null;
  fast?:{scopes:Record<string,{windows:Record<string,{failure_ratio:number|null;http_latency_p95_ms:number|null}>}>};
  decisions?:Decision[]};
type Fault={region:string;latency_ms:number;error_rate:number;duration_seconds:number;remaining_seconds:number};
type Lab={mode?:string;error?:string;region_a_percent?:number;faults?:Record<string,Fault>};
type FaultWindow={key:string;region:string;kind:string;start:number;end:number};

const GROUPS=[
  {id:"aws_only",title:"AWS only",sees:"Prometheus / CloudWatch: app health, CPU, memory, HTTP errors and latency measured inside the region"},
  {id:"aws_ripe",title:"AWS + RIPE Atlas",sees:"…plus external RTT and packet loss towards each region"},
  {id:"aws_ripe_http",title:"AWS + RIPE + HTTP",sees:"…plus the 5-second HTTP witness on the user path"},
];
const REGION_NAME:Record<string,string>={"region-a":"Singapore","region-b":"Virginia"};
const LABEL:Record<string,string>={healthy:"Healthy",unknown:"Not enough data",network_degradation:"Network degradation",
  application_failure:"Application failure",compute_saturation:"Compute saturation",region_down:"Region down",
  multi_region_degradation:"Both regions degraded"};
const REASON:Record<string,string>={app_down:"app down",app_errors:"HTTP 5xx",app_slow:"slow responses (internal)",
  compute_saturated:"CPU saturated",path_loss:"packet loss",path_latency:"high RTT",http_failures:"HTTP failures (witness)",
  http_slow:"slow HTTP (witness)"};
const ACTION:Record<string,string>={hold:"Do nothing",investigate:"Look into it",shift_traffic:"Move 25% of traffic",scale_out:"Add a server"};
const DECIDED:Record<string,string>={"no-op":"No action",failback:"Move traffic back to Singapore",failover:"Approved: move traffic",scale:"Approved: add server (plan only)"};
const CTRL:Record<string,string>={idle:"Idle",observing:"Watching",qualifying:"Confirming",verifying:"Checking customers",
  continue_candidate:"Better, next step",recovered:"Customers recovered",blocked:"Stopped by a safety rule",cooldown:"Cooling down",
  action_pending:"Applying",disabled:"Off",waiting_healthy:"Waiting: Singapore must stay healthy",
  failback_step_ok:"Moved back, customers fine",rolled_back:"Undone: Singapore not ready",
  confirming_signals:"Confirming: 2 signals must agree"};
const WHY:Record<string,string>={primary_must_stay_healthy:"Singapore looks healthy; waiting to be sure",no_action_requested:"nothing wrong",degradation_evidence_missing_or_stale:"not confirmed by its own check yet",
  reviewer_eligible_shift:"passed every safety check",source_still_missing_deadline:"some customers still slow",
  client_window_met_deadline:"all customer requests fast",telemetry_quality_gate_failed:"data too incomplete to act",
  destination_not_confirmed_healthy:"other region not confirmed healthy",route_already_at_limit:"no traffic left to move",
  recent_recovery:"waiting 2 min after the last fix",target_observed:"route change confirmed",insufficient_recovery_evidence:"collecting customer requests",
  route_changed_since_verification:"someone changed the route by hand (press Reset demo)",fresh_destination_probe_required:"checking the other region first"};
// Confidence-rule reasons carry numbers, e.g. "bad_windows:2/3" (services/adaptivenet/controller/confidence.py).
const CONF:[RegExp,(m:RegExpMatchArray)=>string][]=[
  [/^bad_windows:(\d+)\/(\d+)$/,m=>`${m[1]} of ${m[2]} bad checks in a row`],
  [/^signals_disagree:(\d+)\/(\d+)_bad\((.*)\)$/,m=>`only ${m[3]==="none"?"no signal":m[3].replace("+"," + ")} says bad; ${m[2]} must agree`],
  [/^primary_healthy:(\d+)\/(\d+)s$/,m=>`Singapore healthy for ${m[1]} of ${m[2]} s`],
  [/^cooldown_reversal:(\d+)\/(\d+)s$/,m=>`no reversing within ${m[2]} s (${m[1]} s so far)`],
  [/^confident:(.*)_bad_for_(\d+)_windows$/,m=>`${m[1].replace("+"," + ")} agree for ${m[2]} checks`],
];
const why=(r?:string)=>{if(!r)return undefined;for(const[re,f]of CONF){const m=r.match(re);if(m)return f(m);}
  return WHY[r]??r.replaceAll("_"," ");};
const tone=(c?:string)=>!c?"muted":c==="healthy"?"ok":c==="unknown"?"muted":c==="network_degradation"||c==="compute_saturation"?"warn":"bad";
const fmt=(v:number|null|undefined,d=0)=>v===null||v===undefined?"—":v.toFixed(d);
const clock=(t:number)=>new Date(t*1000).toLocaleTimeString();

function Pill({c,children}:{c?:string;children:React.ReactNode}){return <span className={"an-pill "+tone(c)}>{children}</span>;}

export default function Page(){
  const [lab,setLab]=useState<Lab>({});
  const [ad,setAd]=useState<Adaptive>({});
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState("");
  const [duration,setDuration]=useState(120);
  const [target,setTarget]=useState<"region-a"|"region-b">("region-a");
  const [fw,setFw]=useState<FaultWindow|null>(null);
  const [tab,setTab]=useState<"lab"|"real">("lab");
  const firstSeen=useRef<Record<string,Record<string,number>>>({});

  async function refresh(){
    try{const r=await fetch("/api/lab",{cache:"no-store"});setLab(await r.json());}catch{setLab({error:"Control API unreachable"});}
    try{const r=await fetch("/api/adaptive",{cache:"no-store"});setAd(await r.json());}catch{setAd({error:"Decision service unreachable"});}
  }
  useEffect(()=>{void refresh();const h=setInterval(()=>void refresh(),2000);return()=>clearInterval(h);},[]);

  const aws=lab.mode==="aws";
  const active=Object.values(lab.faults??{})[0];
  /* Remember the latest fault window so detection times stay visible after it expires. */
  useEffect(()=>{
    if(!active)return;
    const start=Math.round(Date.now()/1000-(active.duration_seconds-active.remaining_seconds));
    const kind=active.error_rate>0?`HTTP ${Math.round(active.error_rate*100)}% errors`:`+${active.latency_ms} ms latency`;
    const key=`${active.region}:${kind}:${Math.round(start/10)}`;
    setFw(prev=>prev&&prev.key===key?prev:{key,region:active.region,kind,start,end:start+active.duration_seconds});
  },[active?.region,active?.latency_ms,active?.error_rate,active?.duration_seconds,active?.remaining_seconds]);

  const decisions=ad.decisions??[];
  if(fw){
    const seen=firstSeen.current[fw.key]??(firstSeen.current[fw.key]={});
    for(const d of [...decisions].reverse()){
      if(d.at<fw.start||!d.all_groups)continue;
      for(const g of GROUPS){const c=d.all_groups[g.id];if(c&&c!=="healthy"&&c!=="unknown"&&seen[g.id]===undefined)seen[g.id]=d.at-fw.start;}
      if(seen.shift===undefined&&d.region_a_percent!==undefined&&d.region_a_percent!==100)seen.shift=d.at-fw.start;
    }
  }
  const seen=fw?firstSeen.current[fw.key]??{}:{};
  const dets=ad.assessment?.detectors??{};
  const latest=decisions.find(d=>d.decision||d.state==="demo_reset");
  const weight=lab.region_a_percent??latest?.region_a_percent;
  const disagree=dets.aws_only?.condition==="healthy"&&GROUPS.slice(1).some(g=>{const c=dets[g.id]?.condition;return !!c&&c!=="healthy"&&c!=="unknown";});

  async function act(path:string,body:Record<string,unknown>,ok:string){
    setBusy(true);setMessage("");
    try{const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const d=await r.json();setMessage(r.ok?ok:(d.error??"Action failed"));}
    catch{setMessage("Request failed");}finally{setBusy(false);void refresh();}
  }

  if(aws)return <main className="operator">
    <header className="operator-header"><small>ADAPTIVENET / REAL AWS</small><span className="operator-badge live">REAL AWS · LOCAL</span></header>
    <h1>Resilience experiment <span>control room.</span></h1>
    <AwsFaultPanel/>
  </main>;

  const ex=latest?.executed_locally;const step=ex?.verify??ex?.step;
  const stage:{k:string;v:string;c?:string;sub?:string}[]=[
    {k:"Data in",v:ad.assessment?`${fmt(ad.assessment.snapshot_age_seconds)} s old`:"waiting",c:ad.assessment?.ready_for_shadow_assessment?"healthy":"unknown"},
    {k:"Detector sees (all signals)",v:LABEL[dets.aws_ripe_http?.condition??""]??"—",c:dets.aws_ripe_http?.condition,
      sub:dets.aws_ripe_http?.affected_region?REGION_NAME[dets.aws_ripe_http.affected_region]:undefined},
    {k:"Detector suggests",v:ACTION[latest?.proposal?.action??""]??"—",c:latest?.proposal?.action==="hold"?"healthy":latest?.proposal?.action?"network_degradation":undefined,
      sub:latest?.proposal?.target_region_a_percent!==undefined?`→ ${latest.proposal.target_region_a_percent}% Singapore`:undefined},
    {k:"Safety review",v:DECIDED[latest?.decision??""]??"—",c:latest?.decision==="no-op"?"healthy":latest?.decision?"network_degradation":undefined,
      sub:latest?.decision==="no-op"?why((latest.reasons??[])[0]):latest?.decision==="scale"?"shown as a plan, never executed here":why((latest?.reasons??[])[0])},
    {k:"Controller",v:CTRL[step?.state??ex?.state??"idle"]??(step?.state??"idle"),c:step?.state==="recovered"?"healthy":step?.state==="blocked"?"application_failure":step?.state?"network_degradation":"healthy",
      sub:step?.state==="qualifying"?`${step.qualifying_count} of 3 checks agree`:why((step?.reasons??[])[0])},
  ];
  const snap=ad.snapshot;const win=(s:string)=>ad.fast?.scopes?.[s]?.windows?.["60s"];
  const rows=(items:[string,string,number][])=>items.map(([l,k,d])=>
    <tr key={k}><td>{l}</td>{["region-a","region-b"].map(r=><td key={r}>{fmt(snap?.regions?.[r]?.features?.[k],d)}</td>)}</tr>);

  return <main className="operator an">
    <nav className="nav"><a href="/applications">Applications & endpoints ↗</a></nav>
    <header className="operator-header"><small>ADAPTIVENET</small>
      <div className="rt-tabs" role="tablist">
        <button role="tab" aria-selected={tab==="lab"} className={tab==="lab"?"on":""} onClick={()=>setTab("lab")}>Live lab demo</button>
        <button role="tab" aria-selected={tab==="real"} className={tab==="real"?"on":""} onClick={()=>setTab("real")}>Real AWS telemetry</button>
      </div></header>
    <h1>{tab==="lab"?<>Can AWS-only monitoring <span>see this failure?</span></>:<>What is happening <span>in Singapore and Virginia?</span></>}</h1>
    {tab==="real"?<RealTelemetry/>:<>
    <span className="operator-badge">LOCAL SIMULATOR · NO AWS EFFECTS</span>
    <ol className="an-steps"><li><b>Break something.</b> Pick a region and inject a fault below.</li>
      <li><b>Compare the three views.</b> Does monitoring that only sees AWS notice? Do the internet probes?</li>
      <li><b>Watch the response.</b> Traffic moves away step by step, the <a href="http://127.0.0.1:3001" target="_blank" rel="noreferrer">customer view</a> recovers, and once Singapore is healthy again traffic returns to it.</li></ol>

    <section className="an-top">
      <div className="an-route">
        <div className="an-route-head"><b>Where customers are sent</b><span>{weight===undefined?"?":`${weight}% Singapore · ${100-weight}% Virginia`}</span></div>
        <div className="an-bar"><i style={{width:`${weight??100}%`}}/></div>
        <div className="an-fault">{active?<><Pill c="application_failure">FAULT ACTIVE</Pill> {REGION_NAME[active.region]} · {active.error_rate>0?`HTTP ${Math.round(active.error_rate*100)}% errors`:`+${active.latency_ms} ms latency`} · {Math.round(active.remaining_seconds)} s left</>
          :<><Pill c="healthy">NO FAULT</Pill> everything normal</>}</div>
      </div>
      <div className="an-controls">
        <div className="an-row">
          <label>Region<select value={target} onChange={e=>setTarget(e.target.value as "region-a"|"region-b")}><option value="region-a">Singapore</option><option value="region-b">Virginia</option></select></label>
          <label>Duration<select value={duration} onChange={e=>setDuration(Number(e.target.value))}>{[30,60,120].map(n=><option key={n} value={n}>{n} s</option>)}</select></label>
        </div>
        <div className="an-row">
          <button disabled={busy} onClick={()=>void act("/api/lab",{action:"latency",region:target,duration},"Latency fault injected (+1500 ms on the gateway path)")}>Inject +1.5 s latency</button>
          <button disabled={busy} onClick={()=>void act("/api/lab",{action:"failure",region:target,duration},"HTTP 503 fault injected")}>Inject HTTP 503</button>
        </div>
        <div className="an-row">
          <button className="ghost" disabled={busy} onClick={()=>void act("/api/lab",{action:"reset"},"Faults cleared")}>Clear faults</button>
          <button className="ghost" disabled={busy} onClick={()=>void act("/api/lab",{action:"route-a"},"All traffic to Singapore")}>Route → Singapore</button>
          <button className="ghost" disabled={busy} onClick={()=>void act("/api/adaptive",{action:"reset-demo"},"Demo reset: faults cleared, 100% Singapore, fresh controller journal")}>Reset demo</button>
        </div>
        <p className="an-msg" role="status">{message||"Faults only affect this local lab. Latency = slow network path; HTTP 503 = requests fail."}</p>
      </div>
    </section>

    {ad.error&&<p className="operator-warning">{ad.error}</p>}
    {disagree&&<div className="an-callout">AWS-only says <b>healthy</b>, but the external signals see a problem. This is the gray failure AdaptiveNet is built to catch.</div>}

    <section className="an-groups">
      {GROUPS.map(g=>{const d=dets[g.id];return <article key={g.id} className={"an-card "+tone(d?.condition)}>
        <header><h2>{g.title}</h2><Pill c={d?.condition}>{LABEL[d?.condition??""]??"…"}</Pill></header>
        <p className="an-sees">{g.sees}</p>
        {["region-a","region-b"].map(r=>{const v=d?.regions?.[r];return <div className="an-region" key={r}>
          <span>{REGION_NAME[r]}</span><Pill c={v?.state}>{LABEL[v?.state??""]??"—"}</Pill>
          <small>{(v?.reasons??[]).map(x=>REASON[x]??x).join(", ")}</small></div>;})}
        <footer>{fw?(seen[g.id]!==undefined?<>Detected <b>{Math.round(seen[g.id])} s</b> after the fault started</>:<>Not detected during this fault</>):<>Inject a fault to time detection</>}</footer>
      </article>;})}
    </section>

    <section className="operator-panel">
      <div className="operator-panel-head"><h2>What AdaptiveNet is doing about it</h2>
        <span className="operator-badge">{fw?`last fault: ${REGION_NAME[fw.region]} ${fw.kind} at ${clock(fw.start)}${seen.shift!==undefined?` · first traffic shift after ${Math.round(seen.shift)} s`:""}`:"no fault yet"}</span></div>
      <div className="an-flow">{stage.map((s,i)=><div key={s.k} className="an-stage">
        <small>{i+1}. {s.k}</small><Pill c={s.c}>{s.v}</Pill>{s.sub&&<span>{s.sub}</span>}</div>)}</div>
      <p className="muted">The rule detector only suggests (the trained ML models are shadow-only and not used here). An independent safety reviewer must approve, then the controller confirms the problem with three of its own checks before moving 25% of traffic, and checks real customer requests before moving more. Adding servers on EKS is shown as a plan only.</p>
    </section>

    <section className="an-two">
      <div className="operator-panel"><h2>The raw numbers</h2>
        <table className="an-table"><thead><tr><th/><th>Singapore</th><th>Virginia</th></tr></thead><tbody>
          <tr className="sec"><td colSpan={3}>Inside AWS (what AWS-only sees)</td></tr>
          {rows([["App up","app_up",0],["CPU %","cpu_busy_pct",1],["HTTP p95 ms","http_p95_ms",1],["5xx ratio","http_error_ratio",2]])}
          <tr className="sec"><td colSpan={3}>From the internet (RIPE-style probes)</td></tr>
          {rows([["RTT p50 ms","network_rtt_p50_ms",0],["Packet loss %","packet_loss_pct",0],["Probes","ripe_probes_reporting",0]])}
          <tr className="sec"><td colSpan={3}>Real requests (last 60 s)</td></tr>
          <tr><td>Failure ratio</td>{["region-a","region-b"].map(r=><td key={r}>{fmt(win(r)?.failure_ratio,2)}</td>)}</tr>
          <tr><td>p95 ms</td>{["region-a","region-b"].map(r=><td key={r}>{fmt(win(r)?.http_latency_p95_ms)}</td>)}</tr>
        </tbody></table>
        <p className="muted">Lab stand-ins measured from local containers, not real AWS or RIPE data. “—” means missing, never zero.</p>
      </div>
      <div className="operator-panel"><h2>What happened, step by step</h2>
        <div className="an-log">{decisions.filter(d=>d.decision||d.state==="demo_reset").slice(0,14).map(d=>{const s=d.executed_locally?.verify??d.executed_locally?.step;
          return <div key={d.at} className="an-logrow"><time>{clock(d.at)}</time>
            {d.state==="demo_reset"?<b>demo reset</b>:<>
            <span className="an-mini">{GROUPS.map(g=><i key={g.id} className={tone(d.all_groups?.[g.id])} title={`${g.title}: ${d.all_groups?.[g.id]}`}/>)}</span>
            <Pill c={d.decision==="no-op"?"healthy":"network_degradation"}>{DECIDED[d.decision??""]??d.decision}</Pill>
            <span>{d.region_a_percent}% to Singapore{s?.state?` · ${CTRL[s.state]??s.state}${s.state==="qualifying"?` (${s.qualifying_count}/3)`:""}`:""}</span></>}</div>;})}</div>
        <p className="muted">The three dots are AWS only · AWS + RIPE · AWS + RIPE + HTTP at that moment (green fine, amber/red problem).</p>
      </div>
    </section>

    </>}
    <div className="operator-links">
      <a href="http://127.0.0.1:3001" target="_blank" rel="noreferrer">Workload site ↗<small>The catalogue users see, through the lab gateway · :3001</small></a>
      <a href="http://127.0.0.1:8766" target="_blank" rel="noreferrer">Real AWS observability ↗<small>Prometheus + RIPE + CloudFront + HTTP witness · :8766 (when the real pipeline runs)</small></a>
    </div>
    <footer className="operator-footer">Local lab only. Lab rule detectors per feature group (not the trained models); AWS-only cannot read RIPE or HTTP inputs. Nothing on this page changes AWS, CloudFront, EKS or Route 53.</footer>
  </main>;
}
