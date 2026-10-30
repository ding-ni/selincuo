<script setup>
import {ref,computed,watch} from 'vue'
import {api,number,resultName} from './api.mjs'
import {isResultRecord,resultDisplayPeriod} from './result-catalog.mjs'
import PermanentDeletion from './PermanentDeletion.vue'
const props=defineProps({tasks:Array,trashedResults:Array,published:Array,standardId:String,homeId:String,selectedId:String,selectedResult:Object,busy:Boolean})
const emit=defineEmits(['select','metadata','stop','changed'])
const tab=ref('current'),search=ref(''),page=ref(1),compareIds=ref([]),comparisons=ref([]),compareError=ref(''),comparing=ref(false),historyTasks=ref([]),historyError=ref(''),historyLoading=ref(false)
const catalog=computed(()=>{
 const values=[...(props.published||[]).map(t=>({...t,builtin:true,status:'succeeded'})),...(props.tasks||[]),...historyTasks.value,...(props.trashedResults||[])]
 return [...new Map(values.filter(isResultRecord).map(t=>[t.id,t])).values()]
})
const protectedResult=t=>t.builtin||t.id===props.homeId||t.id===props.standardId
const saved=t=>protectedResult(t)||!!t.favorite||!!t.userSaved||!!t.user_saved
const inactive=t=>!['queued','running','stopping'].includes(t.status)
const trashObjects=computed(()=>catalog.value.filter(t=>inactive(t)&&t.trashed)
 .map(t=>({kind:'task',id:t.id,name:t.displayName||resultName(t)})))
