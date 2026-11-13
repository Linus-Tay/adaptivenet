"use client";
// Real AdaptiveNet telemetry in plain language: live when the ingestion API runs,
// otherwise a recorded moment (always labelled with its own time).
import {useEffect,useState} from "react";
import RealExtras,{type Trend,type Check,type Feature} from "./RealExtras";

type Region={name:string;
  aws:{status:string;age_seconds:number|null;app_up:number|null;cpu_pct:number|null;memory_free_pct:number|null;p95_ms:number|null;error_ratio:number|null;missing:string[]};
  ripe:{status:string;age_seconds:number|null;rtt_ms:number|null;loss_pct:number|null;probes:number|null};
  http:{checks:number|null;failure_ratio:number|null;p95_ms:number|null;age_seconds:number|null}};
type Detector={condition:string;affected_region?:string|null;regions:Record<string,{state:string;reasons:string[]}>};
type Episode={id:string;label:string;region:string;scenario:string;start_unix:number;end_unix:number;reviewed:boolean};
type Overview={source:"live"|"recorded"|"none";message?:string;as_of_unix?:number;ready_for_model?:boolean;
  regions?:Record<string,Region>;cloudfront?:{status:string;metrics:Record<string,number|null>;http:{count?:number;failure_ratio?:number|null;http_latency_p95_ms?:number|null};http_age_seconds?:number|null};
  detectors?:Record<string,Detector>;recorded_range?:{first_unix:number;last_unix:number;count:number}|null;episodes?:Episode[];notes?:string[];
  trends?:Trend[];checks?:Check[];features?:Feature[];source_errors?:string[]};

const GROUPS=[["aws_only","AWS only"],["aws_ripe","AWS + RIPE"],["aws_ripe_http","AWS + RIPE + HTTP"]];
const LABEL:Record<string,string>={healthy:"Healthy",unknown:"Not enough data",network_degradation:"Degraded (external signals)",
  application_failure:"Application failure",compute_saturation:"Compute saturation",region_down:"Region down",multi_region_degradation:"Both regions degraded"};
const REASON:Record<string,string>={app_down:"app down",app_errors:"HTTP 5xx inside AWS",app_slow:"slow inside AWS",compute_saturated:"CPU saturated",
  path_loss:"packet loss on the internet path",path_latency:"high internet round-trip time",http_failures:"user HTTP checks failing",http_slow:"user HTTP checks slow"};
const tone=(c?:string)=>!c?"muted":c==="healthy"||c==="fresh"?"ok":c==="unknown"||c==="missing"?"muted":c==="network_degradation"||c==="stale"||c==="loss_only"?"warn":"bad";
const n=(v:number|null|undefined,d=0)=>v===null||v===undefined?"—":v.toFixed(d);
const ago=(s:number|null|undefined)=>s===null||s===undefined?"no data":s<90?`${Math.round(s)} s old`:s<5400?`${Math.round(s/60)} min old`:`${(s/3600).toFixed(1)} h old`;
const when=(t:number)=>new Date(t*1000).toLocaleString([], {day:"numeric",month:"short",hour:"2-digit",minute:"2-digit",second:"2-digit"});
function Pill({c,children}:{c?:string;children:React.ReactNode}){return <span className={"an-pill "+tone(c)}>{children}</span>;}

function Line({title,src,status,age,children}:{title:string;src:string;status:string;age:number|null|undefined;children:React.ReactNode}){
  return <div className="rt-line"><div className="rt-line-head"><b>{title}</b><small>{src}</small>
    <Pill c={status==="fresh"?"fresh":status==="missing"||status==="insufficient_or_stale"?"missing":"stale"}>{status==="fresh"?ago(age):status==="missing"?"missing":status.replaceAll("_"," ")}</Pill></div>
    <p>{children}</p></div>;
}

