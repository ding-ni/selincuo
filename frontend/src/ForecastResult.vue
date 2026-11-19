<script setup>
import {computed,ref,watch} from 'vue'
import Chart from './Chart.vue'
import {api,number,fixed} from './api.mjs'
const props=defineProps({forecasts:{type:Array,default:()=>[]},selected:String})
const emit=defineEmits(['update:selected','warning','risk'])
const COLORS={blue:'#1677ff',yellow:'#f5c400',orange:'#ff7d00',red:'#f5222d'}
const detail=ref(null),history=ref([]),assessment=ref(null),error=ref(''),loading=ref(false)
const chosen=computed({get:()=>props.selected,set:v=>emit('update:selected',v)})
watch(()=>props.selected,load,{immediate:true})
async function load(){const id=props.selected;detail.value=null;history.value=[];assessment.value=null;error.value='';if(!id)return;loading.value=true
 try{const [d,h,a]=await Promise.allSettled([api('forecasts/'+encodeURIComponent(id)),api('forecasts/'+encodeURIComponent(id)+'/history'),api('warnings/assess?'+new URLSearchParams({forecastId:id}))])
  if(props.selected!==id)return;if(d.status==='rejected')throw d.reason;detail.value=d.value.result||d.value;history.value=h.status==='fulfilled'?h.value.rows:[];assessment.value=a.status==='fulfilled'?a.value:null}catch(e){error.value=e.message}finally{loading.value=false}}
const ensemble=computed(()=>Number(detail.value?.memberCount)>1)
const series=key=>ensemble.value?detail.value.quantiles.p50[key]:detail.value.control[key]
const dayOf=t=>new Date(Date.parse(t+'Z')-3600000).toISOString().slice(0,10)
// Hourly forecast values are averaged to Beijing days for the bulletin charts.
function daily(values){const out=new Map();(detail.value?.times||[]).forEach((t,i)=>{const d=dayOf(t),v=values?.[i];if(v==null)return;const o=out.get(d)||{s:0,n:0};o.s+=v;o.n++;out.set(d,o)});return new Map([...out].map(([d,o])=>[d,o.s/o.n]))}
function joined(histKey,obsKey,fcKey,days){if(!detail.value)return []
 const past=history.value.slice(-days).map(r=>({date:r.date,hist:r[histKey],obs:r[obsKey]}))
 const fc=daily(series(fcKey)),lo=ensemble.value?daily(detail.value.quantiles.p10[fcKey]):null,hi=ensemble.value?daily(detail.value.quantiles.p90[fcKey]):null
 if(past.length)past.at(-1).fc=past.at(-1).hist
 return [...past,...[...fc].map(([date,v])=>({date,fc:v,p10:lo?.get(date),p90:hi?.get(date)}))]}
