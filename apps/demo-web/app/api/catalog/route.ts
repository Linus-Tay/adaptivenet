export async function GET(){
 try{const r=await fetch(`${process.env.GATEWAY_URL??"http://127.0.0.1:8080"}/api/catalog`,{cache:"no-store",signal:AbortSignal.timeout(8000)});return Response.json(await r.json(),{status:r.status});}
 catch{return Response.json({error:"Gateway unreachable or timed out"},{status:504});}
}
