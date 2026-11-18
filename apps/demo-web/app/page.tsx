"use client";
import {useEffect, useRef, useState} from "react";

/* The demo coffee shop: a customer website tab and a behind-the-scenes tab. Every number is a real HTTP request. */
type Result = {source:string;status:number;elapsed_ms:number;origin:string|null;
  response?:{items?:Array<{id:string;name:string;price:number}>};error?:string;at:number};
const sources=["cloudfront","region-a","region-b"] as const;
const REGION:Record<string,string>={"region-a":"Singapore","region-b":"Virginia"};
const SLOW_MS=800;
const verdict=(r?:Result)=>!r?"waiting":r.status!==200?"failed":r.elapsed_ms>SLOW_MS?"slow":"fast";
const WORD:Record<string,string>={waiting:"Checking…",failed:"Failed",slow:"Slow",fast:"Fast"};
const price=(p:number)=>"$"+p.toFixed(2);
const BLURB:Record<string,string>={coffee:"Our house espresso blend, pulled fresh.",tea:"Loose-leaf black or green, steeped to order."};

export default function Page(){
  const [rows,setRows]=useState<Record<string,Result>>({});
  const [events,setEvents]=useState<Result[]>([]);
  const [auto,setAuto]=useState(true);
  const [tab,setTab]=useState<"shop"|"behind">("shop");
  const active=useRef(false);
  async function sample(){
    if(active.current)return;
    active.current=true;
    try{
      const next=await Promise.all(sources.map(async source=>{
        const begin=performance.now();
        try{
          const response=await fetch("/api/catalog?source="+source,{cache:"no-store",signal:AbortSignal.timeout(11000)});
          const body=await response.json();
          return {source,status:response.status,elapsed_ms:body.elapsed_ms??Math.round(performance.now()-begin),
            origin:body.origin??null,response:body.response,error:body.error,at:Date.now()} as Result;
        }catch{
          return {source,status:504,elapsed_ms:Math.round(performance.now()-begin),origin:null,error:"No answer within 11 s",at:Date.now()} as Result;
        }
      }));
      setRows(Object.fromEntries(next.map(r=>[r.source,r])));
      setEvents(previous=>[next[0],...previous].slice(0,40));
    }finally{active.current=false}
  }
  useEffect(()=>{if(!auto)return;void sample();const timer=setInterval(()=>void sample(),3000);return()=>clearInterval(timer)},[auto]);

  const user=rows.cloudfront;const v=verdict(user);
  const last=events.filter(e=>Date.now()-e.at<=60000);
  const bad=last.filter(e=>verdict(e)!=="fast").length;
  const byRegion=(r:string)=>last.filter(e=>e.origin===r).length;
  const directOk=(["region-a","region-b"] as const).every(s=>verdict(rows[s])==="fast");
  const story=!user?"Loading the shop…":
    v==="fast"?`Customers are fine: the shop loaded in ${user.elapsed_ms} ms, served from ${REGION[user.origin??""]??"an unknown region"}.`:
    v==="slow"?`Customers are waiting: the shop took ${(user.elapsed_ms/1000).toFixed(1)} s to load (served from ${REGION[user.origin??""]??"?"}).`:
    `Customers see an error: the shop did not load (HTTP ${user.status}).`;
  const hint=user&&v!=="fast"&&directOk?"Both regions answer quickly when asked directly, so the servers are fine. The problem is on the path users take. This is a gray failure: AWS's own health checks would still say everything is healthy.":
    user&&v!=="fast"?"At least one region is also failing when asked directly, so this is a server problem, not only the path.":"";

  const behindView=<main className="workload shop">
    <header><small>HARBOUR LANE COFFEE · BEHIND THE SCENES</small>
      <span className="mode">Every number is a real request to this site · <a href="http://127.0.0.1:3000">Control room ↗</a></span></header>
    <h1>Is the shop working <span>for customers right now?</span></h1>

    <section className={"hero "+v}>
      <div className="big">{WORD[v]}</div>
      <div><p className="story">{story}</p>{hint&&<p className="hint">{hint}</p>}</div>
    </section>

    <div className="actions">
      <button onClick={()=>setAuto(a=>!a)}>{auto?"Pause live checks":"Resume live checks (every 3 s)"}</button>
      <button className="secondary" onClick={()=>void sample()}>Check once now</button>
      <small>A page load counts as slow above {SLOW_MS} ms.</small>
    </div>

    <section className="timeline"><h2>Last minute of page loads</h2>
      <p>Each bar is one real customer page load, newest on the left. Height is load time; colour is who served it.</p>
      {events.length===0?<div className="empty">Waiting for the first page load…</div>:<>
        <div className="bars">{events.map((e,i)=><i key={i} title={`${new Date(e.at).toLocaleTimeString()} · ${e.status} · ${e.elapsed_ms} ms · ${REGION[e.origin??""]??"no answer"}`}
          className={(e.status!==200?"fail ":"")+(e.origin==="region-b"?"us":"sg")} style={{height:`${Math.max(6,Math.min(100,e.elapsed_ms/20))}%`}}/>)}</div>
        <div className="legend"><span><i className="sg"/>Singapore</span><span><i className="us"/>Virginia</span><span><i className="fail"/>Failed</span>
          <b>{last.length?`${Math.round(100*bad/last.length)}% slow or failed`:""} · {byRegion("region-a")} from Singapore, {byRegion("region-b")} from Virginia</b></div></>}
    </section>

    <section className="catalogue">
      <h2>Today&apos;s menu</h2>
      <p>This is the actual response the customer received.</p>
      <div className="catalogue-items">
        {(rows.cloudfront?.response?.items??[]).map(item=>
          <article key={item.id}><span>{item.name}</span>
            <strong>{typeof item.price==="number" ? price(item.price) : "—"}</strong>
          </article>)}
      </div>
      {!rows.cloudfront?.response?.items?.length&&<p>{user?"The menu did not load for this customer.":"Loading…"}</p>}
    </section>

    <h2 className="behind">Behind the scenes: each region asked directly</h2>
    <p className="intro">These skip the routing layer the customer uses. If they are fast while customers are slow, the servers are healthy and the network path is the problem.</p>
    <div className="workload-grid two">{(["region-a","region-b"] as const).map(source=>{
      const r=rows[source],s=verdict(r);
      return <article className="workload-card" key={source}>
        <div className="card-head"><h2>{REGION[source]}</h2><span className={"state "+(s==="fast"?"good":s==="waiting"?"neutral":"bad")}>{WORD[s]}</span></div>
        <div className="stats"><div><small>Answer time</small><strong>{r?(r.status===503&&r.error?.includes("not configured")?"not set up":r.elapsed_ms+" ms"):"—"}</strong></div>
          <div><small>HTTP status</small><strong>{r?.status??"—"}</strong></div></div>
        {r?.error&&<p className="error">{r.error}</p>}
      </article>;})}</div>
    <footer className="workload-footer">Customer view only: nothing on this page changes routing, servers or AWS.</footer>
  </main>;

  const menu=user?.response?.items??[];
  const shopView=<main className="cafe">
    <header className="cafe-top"><div className="logo">Harbour Lane <span>Coffee</span></div>
      <nav><a>Menu</a><a>Visit us</a><a>Order ahead</a></nav></header>
    <section className="cafe-hero"><div>
      <small>Small-batch coffee · Singapore &amp; Virginia</small>
      <h1>Good coffee, ready when you are.</h1>
      <p>Order ahead and skip the queue. Freshly roasted beans, pastries baked every morning.</p>
      <button onClick={()=>void sample()}>Order for pickup</button></div>
      <div className="cup" aria-hidden="true">☕</div></section>
    {user&&v==="failed"&&<div className="cafe-alert">Sorry, we can&apos;t load our menu right now. Please try again in a moment.</div>}
    {user&&v==="slow"&&<div className="cafe-alert slow">Still loading… this is taking longer than usual.</div>}
    <section className="cafe-menu"><h2>Today&apos;s menu</h2>
      <div className="cafe-items">{menu.length?menu.map(item=><article key={item.id}>
          <div><b>{item.name}</b><p>{BLURB[item.id]??"Made to order."}</p></div>
          <strong>{typeof item.price==="number"?price(item.price):"—"}</strong></article>)
        :<p className="muted">{user?"The menu is unavailable.":"Loading the menu…"}</p>}</div></section>
    <section className="cafe-info"><div><h3>Hours</h3><p>Mon–Fri 7:00–19:00<br/>Sat–Sun 8:00–17:00</p></div>
      <div><h3>Find us</h3><p>12 Harbour Lane<br/>and our second shop across the ocean</p></div>
      <div><h3>This visit</h3><p>{user?`Page served in ${user.elapsed_ms} ms from ${REGION[user.origin??""]??"—"}`:"Connecting…"}</p></div></section>
    <footer className="cafe-foot">Harbour Lane Coffee is a demo shop for the AdaptiveNet lab. Nothing here takes real orders.</footer>
  </main>;
  return <>
    <div className="tabs"><button className={tab==="shop"?"on":""} onClick={()=>setTab("shop")}>Customer website</button>
      <button className={tab==="behind"?"on":""} onClick={()=>setTab("behind")}>Behind the scenes</button></div>
    {tab==="shop"?shopView:behindView}
  </>;
}