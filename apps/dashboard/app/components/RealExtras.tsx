"use client";
// The rest of the former :8766 observatory, in plain language: trends, recent real
// HTTP checks, data-quality gates and every raw feature. Read-only.
import {useState} from "react";

export type Trend={t:number;[k:string]:number|null};
export type Check={timestamp:string;scope:string;http_status:number|null;latency_ms:number|null;origin:string|null;outcome:string};
export type Feature={scope:string;name:string;value:number|null;unit:string;source:string;age_seconds:number|null};
type Quality={name:string;aws:{status:string;missing:string[]};ripe:{status:string;probes:number|null}};

const METRICS:[string,string,string][]=[
  ["network_rtt_p50_ms","Internet round-trip time (RIPE)","ms"],["packet_loss_pct","Packet loss (RIPE)","%"],
  ["http_p95_ms","Response time inside AWS (p95)","ms"],["cpu_busy_pct","CPU busy","%"],
  ["memory_available_pct","Memory free","%"],["http_error_ratio","Error ratio inside AWS","ratio"]];
const SCOPE:Record<string,string>={cloudfront:"CloudFront","region-a":"Singapore","region-b":"Virginia"};
const LABEL:Record<string,string>={app_up:"App is up",http_rps:"Requests per second",http_5xx_rps:"Server errors per second",
  http_p95_ms:"Response time p95",cpu_busy_pct:"CPU busy",memory_available_bytes:"Memory free",memory_total_bytes:"Memory total",
  node_load1:"Load average",memory_available_pct:"Memory free",http_error_ratio:"Error ratio",ripe_probes_reporting:"RIPE probes reporting",
  network_rtt_p50_ms:"Internet RTT (median)",network_rtt_p95_ms:"Internet RTT (p95)",packet_loss_pct:"Packet loss (mean)",
  packet_loss_median_pct:"Packet loss (median probe)",ripe_probes_full_loss:"Probes with 100% loss",cpu_busy_ewma_pct:"CPU (smoothed)",
  http_p95_ewma_ms:"Response time (smoothed)",http_p95_robust_z:"Response-time anomaly score",cloudfront_requests:"Requests",
  cloudfront_4xx_pct:"4xx error rate",cloudfront_5xx_pct:"5xx error rate",cloudfront_total_error_pct:"Total error rate",
  cloudfront_bytes_downloaded:"Bytes sent to users",cloudfront_bytes_uploaded:"Bytes received from users"};
const WHY_MISSING:Record<string,string>={network_rtt_p50_ms:"no RIPE measurement is running",network_rtt_p95_ms:"no RIPE measurement is running",
  packet_loss_pct:"no RIPE measurement is running",packet_loss_median_pct:"no RIPE measurement is running",
  http_p95_robust_z:"needs at least 5 earlier samples",http_error_ratio:"no requests in the window"};
const ago=(s:number|null)=>s===null||s===undefined?"—":s<90?`${Math.round(s)} s`:s<5400?`${Math.round(s/60)} min`:`${(s/3600).toFixed(1)} h`;
const num=(v:number|null,u:string)=>v===null||v===undefined?null:u==="bytes"?`${(v/1e6).toFixed(1)} MB`:Math.abs(v)>=100?v.toFixed(0):v.toFixed(2);