export default function RealTelemetry(){
  const [data,setData]=useState<Overview|null>(null);
  const [at,setAt]=useState<number|null>(null);
  async function load(t:number|null){
    try{const r=await fetch("/api/real"+(t?`?at=${t}`:""),{cache:"no-store"});setData(await r.json());}catch{setData({source:"none",message:"Dashboard API unreachable"});}
  }
  useEffect(()=>{void load(at);if(at!==null)return;const h=setInterval(()=>void load(null),5000);return()=>clearInterval(h);},[at]);
  if(!data)return <p className="muted">Loading real telemetry…</p>;
  if(data.source==="none")return <section className="operator-panel"><h2>Real AWS telemetry</h2><p>{data.message}</p>
    <p className="muted">Start the real pipeline with <code>scripts/live_monitor.py --sqs</code> (AWS sign-in needed), or set REAL_DATA_DIR in demo/.env.demo to a recorded data folder.</p></section>;

  const range=data.recorded_range;const det=data.detectors??{};
  const sentenceAws=(r:Region)=>r.aws.status!=="fresh"
    ?`No fresh AWS metrics at this moment${r.aws.missing.length?` (missing: ${r.aws.missing.join(", ")})`:""}. AWS-only monitoring is blind here.`
    :`The app is ${r.aws.app_up===1?"up":"NOT confirmed up"}. Inside AWS it answers in ${n(r.aws.p95_ms,1)} ms (p95) with ${r.aws.error_ratio===null?"unknown":`${n(r.aws.error_ratio*100,1)}%`} errors; CPU ${n(r.aws.cpu_pct)}%, memory free ${n(r.aws.memory_free_pct)}%.`;
  const sentenceRipe=(r:Region)=>r.ripe.probes?`${r.ripe.probes} probes around the world reach it in ${n(r.ripe.rtt_ms)} ms with ${n(r.ripe.loss_pct)}% packet loss.`:"No RIPE Atlas results in the freshness window.";
  const sentenceHttp=(r:Region)=>r.http.checks?`${r.http.checks} real HTTP checks in the last 5 min: ${n((r.http.failure_ratio??0)*100)}% failed, slowest 5% took over ${n(r.http.p95_ms)} ms.`:"No HTTP witness checks in the last 5 minutes.";

  return <>
    <section className="an-top rt-top">
      <div className="an-route">
        <div className="an-route-head"><b>{data.source==="live"?"LIVE from AWS":"RECORDED real data"}</b>
          <Pill c={data.source==="live"?"fresh":"stale"}>{data.source==="live"?"live":"as of "+when(data.as_of_unix!)}</Pill></div>
        <p className="an-msg">{data.source==="live"?"Values update every 5 s from the running ingestion API (:8766).":"The live pipeline is not running, so this shows a recorded real moment. Nothing here is current."}</p>
        {range&&<div className="rt-time">
          <input type="range" min={Math.floor(range.first_unix)} max={Math.ceil(range.last_unix)} step={60} value={Math.round(at??data.as_of_unix??range.last_unix)}
            onChange={e=>setAt(Number(e.target.value))} aria-label="Recorded time"/>
          <div className="an-row"><button className="ghost" onClick={()=>setAt((at??data.as_of_unix??range.last_unix)-300)}>−5 min</button>
            <button className="ghost" onClick={()=>setAt((at??data.as_of_unix??range.last_unix)+300)}>+5 min</button>
            <button className="ghost" onClick={()=>setAt(null)}>{data.source==="live"||at===null?"Latest":"Back to latest / live"}</button></div>
          <small className="muted">{range.count} recorded snapshots, {when(range.first_unix)} – {when(range.last_unix)}</small></div>}
      </div>
      <div className="an-controls"><b>Reviewed episodes</b><p className="an-msg">Moments a person verified from independent evidence. Jump in to see what each view noticed.</p>
        <div className="rt-episodes">{(data.episodes??[]).map(e=><button key={e.id} className="ghost" onClick={()=>setAt(Math.round((e.start_unix+e.end_unix)/2))}>
          <Pill c={e.label==="healthy"?"healthy":"application_failure"}>{e.label.replaceAll("_"," ")}</Pill>
          <span>{e.region==="region-a"?"Singapore":e.region==="region-b"?"Virginia":"both regions"} · {e.scenario?.replaceAll("_"," ")}</span><small>{when(e.start_unix)}</small></button>)}
          {!(data.episodes??[]).length&&<span className="muted">No verified_episodes.json in the data folder.</span>}</div></div>
    </section>

    <section className="an-groups">{GROUPS.map(([id,title])=>{const d=det[id];return <article key={id} className={"an-card "+tone(d?.condition)}>
      <header><h2>{title}</h2><Pill c={d?.condition}>{LABEL[d?.condition??""]??"…"}</Pill></header>
      {["region-a","region-b"].map(r=>{const v=d?.regions?.[r];return <div className="an-region" key={r}><span>{r==="region-a"?"Singapore":"Virginia"}</span>
        <Pill c={v?.state}>{LABEL[v?.state??""]??"—"}</Pill><small>{(v?.reasons??[]).map(x=>REASON[x]??x).join(", ")}</small></div>;})}
    </article>;})}</section>

    <section className="rt-regions">{["region-a","region-b"].map(id=>{const r=data.regions?.[id];if(!r)return null;
      return <article key={id} className="operator-panel"><div className="operator-panel-head"><h2>{r.name}</h2>
        <Pill c={det.aws_ripe_http?.regions?.[id]?.state}>{LABEL[det.aws_ripe_http?.regions?.[id]?.state??""]??"—"}</Pill></div>
        <Line title="Inside AWS" src="Amazon Managed Prometheus" status={r.aws.status} age={r.aws.age_seconds}>{sentenceAws(r)}</Line>
        <Line title="From the internet" src="RIPE Atlas ping (about every 15 min)" status={r.ripe.status} age={r.ripe.age_seconds}>{sentenceRipe(r)}</Line>
        <Line title="Real user requests" src="HTTP witness, one vantage point" status={r.http.age_seconds!==null&&r.http.age_seconds<=45?"fresh":r.http.checks?"stale":"missing"} age={r.http.age_seconds}>{sentenceHttp(r)}</Line>
      </article>;})}</section>

    {data.cloudfront&&<section className="operator-panel"><div className="operator-panel-head"><h2>CloudFront (all users, global)</h2><Pill c={data.cloudfront.status==="fresh"?"fresh":"stale"}>{data.cloudfront.status.replaceAll("_"," ")}</Pill></div>
      <p>CloudWatch counted {n(data.cloudfront.metrics.cloudfront_requests)} requests in the latest period, with {n(data.cloudfront.metrics.cloudfront_total_error_pct,1)}% errors (4xx {n(data.cloudfront.metrics.cloudfront_4xx_pct,1)}%, 5xx {n(data.cloudfront.metrics.cloudfront_5xx_pct,1)}%).
        {data.cloudfront.http?.count?` Our own checks through CloudFront: ${data.cloudfront.http.count} in 5 min, ${n((data.cloudfront.http.failure_ratio??0)*100)}% failed, p95 ${n(data.cloudfront.http.http_latency_p95_ms)} ms.`:""}</p></section>}

    <RealExtras trends={data.trends??[]} checks={data.checks??[]} features={data.features??[]} sourceErrors={data.source_errors??[]}
      quality={Object.values(data.regions??{}).map(r=>({name:r.name,aws:{status:r.aws.status,missing:r.aws.missing},ripe:{status:r.ripe.status,probes:r.ripe.probes}}))}/>
    <p className="muted">{(data.notes??[]).join(" ")} Verdicts use the same transparent per-group rules as the lab tab (not the trained models); they name which signal fired, not a verified label. “—” means missing, never zero.</p>
  </>;
}
