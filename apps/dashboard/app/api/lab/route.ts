const control=process.env.CONTROL_API_URL??"http://127.0.0.1:8000";
const runner=process.env.EXPERIMENT_URL??control;
async function call(url:string,method="GET",body?:unknown){
 const token=process.env.LAB_TOKEN;
 if(!token)return Response.json({error:"LAB_TOKEN is not configured"},{status:503});
 try{const r=await fetch(url,{method,headers:{"X-Lab-Token":token,"Content-Type":"application/json"},body:body===undefined?undefined:JSON.stringify(body),cache:"no-store",signal:AbortSignal.timeout(6000)});return Response.json(await r.json(),{status:r.status});}
 catch{return Response.json({error:"Lab backend unreachable"},{status:502});}
}
export async function GET(){if(process.env.ADAPTIVENET_MODE==='aws')return Response.json({mode:'aws'});return call(`${control}/state`);}
export async function POST(request:Request){
 if(process.env.ADAPTIVENET_MODE==='aws')return Response.json({error:'AWS browser actions disabled. Use guarded SSM CLI.'},{status:403});
 if(!request.headers.get("content-type")?.startsWith("application/json"))return Response.json({error:"JSON required"},{status:415});
 const origin=request.headers.get("origin");
 if(origin && new URL(origin).host!==request.headers.get("host"))return Response.json({error:"Invalid origin"},{status:403});
 let action:string;let duration=30;let region="region-a";try{const body=await request.json();action=body.action;if(body.region==="region-b")region="region-b";if([30,60,120].includes(body.duration))duration=body.duration;}catch{return Response.json({error:"Invalid JSON"},{status:400});}
 if(action==="route-a"||action==="route-b")return call(`${control}/routes`,"PUT",{region_a_percent:action==="route-a"?100:0});
 if(action==="reset")return call(`${runner}/experiments`,"DELETE");
 if(action==="latency"||action==="failure"||action==="loss")return call(`${runner}/experiments`,"POST",{region,latency_ms:action==="latency"?1500:0,error_rate:action==="failure"?1:0,loss_pct:action==="loss"?80:0,duration_seconds:duration});
 return Response.json({error:"Unknown action"},{status:400});
}
