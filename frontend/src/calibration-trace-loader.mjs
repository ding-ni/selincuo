// One request per task at a time; late responses never append to another task.
export function createTraceLoader({getTaskId,getOffset,fetchPage,onStart,onSuccess,onFailure,onFinish}){
 let generation=0,active=null,repeat=false
 function reset(){generation++;active=null;repeat=false}
 function load(){
  const id=getTaskId(),offset=getOffset(),ticket=generation
  if(!id)return Promise.resolve()
  if(active?.ticket===ticket&&active.id===id){repeat=true;return active.promise}
  const request={id,offset,ticket};active=request;onStart()
  request.promise=(async()=>{
   let applied=false
   try{
    const page=await fetchPage(id,offset)
    if(ticket===generation&&getTaskId()===id&&getOffset()===offset){onSuccess(page);applied=true}
   }catch(error){if(ticket===generation&&getTaskId()===id)onFailure(error)}
   finally{
    if(active===request){
     active=null;onFinish()
     const again=repeat;repeat=false
     if(again&&applied&&ticket===generation&&getTaskId()===id)await load()
    }
   }
  })()
  return request.promise
 }
 return {load,reset}
}