const levelRows=computed(()=>joined('level','observedLevel','lakeLevel',60))
const dujaRows=computed(()=>joined('duja','observedFlow','dujaFlow',30))
const rules=computed(()=>assessment.value?.rules?.levels||[])
const warningLines=computed(()=>({markLine:{silent:true,symbol:'none',data:rules.value.map(l=>({yAxis:l.levelM,lineStyle:{color:COLORS[l.color],type:'dashed',width:1},label:{formatter:l.grade+' '+l.levelM.toFixed(2),color:COLORS[l.color],position:'insideEndTop',fontSize:11}}))}}))
const levelSeries=computed(()=>[{key:'hist',label:'模拟湖位'},{key:'obs',label:'主站实测',observed:true},{key:'fc',label:ensemble.value?'预报中位数':'预报湖位',options:{lineStyle:{width:2.5,color:'#d0602e'},itemStyle:{color:'#d0602e'},...warningLines.value}},...(ensemble.value?[{key:'p10',label:'P10',options:{lineStyle:{type:'dashed',width:1,color:'#e3a07c'},itemStyle:{color:'#e3a07c'}}},{key:'p90',label:'P90',options:{lineStyle:{type:'dashed',width:1,color:'#e3a07c'},itemStyle:{color:'#e3a07c'}}}]:[])])
const dujaSeries=computed(()=>[{key:'hist',label:'模拟流量'},{key:'obs',label:'杜加实测',observed:true},{key:'fc',label:ensemble.value?'预报中位数':'预报流量',options:{lineStyle:{width:2.5,color:'#d0602e'},itemStyle:{color:'#d0602e'}}},...(ensemble.value?[{key:'p10',label:'P10',options:{lineStyle:{type:'dashed',width:1,color:'#e3a07c'}}},{key:'p90',label:'P90',options:{lineStyle:{type:'dashed',width:1,color:'#e3a07c'}}}]:[])])
const levelExtent=computed(()=>rules.value.length?[rules.value[0].levelM,rules.value.at(-1).levelM]:[])
const RIVERS=[['zajia','扎加藏布'],['zagen','扎根藏布'],['boqu','波曲'],['ali','阿里藏布'],['fringe','湖周直接入湖区']]
const riverRows=computed(()=>{if(!detail.value?.control?.rivers)return [];const maps=Object.fromEntries(RIVERS.map(([k])=>[k,daily(detail.value.control.rivers[k])]));const out=daily(detail.value.control.outflow);return [...maps.zajia.keys()].map(d=>({date:d,...Object.fromEntries(RIVERS.map(([k])=>[k,maps[k].get(d)])),outflow:out.get(d)}))})
const hasOutflow=computed(()=>(detail.value?.control?.outflow||[]).some(v=>v>0))
const hourlyRows=computed(()=>(detail.value?.times||[]).map((t,i)=>({time:t,level:series('lakeLevel')[i],duja:series('dujaFlow')?.[i],outflow:series('outflow')?.[i]})))
const tableRows=computed(()=>{if(!detail.value)return [];const lv=daily(series('lakeLevel')),dj=daily(series('dujaFlow')),inf=daily(series('inflow')),out=daily(series('outflow'));const rain=new Map(),ev=new Map();detail.value.times.forEach((t,i)=>{const d=dayOf(t);rain.set(d,(rain.get(d)||0)+detail.value.control.lakeRainMm[i]);ev.set(d,(ev.get(d)||0)+detail.value.control.lakeEvaporationMm[i])});return [...lv.keys()].map(d=>({date:d,level:lv.get(d),duja:dj.get(d),inflow:inf.get(d),outflow:out.get(d),rain:rain.get(d),evap:ev.get(d)}))})
const s=computed(()=>detail.value?.summary||{})
const change=computed(()=>s.value.finalMedianLevel!=null&&detail.value?s.value.finalMedianLevel-detail.value.initialLakeLevel:null)
const grade=computed(()=>assessment.value?.selected)
const time=t=>t?String(t).replace('T',' ').slice(5,16):'—'
const issueDay=computed(()=>detail.value?.issueTime?.slice(0,10))
</script>
<template>
<section class="panel fr">
 <div class="fr-head">
  <label>预报成果<select v-model="chosen"><option value="">请选择</option><option v-for="f in forecasts" :key="f.id" :value="f.id">{{f.name}} · 起报 {{String(f.issueTime||'').replace('T',' ').slice(0,16)}}</option></select></label>
  <button v-if="detail" class="primary fr-risk" @click="emit('risk',detail.id)">推演漫溢情景 →</button>
  <span v-if="detail" class="fr-meta">{{ensemble?detail.memberCount+' 个成员':'单情景'}} · {{number(detail.hours/24,0)}} 天 · {{String(detail.issueTime).replace('T',' ').slice(0,16)}} 起报</span>
 </div>
 <p v-if="error" class="message error">{{error}}</p>
 <p v-if="loading" class="muted">读取预报成果…</p>
 <p v-if="!forecasts.length" class="empty">尚无预报成果，请先在“预报计算”中完成一次预报。</p>
 <template v-if="detail">
  <div class="fr-kpis">
   <div><span>起报湖位</span><b>{{fixed(detail.initialLakeLevel,2)}}<em>m</em></b></div>
   <div><span>最高湖位</span><b>{{fixed(s.maximumMedianLevel,2)}}<em>m</em></b><small>{{time(s.maximumMedianLevelTime)}}</small></div>
   <div><span>期末湖位</span><b>{{fixed(s.finalMedianLevel,2)}}<em>m</em></b><small :class="change>0?'up':'down'">{{change==null?'':(change>=0?'+':'')+number(change*100,1)+' cm'}}</small></div>
   <div><span>杜加最大流量</span><b>{{number(s.peakDujaM3s,1)}}<em>m³/s</em></b><small>{{time(s.peakDujaTime)}}</small></div>
   <div><span>溃口出流总量</span><b>{{number((s.outflowVolumeM3||0)/1e4,0)}}<em>万m³</em></b><small>预见期累计</small></div>
   <button class="fr-grade" :style="grade?{'--g':COLORS[grade.color]}:{}" :class="{none:!grade}" @click="emit('warning')"><span>预警等级</span><b>{{grade?grade.grade+' '+({blue:'蓝色',yellow:'黄色',orange:'橙色',red:'红色'})[grade.color]:'未达预警'}}</b><small>{{grade?(grade.basis==='outflow'?'溃口出流':'湖位')+'首达 '+time(grade.firstTime):'查看等级标准 →'}}</small></button>
  </div>
  <div class="fr-charts">
   <div class="fr-card"><h4>色林错湖位 <small>近60天实况 + 预报（日均）</small></h4><Chart :rows="levelRows" :series="levelSeries" unit="m" :day="issueDay" :include="levelExtent" :context="'fr-level:'+detail.id"/></div>
   <div class="fr-card"><h4>杜加站流量 <small>近30天实况 + 预报（日均）</small></h4><Chart :rows="dujaRows" :series="dujaSeries" unit="m³/s" :day="issueDay" zero :context="'fr-duja:'+detail.id"/></div>
  </div>
  <div class="fr-charts">
   <div class="fr-card small"><h4>各河入湖流量 <small>日均</small></h4><Chart :rows="riverRows" :series="RIVERS.map(([k,l])=>({key:k,label:l}))" unit="m³/s" zero :context="'fr-rivers:'+detail.id"/></div>
   <div class="fr-card small"><h4>溃口出流过程 <small>日均</small></h4><Chart v-if="hasOutflow" :rows="riverRows" :series="[{key:'outflow',label:'溃口出流',options:{areaStyle:{opacity:.15}}}]" unit="m³/s" zero :context="'fr-out:'+detail.id"/><p v-else class="fr-empty">预见期内湖位低于溃口堰顶，无出流。</p></div>
  </div>
  <details><summary>逐时过程</summary><div class="fr-charts"><div><Chart :rows="hourlyRows" :series="[{key:'level',label:'湖位'}]" unit="m" hourly :context="'fr-hl:'+detail.id"/></div><div><Chart :rows="hourlyRows" :series="[{key:'duja',label:'杜加流量'},{key:'outflow',label:'溃口出流'}]" unit="m³/s" hourly zero :context="'fr-hd:'+detail.id"/></div></div></details>
  <details><summary>逐日数值表</summary><div class="table-wrap"><table><thead><tr><th>日期</th><th>日均湖位 m</th><th>杜加 m³/s</th><th>入湖 m³/s</th><th>出流 m³/s</th><th>湖面降水 mm</th><th>湖面蒸发 mm</th></tr></thead><tbody><tr v-for="r in tableRows" :key="r.date"><td>{{r.date}}</td><td>{{fixed(r.level,3)}}</td><td>{{number(r.duja,2)}}</td><td>{{number(r.inflow,2)}}</td><td>{{number(r.outflow,2)}}</td><td>{{number(r.rain,1)}}</td><td>{{number(r.evap,1)}}</td></tr></tbody></table></div></details>
 </template>