function Chart({trends,metric,unit}:{trends:Trend[];metric:string;unit:string}){
  const W=760,H=200,P=36;
  const pts=(r:string)=>trends.map(t=>[t.t,t[`${r}.${metric}`]] as [number,number|null]);
  const all=["region-a","region-b"].flatMap(r=>pts(r).map(p=>p[1]).filter((v):v is number=>v!==null));
  if(trends.length<2||!all.length)return <p className="muted">No history for this metric yet. Missing values are gaps, never zeros.</p>;
  const t0=trends[0].t,t1=trends[trends.length-1].t,lo=Math.min(0,...all),hi=Math.max(...all)*1.1||1;
  const x=(t:number)=>P+(W-2*P)*((t-t0)/Math.max(1,t1-t0)),y=(v:number)=>H-P+(-(H-2*P))*((v-lo)/(hi-lo));
  const path=(r:string)=>{let d="",pen=false;for(const [t,v] of pts(r)){if(v===null){pen=false;continue;}d+=`${pen?"L":"M"}${x(t).toFixed(1)},${y(v).toFixed(1)} `;pen=true;}return d;};
  const tick=(t:number)=>new Date(t*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  return <svg viewBox={`0 0 ${W} ${H}`} className="rx-chart" role="img" aria-label={`${metric} over time`}>
    {[lo,(lo+hi)/2,hi].map(v=><g key={v}><line x1={P} x2={W-P} y1={y(v)} y2={y(v)} className="grid"/><text x={4} y={y(v)+4}>{v.toFixed(v<10?1:0)}{unit==="%"?"%":""}</text></g>)}
    <text x={P} y={H-8}>{tick(t0)}</text><text x={W-P} y={H-8} textAnchor="end">{tick(t1)}</text>
    <path d={path("region-a")} className="sg"/><path d={path("region-b")} className="us"/>
  </svg>;
}

export default function RealExtras({trends,checks,features,quality,sourceErrors}:{trends:Trend[];checks:Check[];features:Feature[];quality:Quality[];sourceErrors:string[]}){
  const [metric,setMetric]=useState(METRICS[2][0]);
  const [q,setQ]=useState("");
  const m=METRICS.find(x=>x[0]===metric)!;
  const shown=features.filter(f=>!q||`${f.scope} ${f.name} ${LABEL[f.name]??""}`.toLowerCase().includes(q.toLowerCase()));
  return <>
    <section className="operator-panel"><div className="operator-panel-head"><h2>How it changed over time</h2>
      <select value={metric} onChange={e=>setMetric(e.target.value)} className="rx-select">{METRICS.map(([k,l])=><option key={k} value={k}>{l}</option>)}</select></div>
      <Chart trends={trends} metric={metric} unit={m[2]}/>
      <div className="rx-legend"><span><i className="sg"/>Singapore</span><span><i className="us"/>Virginia</span><small>{trends.length} snapshots · gaps mean the value was missing</small></div>
    </section>

    <section className="an-two">
      <div className="operator-panel"><h2>Latest real HTTP checks</h2>
        <p className="muted">Real requests from this Mac to CloudFront and directly to each origin. One vantage point: it cannot prove a global outage.</p>
        <div className="an-log">{checks.length?checks.map((c,i)=><div key={i} className="an-logrow">
          <time>{new Date(c.timestamp).toLocaleTimeString()}</time><b>{SCOPE[c.scope]??c.scope}</b>
          <span className={"an-pill "+(c.outcome==="ok"?(c.latency_ms??0)>800?"warn":"ok":"bad")}>{c.outcome==="ok"?`HTTP ${c.http_status}`:c.outcome.replaceAll("_"," ")}</span>
          <span>{c.latency_ms===null?"—":`${Math.round(c.latency_ms)} ms`}{c.scope==="cloudfront"&&c.origin?` · served by ${SCOPE[c.origin]??c.origin}`:""}</span></div>)
          :<span className="muted">No HTTP checks recorded in this window.</span>}</div></div>
      <div className="operator-panel"><h2>Can we trust the data right now?</h2>
        {quality.map(r=><div key={r.name} className="rt-line">
          <div className="rt-line-head"><b>{r.name}</b></div>
          <p>AWS metrics: <span className={"an-pill "+(r.aws.status==="fresh"?"ok":"bad")}>{r.aws.status==="fresh"?"fresh":"missing"}</span>{r.aws.missing.length?` (missing ${r.aws.missing.join(", ")})`:""}
            {" · "}RIPE: <span className={"an-pill "+(r.ripe.status==="fresh"?"ok":r.ripe.status==="loss_only"?"warn":"muted")}>{r.ripe.status==="fresh"?"fresh":r.ripe.status==="loss_only"?"loss only":"not enough probes"}</span> {r.ripe.probes??0} of at least 3 probes</p></div>)}
        <p className="muted">{sourceErrors.length?`Source problems in this snapshot: ${sourceErrors.join(", ")}.`:"No source errors in this snapshot."} AdaptiveNet only acts when the data it needs is fresh.</p>
      </div>
    </section>

    <section className="operator-panel"><div className="operator-panel-head"><h2>Every raw value</h2>
      <input className="rx-search" placeholder="Search, e.g. memory or Virginia" value={q} onChange={e=>setQ(e.target.value)}/></div>
      <table className="an-table"><thead><tr><th>Where</th><th>What</th><th>Value</th><th>Source</th><th>Age</th></tr></thead><tbody>
        {shown.map(f=>{const v=num(f.value,f.unit);return <tr key={f.scope+f.name}><td>{f.scope}</td><td>{LABEL[f.name]??f.name}<br/><small className="muted">{f.name}</small></td>
          <td>{v===null?<span className="muted">missing{WHY_MISSING[f.name]?` (${WHY_MISSING[f.name]})`:""}</span>:`${v}${f.unit&&f.unit!=="bytes"&&f.unit!=="0/1"?" "+f.unit:""}`}</td>
          <td>{f.source}</td><td>{ago(f.age_seconds)}</td></tr>;})}
      </tbody></table>
      <p className="muted">{shown.length} of {features.length} values · missing is never shown as zero.</p>
    </section>
  </>;
}
