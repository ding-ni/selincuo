export async function api(path,body,options={}){
 const response=await fetch('/api/model/'+path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:undefined,body:body?JSON.stringify(body):undefined,...options})
 const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败');return data
}
export const number=(v,n=2)=>v==null||!Number.isFinite(Number(v))?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:n})
export const standardName='色林错流域—湖泊耦合模型标准方案'
export function resultName(t){if(t?.builtin)return '色林错湖位与入湖径流模拟成果（2024—2025）';return t?.name||'湖位与入湖径流模拟成果'}
