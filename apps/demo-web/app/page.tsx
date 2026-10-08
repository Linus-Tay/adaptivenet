"use client";
import {useEffect,useState} from "react";
export default function Page(){
 const [rows,setRows]=useState<string[]>([]);
 const [busy,setBusy]=useState(false);
 async function request(){setBusy(true);const start=performance.now();
  try{const r=await fetch("/api/catalog",{cache:"no-store",signal:AbortSignal.timeout(9000)});const b=await r.json();setRows(old=>[`${new Date().toLocaleTimeString()} | HTTP ${r.status} | ${Math.round(performance.now()-start)}ms | ${JSON.stringify(b)}`,...old].slice(0,12));}
  catch{setRows(old=>["Request timed out or connection failed",...old].slice(0,12));}finally{setBusy(false);}}
 useEffect(()=>{void request();},[]);
 return <main><small>ADAPTIVENET / DEMO WORKLOAD</small><h1>Regional coffee catalogue</h1><p>This frontend stays available while regional API requests slow down or fail. Requests are not automatically retried.</p><button disabled={busy} onClick={request}>{busy?"Waiting for response…":"Fetch catalogue"}</button><pre aria-live="polite">{rows.join("\n\n")}</pre></main>;
}
