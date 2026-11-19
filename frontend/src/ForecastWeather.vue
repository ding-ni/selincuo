<script setup>
import {computed,reactive,ref,watch} from 'vue'
import Chart from './Chart.vue'
import {api,number} from './api.mjs'
const props=defineProps({issue:Object,sourceId:String,busy:Boolean})
const emit=defineEmits(['saved','task','navigate','error'])
const REGION_CODES={'全流域':'uniform','扎加藏布':'zajia','杜加站以上':'duja','扎根藏布':'zagen','波曲':'boqu','阿里藏布':'ali','湖周直接入湖区':'fringe','湖周':'fringe','湖面':'lake'}
const method=ref('station'),days=ref(10),diurnal=ref('uniform'),name=ref('未来10天站点预报'),working=ref(false),file=ref(null),fileRows=ref(null)
const weekday=['日','一','二','三','四','五','六']
const issueDay=computed(()=>props.issue?.issueTime?.slice(0,10))
const dateAt=i=>{const d=new Date(Date.parse(issueDay.value+'T00:00:00Z')+i*86400000);return {date:d.toISOString().slice(0,10),label:d.toISOString().slice(5,10)+' 周'+weekday[d.getUTCDay()]}}
// Station forecast: daily rain per gauge; blank cells use the gauge's same-period mean.
const info=ref(null),rows=ref([])
const stations=computed(()=>info.value?.stations||[])
function rebuild(){if(!issueDay.value)return;const old=rows.value;rows.value=Array.from({length:days.value},(_,i)=>({...dateAt(i),stations:old[i]?.stations||{},temperature:old[i]?.temperature??''}))
 const base=method.value==='climatology'?'历史同期估算':'站点预报';if(!name.value||/^未来\d+天(情景|站点预报|历史同期估算)$/.test(name.value))name.value='未来'+days.value+'天'+base}
watch([issueDay,days,method],rebuild,{immediate:true})
watch([()=>props.sourceId,()=>props.issue?.issueTime,days],async()=>{info.value=null;if(!props.sourceId||!props.issue?.issueTime)return
 try{info.value=await api('forecast-inputs/station-stations',{sourceResultId:props.sourceId,issueTime:props.issue.issueTime,days:days.value})}catch(e){emit('error',e.message)}},{immediate:true})
const blank=v=>v===''||v==null
const climAt=(sid,i)=>stations.value.find(s=>s.id===sid)?.climatologyMm?.[i]
const filled=computed(()=>rows.value.reduce((n,r)=>n+Object.values(r.stations).filter(v=>!blank(v)).length,0))
const preview=computed(()=>rows.value.map((r,i)=>{const vals=stations.value.map(s=>blank(r.stations[s.id])?null:Number(r.stations[s.id])).filter(v=>v!=null)
 return {date:r.date,rain:method.value==='station'&&vals.length?vals.reduce((a,b)=>a+b,0)/vals.length:null,clim:info.value?.basinClimatologyMm?.[i]??null,temp:blank(r.temperature)?null:Number(r.temperature)}}))
function fillStation(sid){const v=rows.value[0]?.stations[sid];for(const r of rows.value)r.stations[sid]=v}
function payload(){const shape=diurnal.value==='afternoon'?{rainStartHour:12,rainEndHour:24,rainShape:'triangle'}:{rainStartHour:0,rainEndHour:24,rainShape:'uniform'}
 return {...shape,daily:rows.value.map(r=>({date:r.date,stations:method.value==='station'?Object.fromEntries(Object.entries(r.stations).filter(([,v])=>!blank(v)).map(([k,v])=>[k,Number(v)])):{},
  ...(blank(r.temperature)?{}:{temperatureC:Number(r.temperature)})}))}}
