"use client";
import {useEffect, useRef, useState} from "react";

type Result = {source:string;status:number;elapsed_ms:number;origin:string|null;
  response?:{items?:Array<{id:string;name:string;price:number}>};error?:string;checked_at:string};
const sources=["cloudfront","region-a","region-b"] as const;
const labels:Record<string,[string,string]>={
  cloudfront:["CloudFront CDN","Primary Singapore · automatic Virginia origin failover"],
  "region-a":["Singapore EC2","Direct origin · bypasses CDN failover"],
  "region-b":["Virginia EC2","Direct origin · bypasses CDN failover"]
};

export default function Page(){
  const [rows,setRows]=useState<Record<string,Result>>({});
  const [events,setEvents]=useState<Result[]>([]);
  const [auto,setAuto]=useState(false);
  const [busy,setBusy]=useState(false);
  const active=useRef(false);
  async function sample(){
    if(active.current)return;
    active.current=true;setBusy(true);
    try{
      const next=await Promise.all(sources.map(async source=>{
        const begin=performance.now();
        try{
          const response=await fetch("/api/catalog?source="+source,{cache:"no-store",signal:AbortSignal.timeout(11000)});
          const body=await response.json();
          return {source,status:response.status,elapsed_ms:body.elapsed_ms??Math.round(performance.now()-begin),
            origin:body.origin??null,response:body.response,error:body.error,
            checked_at:new Date().toLocaleTimeString()} as Result;
        }catch{
          return {source,status:504,elapsed_ms:Math.round(performance.now()-begin),
            origin:null,error:"Client timeout",checked_at:new Date().toLocaleTimeString()} as Result;
        }
      }));
      setRows(Object.fromEntries(next.map(r=>[r.source,r])));
      setEvents(previous=>[...next,...previous].slice(0,30));
    }finally{active.current=false;setBusy(false)}
  }
  useEffect(()=>{if(!auto)return;void sample();const timer=setInterval(()=>void sample(),5000);return()=>clearInterval(timer)},[auto]);
  return <main className="workload">
    <header><small>ADAPTIVENET / WORKLOAD TEST</small><span className="mode">Viewer · No fault controls</span></header>
    <h1>Regional catalogue <span>resilience test.</span></h1>
    <p className="intro">Real HTTP responses through CloudFront and directly from each regional origin.
    This is intentionally a different page from the <a href="http://127.0.0.1:8766" target="_blank" rel="noreferrer">observability dashboard</a> and
    the <a href="http://127.0.0.1:3000" target="_blank" rel="noreferrer">experiment controller</a>.</p>
    <div className="actions">
      <button disabled={busy} onClick={()=>void sample()}>{busy?"Checking…":"Check all three endpoints"}</button>
      <button className="secondary" onClick={()=>setAuto(v=>!v)}>{auto?"Stop auto-checking":"Auto-check every 5 seconds"}</button>
      <small>Checks generate real HTTP traffic while enabled.</small>
    </div>
    <div className="workload-grid">{sources.map(source=>{
      const r=rows[source], good=r?.status===200;
      return <article className="workload-card" key={source}>
        <div className="card-head"><div><small>{source==="cloudfront"?"CDN":source==="region-a"?"SG":"US"} / HTTP</small>
          <h2>{labels[source][0]}</h2></div>
          <span className={"state "+(!r?"neutral":good?"good":"bad")}>{!r?"Not checked":("HTTP "+r.status)}</span></div>
        <p>{labels[source][1]}</p>
        <div className="stats"><div><small>Response time</small><strong>{r?(r.elapsed_ms+" ms"):"—"}</strong></div>
          <div><small>Served by</small><strong>{r?.origin??"—"}</strong></div></div>
        {r?.error&&<p className="error">{r.error}</p>}
        <footer>Last request: {r?.checked_at??"None"}</footer>
      </article>
    })}</div>
    <section className="catalogue"><h2>Live coffee catalogue</h2><p>These items come from your real CloudFront origin. The CDN panel above shows which region served them.</p><div className="catalogue-items">
      {(rows.cloudfront?.response?.items??[]).map(item=><article key={item.id}><span>{item.name}</span><strong>${typeof item.price==='number'?('
      <p>An EC2 application outage may produce an origin failure while CloudFront still returns 200 from Virginia. RIPE ICMP probes test network reachability, not the HTTP application.</p>
      {events.length===0?<div className="empty">Run a check to record actual HTTP observations.</div>:
        events.map((e,i)=><div className="event" key={i}><span>{e.checked_at}</span><b>{e.source}</b>
          <span className={e.status===200?"ok":"err"}>HTTP {e.status}</span><span>{e.elapsed_ms} ms</span>
          <span>Origin: {e.origin??"—"}</span></div>)}
    </section>
    <footer className="workload-footer">Public workload test page · No AWS configuration, routing or scaling actions here</footer>
  </main>;
}
+item.price.toFixed(2)):'—'}</strong></article>)}
      </div>{!rows.cloudfront?.response?.items?.length&&<p>Run a real HTTP check to load the catalogue.</p>}</section>
    <section className="timeline"><h2>Request timeline</h2>
      <p>An EC2 application outage may produce an origin failure while CloudFront still returns 200 from Virginia. RIPE ICMP probes test network reachability, not the HTTP application.</p>
      {events.length===0?<div className="empty">Run a check to record actual HTTP observations.</div>:
        events.map((e,i)=><div className="event" key={i}><span>{e.checked_at}</span><b>{e.source}</b>
          <span className={e.status===200?"ok":"err"}>HTTP {e.status}</span><span>{e.elapsed_ms} ms</span>
          <span>Origin: {e.origin??"—"}</span></div>)}
    </section>
    <footer className="workload-footer">Public workload test page · No AWS configuration, routing or scaling actions here</footer>
  </main>;
}
