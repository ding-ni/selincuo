// Enabled by the installed launcher; source development remains persistent.
export async function connectBrowserSession() {
 let timer
 const response=await fetch('/api/model/browser-session')
 if(!response.ok)return ()=>{}
 const settings=await response.json()
 if(!settings.enabled)return ()=>{}
 const sessionId=crypto.randomUUID()
 const payload=action=>JSON.stringify({sessionId,action,instanceId:settings.instanceId})
 const open=()=>fetch('/api/model/browser-session',{method:'POST',headers:{'Content-Type':'application/json'},body:payload('open'),keepalive:true}).catch(()=>{})
 const close=()=>{navigator.sendBeacon('/api/model/browser-session',new Blob([payload('close')],{type:'application/json'}))}
 const visible=()=>{if(document.visibilityState==='visible')open()}
 await open()
 timer=setInterval(open,15000)
 window.addEventListener('pagehide',close)
 window.addEventListener('pageshow',open)
 document.addEventListener('visibilitychange',visible)
 return ()=>{clearInterval(timer);close();window.removeEventListener('pagehide',close);window.removeEventListener('pageshow',open);document.removeEventListener('visibilitychange',visible)}
}