const filtered=computed(()=>catalog.value.filter(t=>{
 if(!inactive(t))return false
 if(tab.value==='trash'?!t.trashed:t.trashed)return false
 if(tab.value==='current'&&t.id!==props.homeId)return false
 if(tab.value==='saved'&&(t.id===props.homeId||t.status!=='succeeded'||!saved(t)))return false
 if(tab.value==='temporary'&&t.status==='succeeded'&&saved(t))return false
 return !search.value||(t.name||t.displayName||'').includes(search.value)||t.id.includes(search.value)
}))
const visible=computed(()=>filtered.value.slice((page.value-1)*10,page.value*10))
watch([search,tab],()=>{page.value=1;compareIds.value=[]})
let historyTicket=0
watch(tab,async value=>{
 const ticket=++historyTicket
 if(value==='current'){
  historyLoading.value=false;historyError.value=''
  return
 }
 historyLoading.value=true;historyError.value=''
 try{const rows=(await api('tasks?includeExperiments=true')).tasks||[];if(ticket===historyTicket)historyTasks.value=rows}
 catch(e){if(ticket===historyTicket)historyError.value=e.message}finally{if(ticket===historyTicket)historyLoading.value=false}
})
watch(()=>[tab.value,props.homeId,props.selectedId],([current,home,selected])=>{
 if(current==='current'&&home&&selected!==home)emit('select',home)
},{immediate:true})
const period=t=>resultDisplayPeriod(t,props.selectedResult)
const schemeName=t=>t.request?.schemeName||t.request?.schemeSnapshot?.name||t.scheme?.name||(t.id===props.homeId?'色林错综合模拟方案':t.builtin?'历史方案':'见成果详情')
const typeName=t=>({simulation:'历史模拟',continuation:'续算',trial:'试算',calibration:'率定','two-stage-calibration':'两阶段率定','calibration-replay':'率定复现','calibration-workflow':'率定',sensitivity:'敏感性分析'})[t.kind]||'历史模拟'
const statusName=t=>({failed:'失败',cancelled:'已停止',interrupted:'已中断'})[t.status]||t.status
const exportHref=t=>t.builtin?'/api/model/published-result?'+new URLSearchParams({id:t.id,file:'daily.csv'}):'/api/model/export?'+new URLSearchParams({taskId:t.id,format:'daily.csv'})
function rename(t,save=false){const value=prompt(save?'保存成果名称':'成果名称',t.name||t.displayName||resultName(t));if(value?.trim())emit('metadata',t,{name:value.trim(),...(save?{favorite:true}:{})})}
let ticket=0
watch(compareIds,async ids=>{
 const current=++ticket;comparisons.value=[];compareError.value='';if(ids.length<2)return
 comparing.value=true
 try{const values=await Promise.all(ids.map(id=>api('result-summary?resultId='+encodeURIComponent(id))));if(current===ticket)comparisons.value=values}
 catch(e){if(current===ticket)compareError.value=e.message}finally{if(current===ticket)comparing.value=false}
},{deep:true})
const compareYears=computed(()=>[...new Set(comparisons.value.flatMap(s=>Object.keys(s.evaluation?.annual||{})))].sort())
const compareDifferences=computed(()=>{
 if(comparisons.value.length<2)return[]
 return [['初始化方式',s=>s.evaluation?.mode],['气象资料',s=>s.inputId],['观测质量规则',s=>s.evaluation?.qualityProfile],['入湖范围',s=>s.inflowComplete===false?'verified-only':'other']]
  .filter(([,read])=>new Set(comparisons.value.map(read)).size>1).map(([name])=>name)
})
</script>
<template>
 <section class="panel">
  <div class="panel-title"><h3>成果中心</h3><input v-if="tab!=='current'" type="search" v-model="search" placeholder="查找成果名称或编号"></div>
  <nav class="workspace-tabs"><button v-for="t in [['current','当前成果'],['saved','历史成果'],['temporary','试算与计算'],['trash','回收站']]" :key="t[0]" :class="{active:tab===t[0]}" @click="tab=t[0]">{{t[1]}}</button></nav>
  <p v-if="historyLoading" class="muted">正在读取历史记录…</p><p v-if="historyError" class="warning">{{historyError}}</p>
  <PermanentDeletion :objects="tab==='trash'?trashObjects:[]" :busy="busy" @changed="emit('changed',$event)">
   <template #rows="{isSelected,toggle}">
  <div class="table-wrap"><table><thead><tr><th v-if="tab!=='current'">{{tab==='trash'?'选择':'比较'}}</th><th>成果名称</th><th>方案</th><th>时段</th><th>类型 / 保存时间</th><th>操作</th></tr></thead><tbody>
   <tr v-for="t in visible" :key="t.id" :class="{selected:t.id===selectedId}">
    <td v-if="tab!=='current'"><input v-if="tab==='trash'" type="checkbox" :checked="isSelected({kind:'task',id:t.id})" :disabled="busy" :aria-label="'选择 '+resultName(t)" @change="toggle({kind:'task',id:t.id})"><input v-else-if="t.status==='succeeded'" type="checkbox" v-model="compareIds" :value="t.id" :disabled="!compareIds.includes(t.id)&&compareIds.length>=3" :aria-label="'比较 '+resultName(t)"></td>
    <td><button v-if="tab!=='trash'" class="link" @click="emit('select',t.id)">{{t.displayName||resultName(t)}}</button><span v-else>{{t.displayName||resultName(t)}}</span><span v-if="protectedResult(t)" class="badge">{{t.id===homeId?'当前成果':'历史成果'}}</span></td>
    <td>{{schemeName(t)}}</td><td class="period">{{period(t)}}</td><td>{{typeName(t)}}<span v-if="t.status!=='succeeded'"> · {{statusName(t)}}</span><small class="block time">{{(t.created_at||t.publishedAt||'').slice(0,16).replace('T',' ')||'内置成果'}}</small></td>
    <td><template v-if="tab==='trash'"><button :disabled="busy" @click="emit('metadata',t,{trashed:false})">恢复</button><button v-if="t.favorite" :disabled="busy" @click="emit('metadata',t,{favorite:false})">取消收藏</button></template><template v-else><button v-if="!protectedResult(t)" :disabled="busy" @click="rename(t,tab==='temporary'&&t.status==='succeeded')">{{tab==='temporary'&&t.status==='succeeded'?'命名保存':'重命名'}}</button><a v-if="t.status==='succeeded'" class="button result-export" :href="exportHref(t)">导出</a><button v-if="!protectedResult(t)" :disabled="busy" @click="emit('metadata',t,{trashed:true})">移入回收站</button></template></td>
   </tr></tbody></table></div>
   </template>
  </PermanentDeletion>
  <p v-if="!filtered.length" class="muted">{{tab==='trash'?'回收站为空。':tab==='temporary'?'暂无临时计算成果。':'暂无符合条件的成果。'}}</p>
  <div v-if="filtered.length>10" class="pagination"><button :disabled="page===1" @click="page--">上一页</button><span>{{page}} / {{Math.ceil(filtered.length/10)}}</span><button :disabled="page*10>=filtered.length" @click="page++">下一页</button></div>
  <div v-if="compareIds.length>=2" class="result-compare"><h3>成果比较</h3><p v-if="comparing">正在读取成果…</p><p v-if="compareError" class="warning">{{compareError}}</p>
   <template v-if="comparisons.length"><p class="note">{{compareDifferences.length?'以下条件不同：'+compareDifferences.join('、')+'，指标不可直接排序。':'请结合评价日期和观测范围解读指标。'}}</p>
    <div class="table-wrap"><table><thead><tr><th>成果 / 年份</th><th>湖位 RMSE / m</th><th>湖位日数</th><th>杜加 NSE</th><th>流量偏差 / %</th><th>流量日数</th></tr></thead><tbody><template v-for="s in comparisons" :key="s.id"><tr v-for="y in compareYears" :key="s.id+y"><td>{{s.name}} · {{y}}</td><td>{{number(s.evaluation?.annual?.[y]?.lake?.rmse,4)}}</td><td>{{s.evaluation?.annual?.[y]?.lake?.count??'—'}}</td><td>{{number(s.evaluation?.annual?.[y]?.flow?.nse,4)}}</td><td>{{number(s.evaluation?.annual?.[y]?.flow?.pbias,2)}}</td><td>{{s.evaluation?.annual?.[y]?.flow?.count??'—'}}</td></tr></template></tbody></table></div>
   </template>
  </div>
 </section>
 <slot v-if="selectedId&&visible.some(t=>t.id===selectedId)"/>
</template>
<style scoped>.result-export{font-size:13px;min-height:32px;padding:5px 9px;margin-right:8px}.result-compare{border-top:1px solid var(--line);margin-top:24px;padding-top:20px}.block{display:block}.period,.time{white-space:nowrap}</style>