</section>
</template>
<style scoped>
.fr-head{display:flex;gap:18px;align-items:flex-end;flex-wrap:wrap;margin-bottom:18px}
.fr-head label{flex:0 1 520px}
.fr-meta{font-size:13px;color:var(--muted);padding-bottom:10px}
.fr-risk{order:3;margin-left:auto}
.fr-kpis{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin-bottom:20px}
.fr-kpis>*{display:grid;gap:4px;align-content:start;padding:14px 16px;border:1px solid var(--line);border-radius:8px;background:#fbfdfd;text-align:left;justify-items:start;min-height:0}
.fr-kpis span{font-size:12px;color:var(--muted)}
.fr-kpis b{font-size:22px;font-weight:600;color:#245e6f;font-variant-numeric:tabular-nums;white-space:nowrap}
.fr-kpis em{font-style:normal;font-size:12px;font-weight:400;color:var(--muted);margin-left:3px}
.fr-kpis small{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.fr-kpis small.up{color:#c2582c}.fr-kpis small.down{color:#2a7d9c}
.fr-grade{--g:#9aaeb5;border:1px solid var(--g)!important;background:color-mix(in srgb,var(--g) 10%,#fff)!important}
.fr-grade b{color:var(--g)!important;font-size:19px!important}
.fr-grade.none b{color:#5f7f89!important}
.fr-charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin-bottom:18px}
.fr-card{min-width:0;border:1px solid var(--line);border-radius:8px;padding:14px 16px 6px}
.fr-card h4{margin:0 0 4px;font-size:15px}
.fr-card.small :deep(.chart){height:250px}
.fr-card h4 small{font-weight:400;color:var(--muted);margin-left:6px;font-size:12px}
.fr-empty{color:var(--muted);font-size:13px;padding:60px 0;text-align:center;margin:0}
@media(max-width:1300px){.fr-kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:900px){.fr-charts{grid-template-columns:1fr}}
</style>
