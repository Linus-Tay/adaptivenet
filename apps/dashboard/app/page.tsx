"use client";
import {useEffect,useState} from "react";
export default function Page(){
 const [state,setState]=useState<unknown>({});const [message,setMessage]=useState("Ready");
 async function refresh(){try{const r=await fetch("/api/lab");setState(await r.json());}catch{setMessage("Control API unreachable");}}
 useEffect(()=>{void refresh();const timer=setInterval(refresh,3000);return()=>clearInterval(timer);},[]);
 async function action(action:string){setMessage("Running…");try{const r=await fetch("/api/lab",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action})});const b=await r.json();setMessage(r.ok?"Applied":JSON.stringify(b));await refresh();}catch{setMessage("Action failed");}}
 return <main><nav className="nav"><a href="/applications">Applications & endpoints ↗</a></nav><small>ADAPTIVENET / LOCAL LAB</small><h1>Resilience control room</h1><p>Manual routing simulator. Automatic detection, trained models and AWS integrations are not connected yet.</p><a href="http://localhost:3001" target="_blank" rel="noreferrer">Open demo catalogue ↗</a><p>1. Send requests to A. 2. Inject a bounded fault. 3. Observe the failure. 4. Route to B and retry.</p><button onClick={()=>action("route-a")}>Route to A</button><button onClick={()=>action("latency")}>A: +1.5s for 30s</button><button onClick={()=>action("failure")}>A: HTTP 503 for 30s</button><button onClick={()=>action("route-b")}>Route to B</button><button onClick={()=>action("reset")}>Clear faults</button><p role="status">{message}</p><pre>{JSON.stringify(state,null,2)}</pre></main>;
}