async function action(fn){working.value=true;try{await fn()}catch(e){emit('error',e.message)}finally{working.value=false}}
async function saveStation(){await action(async()=>{const value=await api('forecast-inputs/station',{name:name.value,sourceResultId:props.sourceId,issueTime:props.issue.issueTime,days:days.value,method:method.value,...payload()});emit('saved',value.id)})}
// Hourly table: template generated here; CSV is parsed in the browser, XLSX goes through the existing upload path.
function template(){const start=Date.parse(props.issue.issueTime+'Z'),lines=['时间,区域,降水(mm),气温(℃),蒸发(mm)'];for(let h=1;h<=days.value*24;h++){const t=new Date(start+h*3600000).toISOString().replace('T',' ').slice(0,16);lines.push(`${t},全流域,0,,`)}
 const blob=new Blob(['﻿'+lines.join('\r\n')],{type:'text/csv;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`逐时气象模板-${issueDay.value}-${days.value}天.csv`;a.click();URL.revokeObjectURL(a.href)}
async function pick(event){file.value=event.target.files?.[0]||null;fileRows.value=null;if(file.value&&/\.csv$/i.test(file.value.name)){const text=await file.value.text();fileRows.value=parseCsv(text)}}
function parseCsv(text){const lines=text.replace(/^﻿/,'').split(/\r?\n/).filter(l=>l.trim()),head=lines.shift().split(',').map(s=>s.trim())
 const col=k=>head.findIndex(h=>h.startsWith(k)),ti=col('时间'),ri=col('区域'),pi=col('降水'),te=col('气温'),ei=col('蒸发')
 if(ti<0||pi<0||te<0)throw Error('表头须包含 时间、降水(mm)、气温(℃)')
 return lines.map(l=>{const c=l.split(',').map(s=>s.trim()),region=ri>=0&&c[ri]?REGION_CODES[c[ri]]||c[ri]:'uniform',row={time:c[ti].replace(' ','T')+(c[ti].length===16?':00':''),region,precipitationMm:c[pi]===''?null:Number(c[pi]),temperatureC:c[te]===''?null:Number(c[te])};if(ei>=0&&c[ei]!=='')row.evaporationMm=Number(c[ei]);return row})}
async function importHourly(){await action(async()=>{let body={name:name.value,sourceResultId:props.sourceId,issueTime:props.issue.issueTime,days:days.value}
 if(fileRows.value){if(fileRows.value.some(r=>r.precipitationMm==null||r.temperatureC==null))throw Error('表格中仍有空白的降水或气温');body.rows=fileRows.value}
 else{const response=await fetch('/api/model/upload?'+new URLSearchParams({filename:file.value.name,format:'exchange'}),{method:'PUT',body:file.value});const upload=await response.json();if(!response.ok)throw Error(upload.error||'上传失败');body.filename=upload.filename;body.path='incoming/'+upload.filename}
 const value=await api('forecast-inputs/hourly',body);emit('saved',value.id)})}
const gapDays=computed(()=>props.issue?.lagDays??null)
async function ensemble(){await action(async()=>{const task=await api('forecast-inputs/ensemble',{sourceResultId:props.sourceId,days:15});emit('task',task)})}
</script>
<template>
<section class="panel fw">
 <div class="fw-methods four" role="tablist">
  <button :class="{on:method==='station'}" @click="method='station'"><b>站点预报</b><span>按雨量站逐日填写预报雨量，平台按站点比例法换算到各计算单元</span></button>
  <button :class="{on:method==='climatology'}" @click="method='climatology'"><b>历史同期估算</b><span>无预报时，按多年同期（前后 7 天）平均降水、气温和蒸发能力</span></button>
  <button :class="{on:method==='hourly'}" @click="method='hourly'"><b>导入逐时表格</b><span>下载模板，填写逐小时过程后导入</span></button>
  <button :class="{on:method==='ensemble'}" @click="method='ensemble'"><b>ECMWF 集合预报</b><span>未来15天全球数值预报，51个成员</span></button>
 </div>

 <template v-if="method!=='ensemble'">
  <div class="fw-bar">
   <label class="grow">情景名称<input v-model="name"></label>
   <div class="fw-field"><span>预见期</span><div class="seg"><button v-for="d in [7,10,15]" :key="d" :class="{on:days===d}" @click="days=d">{{d}}天</button></div></div>
   <div class="fw-field"><span>起始</span><b class="fw-start">{{issue?.issueTime?.replace('T',' ').slice(0,16)||'—'}}</b></div>
  </div>
 </template>

 <template v-if="method==='station'||method==='climatology'">
  <div v-if="method==='station'" class="fw-bar">
   <div class="fw-field"><span>日内过程</span><div class="seg"><button :class="{on:diurnal==='uniform'}" @click="diurnal='uniform'">全天均匀</button><button :class="{on:diurnal==='afternoon'}" @click="diurnal='afternoon'">午后集中（12—24时）</button></div></div>
   <p class="fw-hint">各站填写预报的日雨量；留空的格子按该站多年同期平均（灰色）参与插值。气温留空时用多年同期逐时过程。</p>
  </div>
  <p v-else class="fw-hint">全部 {{days}} 天按多年同期（前后 7 天）平均的降水、气温和蒸发能力生成，用于没有天气预报时的趋势估计。下表为各站同期平均日雨量，仅供查看。</p>
  <p v-if="!info" class="muted">正在读取雨量站与多年同期资料…</p>
  <div v-else class="table-wrap fw-table">
   <table>
    <thead><tr><th>日期</th>
     <th v-for="st in stations" :key="st.id">{{st.name}}<small>日雨量 mm</small><button v-if="method==='station'" class="link fw-copy" title="将首日值填入各日" @click="fillStation(st.id)">↓</button></th>
     <th>流域日均气温 ℃<small>{{method==='station'?'可留空':'多年同期'}}</small></th><th class="num">同期面雨量 mm</th></tr></thead>
    <tbody><tr v-for="(r,i) in rows" :key="r.date">
     <td class="fw-date">{{r.label}}</td>
     <td v-for="st in stations" :key="st.id">
      <input v-if="method==='station'" type="number" min="0" max="300" step="0.1" v-model="r.stations[st.id]" :placeholder="number(climAt(st.id,i),1)" :aria-label="r.date+st.name+'预报雨量'">
      <span v-else class="fw-clim-v">{{number(climAt(st.id,i),1)}}</span></td>
     <td><input v-if="method==='station'" type="number" step="0.1" v-model="r.temperature" placeholder="同期" :aria-label="r.date+'气温'"><span v-else class="fw-clim-v">同期</span></td>
     <td class="num fw-clim-v">{{number(info.basinClimatologyMm[i],1)}}</td>
    </tr></tbody>
   </table>
  </div>
  <div class="fw-preview single"><div><h4>逐日降水 <small v-if="method==='station'">已填 {{filled}} 项站点预报</small></h4>
   <Chart :rows="preview" :series="method==='station'?[{key:'rain',label:'填写站点平均',type:'bar'},{key:'clim',label:'同期面雨量'}]:[{key:'clim',label:'同期面雨量',type:'bar'}]" unit="mm" zero context="fw-rain"/></div></div>
  <div class="actions"><button class="primary" :disabled="busy||working||!sourceId||!info" @click="saveStation">保存未来气象</button>
   <span class="muted">{{method==='station'?(filled?'未填的站日按多年同期补齐':'尚未填写站点预报，将全部按多年同期生成'):'约需 1 分钟'}}</span></div>
 </template>

 <template v-if="method==='hourly'">
  <ol class="fw-steps">
   <li><b>下载模板</b><span>已按起报时刻生成 {{days*24}} 个整点，区域默认“全流域”。可增加分区行：扎加藏布、杜加站以上、扎根藏布、波曲、阿里藏布、湖周直接入湖区、湖面。</span><button @click="template" :disabled="!issue">下载 CSV 模板</button></li>
   <li><b>填写</b><span>每小时降水（mm）和气温（℃），蒸发可留空。时间为该小时的结束时刻。</span></li>
   <li><b>导入</b><label class="button fw-file">选择文件（CSV / XLSX）<input type="file" accept=".csv,.xlsx" @change="pick($event).catch(e=>emit('error',e.message))"></label><span v-if="file">{{file.name}}<template v-if="fileRows"> · {{fileRows.length}} 行</template></span></li>
  </ol>
  <div class="actions"><button class="primary" :disabled="busy||working||!file" @click="importHourly">检查并导入</button></div>
 </template>

 <template v-if="method==='ensemble'">
  <div class="fw-ensemble">
   <div><h4>ECMWF IFS 0.25° 集合预报</h4><p>按流域所在格点下载未来 15 天逐时降水和气温（51 个成员），自动映射到计算单元，结果给出中位数及 10%—90% 范围。</p></div>
   <dl><dt>模型状态末时刻</dt><dd>{{issue?.stateTime?.replace('T',' ').slice(0,16)||'—'}}</dd><dt>距今</dt><dd>{{gapDays==null?'—':gapDays+' 天'}}</dd></dl>
   <p v-if="gapDays>1" class="fw-gap">集合预报从当前时刻起报，模型状态须先更新到今日才能衔接。请在“实况更新”中完成状态更新。</p>
   <button class="primary" :disabled="busy||working||gapDays>1||!sourceId" @click="ensemble">获取集合预报</button>
  </div>
 </template>

</section>
</template>
<style scoped>
.fw-methods{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:22px}
.fw-methods.four{grid-template-columns:repeat(4,minmax(0,1fr))}
.fw-hint{flex:1 1 320px;margin:0 0 12px;font-size:13px;color:#4c6772;line-height:1.6}
.fw-table th small{display:block;font-weight:400;font-size:11px;color:var(--muted)}
.fw-clim-v{color:#8a989d;font-variant-numeric:tabular-nums}
.fw-preview.single{grid-template-columns:1fr}
.fw-methods>button{display:grid;justify-items:start;gap:4px;text-align:left;padding:14px 16px;border-radius:8px}
.fw-methods>button span{font-size:12.5px;color:var(--muted)}
.fw-methods>button.on{border-color:#2f8d9b;background:#eef7f8;box-shadow:inset 0 -3px 0 #2f8d9b}
.fw-bar{display:flex;flex-wrap:wrap;gap:18px;align-items:flex-end;margin-bottom:16px}
.fw-bar .grow{flex:1 1 240px}
.fw-field{display:grid;gap:7px;font-size:14px}
.fw-field>span{font-size:14px}
.fw-start{display:block;min-height:42px;line-height:42px;font-variant-numeric:tabular-nums;color:#245e6f}
.seg{display:inline-flex;border:1px solid #cddce3;border-radius:6px;overflow:hidden}
.seg button{border:0;border-radius:0;min-height:40px;padding:6px 14px;background:#fff}
.seg button+button{border-left:1px solid #cddce3}
.seg button.on{background:#1c7885;color:#fff}
.fw-generator{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;padding:14px 16px;background:#f4f8f9;border-radius:8px;margin-bottom:16px}
.fw-generator>b{align-self:center;font-size:14px;color:#2b5f6d;margin-right:4px}
.fw-generator label{width:150px;font-size:13px}
.fw-generator .fw-clim{width:auto;display:flex;flex-direction:row;align-items:center;gap:6px;min-height:42px}
.fw-table{max-height:none}
.fw-table th{white-space:nowrap}
.fw-table td{padding:6px 8px}
.fw-table input{min-height:36px;padding:5px 8px;width:100%;min-width:72px}
.fw-date{white-space:nowrap;font-variant-numeric:tabular-nums;color:#3e6170}
.fw-copy{min-height:20px;padding:0 0 0 6px;font-size:13px}
.fw-note{font-size:12.5px;color:var(--muted);margin:10px 0 18px}
.fw-preview{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
.fw-preview h4{margin:0 0 6px;font-size:14px}
.fw-preview :deep(.chart){height:230px}
.fw-preview small{font-weight:400;color:var(--muted);margin-left:6px}
.fw-steps{display:grid;gap:12px;padding:0;margin:0 0 8px;list-style:none;counter-reset:s}
.fw-steps li{display:grid;grid-template-columns:90px 1fr auto;gap:14px;align-items:center;padding:14px 16px;border:1px solid var(--line);border-radius:8px}
.fw-steps li span{font-size:13px;color:var(--muted)}
.fw-file{position:relative;overflow:hidden}
.fw-file input{position:absolute;inset:0;opacity:0;cursor:pointer}
.fw-ensemble{display:grid;gap:14px;max-width:760px}
.fw-ensemble h4{margin:0 0 6px}
.fw-ensemble p{margin:0;font-size:14px;color:#47636e}
.fw-ensemble dl{grid-template-columns:120px 1fr;margin:0}
.fw-gap{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:12px 14px;background:#fdf6e9;border:1px solid #ecd9ae;border-radius:6px;color:#7a5a1d!important}
.fw-ensemble>button{justify-self:start}
@media(max-width:900px){.fw-methods,.fw-preview{grid-template-columns:repeat(2,minmax(0,1fr))}.fw-steps li{grid-template-columns:1fr}}

</style>
