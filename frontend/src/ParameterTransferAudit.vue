<script setup>
import {computed,ref,watch} from 'vue'
import {number} from './api.mjs'
import {regionName} from './state-map.mjs'
import {transferNames} from './parameter-transfer.mjs'
const props=defineProps({audit:Object,definitions:{type:Array,default:()=>[]}})
const selected=ref('')
const regions=computed(()=>Object.entries(props.audit?.effective?.regions||{}))
watch(regions,value=>{if(!value.some(([id])=>id===selected.value))selected.value=value[0]?.[0]||''},{immediate:true})
const region=computed(()=>props.audit?.effective?.regions?.[selected.value])
const sources={'global-base':'共用参数','model-default':'模型默认值','regional-override':'区域单独设置'}
function sourceName(value){
 if(!value)return '来源待核实'
 if(value.startsWith('area-scaled:'))return '面积假设缩放 · '+(sources[value.slice(12)]||value.slice(12))
 return sources[value]||value
}
const matchingUnits=computed(()=>(props.audit?.units||[]).filter(u=>u.partition===selected.value))
</script>
<template>
 <div class="transfer-audit">
  <p>传递方式：{{transferNames[audit?.policy?.mode]||'沿用已有分区参数'}}。以下为后台解析的计算值，原方案设置不被替换。</p>
  <label v-if="regions.length">区域<select v-model="selected"><option v-for="[id] in regions" :key="id" :value="id">{{regionName(id)}}</option></select></label>
  <template v-if="region">
   <p class="note">陆地与冰川单元总面积 {{number(region.areaKm2,2)}} km² · {{matchingUnits.length}} 个计算单元<span v-if="region.routingScaleApplied"> · 汇流尺度系数 {{number(region.routingScale,4)}}</span>。<span v-if="region.routingScaleApplied">面积缩放是等形、等流速假设，并非已验证规律。</span></p>
   <div class="table-wrap"><table><thead><tr><th>参数</th><th>实际计算值</th><th>单位</th><th>来源</th><th>缩放 / 范围限制</th></tr></thead><tbody><tr v-for="d in definitions" :key="d.key"><td>{{d.label}}</td><td>{{number(region.parameters?.[d.key],6)}}</td><td>{{d.unit||'无量纲'}}</td><td>{{sourceName(region.sources?.[d.key])}}</td><td><template v-if="region.routing?.[d.key]&&region.routingScaleApplied">基准 {{number(region.routing[d.key].donorValue,4)}} → {{number(region.routing[d.key].unclampedValue,4)}}<span v-if="region.routing[d.key].clamped">，超界后取 {{number(region.routing[d.key].value,4)}}</span></template><template v-else>—</template></td></tr></tbody></table></div>
   <details><summary>查看计算单元与参数组对应关系</summary><div class="table-wrap"><table><thead><tr><th>单元</th><th>所属区域</th><th>参数组</th></tr></thead><tbody><tr v-for="u in matchingUnits" :key="u.id"><td>{{u.id}}</td><td>{{regionName(u.partition)}}</td><td>{{u.parameterGroup}}</td></tr></tbody></table></div></details>
  </template>
  <p v-else class="note">当前成果没有可读取的分区参数审计。</p>
  <details v-if="audit?.assumptions?.length||audit?.structuralNotes?.length"><summary>查看适用条件与限制</summary><p v-for="(item,i) in audit.assumptions" :key="'a'+i" class="note">{{item}}</p><p v-for="(item,i) in audit.structuralNotes" :key="'s'+i" class="note">{{item.explanation}}</p></details>
 </div>
</template>
<style scoped>.transfer-audit{padding:12px 0}.transfer-audit>label{max-width:320px}.transfer-audit table{min-width:720px}</style>
