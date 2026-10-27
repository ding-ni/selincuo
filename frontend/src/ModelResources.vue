<script setup>
import {ref,computed,onMounted} from 'vue'
import DataImport from './DataImport.vue'
import BasinMap from './Map.vue'
import {api,number} from './api.mjs'
import {shiftCalendarDay} from './history-workflow.mjs'
import {variableDefinitions} from './business-view.mjs'
import {stationLocation,stationDatum} from './shore-comparison.mjs'
const props=defineProps({boot:Object,busy:Boolean,active:Array})
const emit=defineEmits(['navigate','refresh','error','submit','station'])
const tab=ref('forcing'),catalog=ref(null),coverage=ref(null),files=ref([]),station=ref('SLC'),working=ref(false),message=ref(''),startDate=ref(''),endDate=ref('')
const inputs=computed(()=>(props.boot.inputs||[]).filter(i=>i.manifest).slice().sort((a,b)=>b.manifest.end.localeCompare(a.manifest.end)))
const latestInput=computed(()=>inputs.value[0])
const lastDay=computed(()=>latestInput.value?.manifest.end?shiftCalendarDay(latestInput.value.manifest.end,-1):'尚未登记')
const label=v=>variableDefinitions.find(x=>x.id===v)?.label||v
async function imported(){
 emit('refresh')
 try{catalog.value=await api('observation-catalog')}catch(e){emit('error',e.message)}
}
async function checkCoverage(){
 working.value=true
 try{coverage.value=await api('forcing-coverage?check=true')}catch(e){emit('error',e.message)}finally{working.value=false}
}
function update(range=false){
 message.value=''
 if(range&&(!startDate.value||!endDate.value||endDate.value<startDate.value)){message.value='请选择有效的起止日期。';return}
 emit('submit',{kind:'update-forcing',...(range?{startDate:startDate.value,endDate:endDate.value}:{})})
}
async function upload(kind){
 working.value=true;message.value=''
 try{
  const uploaded=[]
  for(const file of files.value){
   const response=await fetch('/api/model/upload?'+new URLSearchParams({filename:file.name,station:station.value}),{method:'PUT',body:file})
   const data=await response.json();if(!response.ok)throw Error(data.error);uploaded.push('incoming/'+data.filename)
  }
  emit('submit',kind==='forcing'?{kind:'register-inputs',files:uploaded,...(latestInput.value?{baseInputId:latestInput.value.id}:{})}:{kind:'import-observations'})
  message.value='资料已上传，正在校验和登记。';files.value=[]
 }catch(e){emit('error',e.message)}finally{working.value=false}
}
onMounted(async()=>{
 const results=await Promise.allSettled([api('observation-catalog'),api('forcing-coverage')])
 if(results[0].status==='fulfilled')catalog.value=results[0].value
 if(results[1].status==='fulfilled')coverage.value=results[1].value
})
</script>
<template>
 <nav class="workspace-tabs"><button v-for="t in [['forcing','气象资料'],['observations','观测资料'],['spatial','空间与站网']]" :key="t[0]" :class="{active:tab===t[0]}" @click="tab=t[0]">{{t[1]}}</button></nav>
 <template v-if="tab==='forcing'">
  <section class="panel">
   <div class="panel-title"><h3>更新气象资料</h3><span class="badge">ERA5-Land</span><button :disabled="working||busy" @click="checkCoverage">{{working?'检查中…':'检查官方可用范围'}}</button></div>
   <div class="resource-summary"><div><span>已登记资料</span><b>{{latestInput?.manifest.start?.slice(0,10)||'—'}} 至 {{lastDay}}</b><small>截至完整北京时间日</small></div><div><span>官方目录</span><b>{{coverage?.catalog?.availableEndUTC?.slice(0,10)||'尚未检查'}}</b><small>以三变量实际覆盖校验为准</small></div></div>
   <p class="muted">按需下载、续传并登记气象资料；已有成果继续使用各自的输入。</p>
   <div class="query-grid"><label>补充起日<input type="date" v-model="startDate"></label><label>补充止日<input type="date" v-model="endDate"></label><button :disabled="working||busy||active.length>0" @click="update(true)">更新所选时段</button><button class="primary" :disabled="working||busy||active.length>0" @click="update(false)">更新至可用最新日期</button></div>
   <p v-if="message" class="note warning" role="status">{{message}}</p>
   <p v-if="coverage?.update?.status&&coverage.update.status!=='succeeded'" class="note">上次更新：{{coverage.update.message||coverage.update.status}}</p>
   <p class="note">如提示 CDS 账户或许可问题，可查看 <a href="/api/model/cds-help">账户配置说明</a>。</p>
   <div class="actions"><button @click="emit('navigate','simulation')">前往历史模拟 →</button><button @click="emit('navigate','calibration')">前往方案与率定 →</button></div>
   <details><summary>已登记资料及手动导入</summary><div class="table-wrap"><table><thead><tr><th>资料</th><th>起始</th><th>结束</th></tr></thead><tbody><tr v-for="i in inputs" :key="i.id"><td>{{i.name}}</td><td>{{i.manifest.start.replace('T',' ')}}</td><td>{{i.manifest.end.replace('T',' ')}}</td></tr></tbody></table></div><div class="result-toolbar"><label class="grow">已有气象文件<input type="file" multiple accept=".nc" @change="files=Array.from($event.target.files)"></label><button :disabled="working||busy||active.length>0||!files.length" @click="upload('forcing')">导入并处理</button></div></details>
  </section>
 </template>
 <template v-if="tab==='observations'">
  <section class="panel"><div class="panel-title"><h3>更新观测资料</h3><button class="link" @click="emit('navigate','monitor')">查询监测资料 →</button></div>
   <DataImport :stations="boot.stations" :disabled="busy||working||active.length>0" @imported="imported" @error="emit('error',$event)"/>
   <details><summary>导入原格式观测表</summary><div class="result-toolbar"><label>所属站点<select v-model="station"><option v-for="s in boot.stations.filter(s=>['SLC','DUJA','BLQ','115DB'].includes(s.id))" :key="s.id" :value="s.id">{{s.name}}</option></select></label><label class="grow">Excel 文件<input type="file" multiple accept=".xls,.xlsx" @change="files=Array.from($event.target.files)"></label><button :disabled="working||busy||active.length>0||!files.length" @click="upload('observations')">导入</button></div></details>
   <div class="actions"><button @click="emit('navigate','simulation')">前往历史模拟 →</button><button @click="emit('navigate','calibration')">前往方案与率定 →</button></div>
  </section>
  <section class="panel"><div class="panel-title"><h3>站点资料范围</h3></div><div class="table-wrap"><table><thead><tr><th>站点 / 要素</th><th>资料时段</th><th>有值记录</th><th>状态</th></tr></thead><tbody><template v-for="s in catalog?.stations||[]" :key="s.id"><tr v-for="v in s.variables" :key="v.id"><td><button class="link" @click="emit('station',s.id)">{{s.name}} · {{label(v.id)}}</button></td><td>{{v.start.slice(0,10)}} — {{v.end.slice(0,10)}}</td><td>{{v.valid.toLocaleString()}}</td><td>{{s.kind==='lake'?(s.id==='SLC'?'主站工作基准':'基准待核'):'观测资料'}}</td></tr></template></tbody></table></div></section>
 </template>
 <section v-if="tab==='spatial'" class="panel"><div class="panel-title"><h3>流域与站网</h3></div><BasinMap :stations="boot.stations" @station="emit('station',$event)"/>
  <div class="table-wrap"><table><thead><tr><th>分区</th><th>陆域面积 km²</th><th>冰川面积 km²</th></tr></thead><tbody><tr v-for="b in boot.spatial?.basins||[]" :key="b.id"><td>{{b.name}}</td><td>{{number(b.areaKm2)}}</td><td>{{number(b.glacierAreaKm2)}}</td></tr></tbody></table></div>
  <details><summary>站点位置与基准</summary><div class="table-wrap"><table><thead><tr><th>站点</th><th>经度 WGS84</th><th>纬度 WGS84</th><th>水位基准</th><th>定位来源</th></tr></thead><tbody><tr v-for="s in catalog?.stations||boot.stations" :key="s.id"><td><button class="link" @click="emit('station',s.id)">{{s.name}}</button></td><td>{{number(s.lon,6)}}</td><td>{{number(s.lat,6)}}</td><td>{{stationDatum(s)?(s.id==='SLC'?'主站工作基准':'基准待核'):'—'}}</td><td>{{stationLocation(s)?.source||'既有登记'}}</td></tr></tbody></table></div></details>
 </section>
</template>
<style scoped>.resource-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin:18px 0}.resource-summary>div{display:grid;gap:4px;padding:16px;background:#f3f7f8;border-radius:6px}.resource-summary span,.resource-summary small{font-size:12px;color:var(--muted)}.resource-summary b{font-size:19px;font-variant-numeric:tabular-nums}@media(max-width:700px){.resource-summary{grid-template-columns:1fr}}</style>
