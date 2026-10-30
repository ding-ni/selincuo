<script setup>
import {ref,computed,watch,onMounted} from 'vue'
import DataImport from './DataImport.vue'
import NativeObservationImport from './NativeObservationImport.vue'
import {api,number} from './api.mjs'
import {variableDefinitions} from './business-view.mjs'
const props=defineProps({boot:Object,busy:Boolean,active:Array})
const emit=defineEmits(['navigate','refresh','error','submit','station'])
const tab=ref('observations'),catalog=ref(null),coverage=ref(null),working=ref(false),message=ref(''),startDate=ref(''),endDate=ref('')
const activeUpdate=computed(()=>(props.active||[]).find(t=>t.kind==='update-forcing'))
const label=v=>variableDefinitions.find(x=>x.id===v)?.label||v
const stationSummary=computed(()=>(catalog.value?.stations||[]).filter(s=>s.variables?.length).map(s=>({
 id:s.id,name:s.name,variables:s.variables.map(v=>label(v.id)).join(' / '),
 latest:s.variables.map(v=>v.validEnd||v.end||'').sort().at(-1)?.slice(0,10)||'暂无记录'
})))
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
onMounted(async()=>{
 const results=await Promise.allSettled([api('observation-catalog'),api('forcing-coverage')])
 if(results[0].status==='fulfilled')catalog.value=results[0].value
 if(results[1].status==='fulfilled')coverage.value=results[1].value
})
watch(()=>props.boot,async()=>{try{coverage.value=await api('forcing-coverage')}catch(e){emit('error',e.message)}})
</script>
<template>
 <nav class="workspace-tabs"><button v-for="t in [['observations','观测资料'],['forcing','模型气象资料']]" :key="t[0]" :class="{active:tab===t[0]}" @click="tab=t[0]">{{t[1]}}</button></nav>
 <template v-if="tab==='forcing'">
  <section class="panel">
   <div class="panel-title"><h3>更新气象资料</h3><span class="badge">ERA5-Land</span><button :disabled="working||busy" @click="checkCoverage">{{working?'检查中…':'检查官方可用范围'}}</button></div>
   <div class="resource-summary"><div><span>当前可用的完整日</span><b>{{coverage?.current?.complete?coverage.current.firstCompleteDay+' 至 '+coverage.current.lastCompleteDay:'尚未确认'}}</b><small>{{coverage?.current?.complete?'降水、气温和蒸发能力完整':'请检查或补充气象资料'}}</small></div><div><span>官方目录</span><b>{{coverage?.catalog?.availableEndUTC?.slice(0,10)||'尚未检查'}}</b><small>以三变量实际覆盖校验为准</small></div></div>
   <p v-if="coverage?.current&&!coverage.current.complete" class="note warning">{{coverage.current.message}}</p>
   <p class="muted">按需下载、续传并登记气象资料；已有成果继续使用各自的输入。</p>
   <div class="query-grid"><label>补充起日<input type="date" v-model="startDate"></label><label>补充止日<input type="date" v-model="endDate"></label><button :disabled="working||busy||active.length>0" @click="update(true)">更新所选时段</button><button class="primary" :disabled="working||busy||active.length>0" @click="update(false)">更新至可用最新日期</button></div>
   <p v-if="message" class="note warning" role="status">{{message}}</p>
   <p v-if="activeUpdate" class="note">{{activeUpdate.message||'正在更新气象资料'}} · {{number(activeUpdate.progress,1)}}%</p>
   <p v-if="coverage?.update?.status&&coverage.update.status!=='succeeded'" class="note">上次更新：{{coverage.update.message||coverage.update.status}}</p>
   <p class="note">如提示 CDS 账户或许可问题，可查看 <a href="/api/model/cds-help">账户配置说明</a>。</p>
   <div class="actions"><button @click="emit('navigate','simulation')">前往历史模拟 →</button><button @click="emit('navigate','calibration')">前往方案与率定 →</button></div>
  </section>
 </template>
 <template v-if="tab==='observations'">
  <section class="panel"><div class="panel-title"><h3>更新观测资料</h3><button class="link" @click="emit('navigate','monitor')">查询监测资料 →</button></div>
   <NativeObservationImport :stations="boot.stations" :disabled="busy||working||active.length>0" @imported="imported" @error="emit('error',$event)"/>
   <details><summary>其他格式导入</summary><DataImport :stations="boot.stations" :disabled="busy||working||active.length>0" @imported="imported" @error="emit('error',$event)"/></details>
   <details><summary>观测资料与模型驱动</summary><p class="muted">站点更新用于监测查询和后续降水订正。率定采用方案绑定的气象与观测版本；新增资料经整编后用于新的方案修订。</p><p v-if="boot.currentBusiness?.inputId">当前方案：{{boot.currentBusiness.name}}<br>驱动版本：{{boot.currentBusiness.inputId}}</p></details>
  </section>
  <section class="panel"><div class="panel-title"><h3>站点更新概况</h3><span class="muted">最近有效记录</span></div>
   <div class="station-summary"><button v-for="s in stationSummary" :key="s.id" @click="emit('station',s.id)"><strong>{{s.name}}</strong><span>{{s.variables}}</span><time>{{s.latest}}</time></button></div>
   <details><summary>查看各站资料范围</summary><div class="table-wrap"><table><thead><tr><th>站点 / 要素</th><th>有效资料时段</th><th>有值记录</th></tr></thead><tbody><template v-for="s in catalog?.stations||[]" :key="s.id"><tr v-for="v in s.variables" :key="v.id"><td>{{s.name}} · {{label(v.id)}}</td><td>{{v.start.slice(0,10)}} — {{v.end.slice(0,10)}}</td><td>{{v.valid.toLocaleString()}}</td></tr></template></tbody></table></div></details>
  </section>
 </template>
</template>
<style scoped>.resource-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin:18px 0}.resource-summary>div{display:grid;gap:4px;padding:16px;background:#f3f7f8;border-radius:6px}.resource-summary span,.resource-summary small{font-size:12px;color:var(--muted)}.resource-summary b{font-size:19px;font-variant-numeric:tabular-nums}.station-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}.station-summary button{display:grid;gap:7px;text-align:left;padding:16px;background:#f5f8f9;border-color:#e2ebed}.station-summary button:hover{border-color:var(--accent);background:#eef5f5}.station-summary span{font-size:12px;color:var(--muted)}.station-summary time{font-size:16px;font-variant-numeric:tabular-nums}.panel>details{margin-top:18px}.panel>details>summary{cursor:pointer;color:var(--muted);font-size:13px}@media(max-width:1000px){.station-summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.resource-summary{grid-template-columns:1fr}}</style>
