<script setup>
import {computed} from 'vue'
import {comparisonDifferences} from './diagnostics.mjs'
const props=defineProps({current:Object,other:Object,currentRequest:Object,otherRequest:Object,year:String,definitions:Array})
const differences=computed(()=>{
 const rows=comparisonDifferences(props.current,props.other)
 const left=props.currentRequest?.stateId||'统一新起算',right=props.otherRequest?.stateId||'统一新起算'
 return [...rows,{label:'起算状态',left,right,status:left===right?'same':'different'}]
})
const parameters=computed(()=>Object.keys(props.current?.parameters||{}).filter(k=>props.current.parameters[k]!==props.other?.parameters?.[k]).map(k=>({key:k,label:props.definitions?.find(d=>d.key===k)?.label||k,left:props.current.parameters[k],right:props.other?.parameters?.[k]})))
const left=computed(()=>props.current?.metrics?.[props.year]),right=computed(()=>props.other?.metrics?.[props.year])
const n=(v,d=3)=>v==null?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:d})
const short=v=>v.length>32?v.slice(0,16)+'…'+v.slice(-6):v
</script>
<template>
 <section v-if="other" class="comparison-audit">
  <h4>同年双目标检查 <small>{{year}} · 各成果自身匹配样本</small></h4><div class="table-wrap"><table><thead><tr><th>评价项目</th><th>当前成果</th><th>对比成果</th></tr></thead><tbody><tr><td>杜加匹配天数</td><td>{{n(left?.flow.count,0)}}</td><td>{{n(right?.flow.count,0)}}</td></tr><tr><td>杜加 NSE / log1p NSE</td><td>{{n(left?.flow.nse)}} / {{n(left?.flow.logNse)}}</td><td>{{n(right?.flow.nse)}} / {{n(right?.flow.logNse)}}</td></tr><tr><td>杜加水量偏差 %</td><td>{{n(left?.flow.pbias,2)}}</td><td>{{n(right?.flow.pbias,2)}}</td></tr><tr><td>湖位匹配天数</td><td>{{n(left?.lake.count,0)}}</td><td>{{n(right?.lake.count,0)}}</td></tr><tr><td>条件湖位 RMSE / MAE · m</td><td>{{n(left?.lake.rmse,4)}} / {{n(left?.lake.mae,4)}}</td><td>{{n(right?.lake.rmse,4)}} / {{n(right?.lake.mae,4)}}</td></tr></tbody></table></div>
  <p class="note">同时检查流量、低流量、水量偏差与湖位。不同起止、状态或观测快照可能改变匹配样本；表中指标各自计算，不据此自动排名。2025为留出评价，不能用于反选2024参数。</p>
  <h4>条件核对 <small>模型、资料与结构差异</small></h4><div class="table-wrap"><table><thead><tr><th>条件</th><th>当前成果</th><th>对比成果</th><th>核对</th></tr></thead><tbody><tr v-for="d in differences" :key="d.label"><td>{{d.label}}</td><td :title="d.left">{{short(d.left)}}</td><td :title="d.right">{{short(d.right)}}</td><td><span :class="['comparison-status',d.status]">{{d.status==='same'?'一致':d.status==='different'?'有差异':'缺少记录'}}</span></td></tr></tbody></table></div>
  <h4>变更参数 <small>{{parameters.length}} 项</small></h4><div class="table-wrap"><table><thead><tr><th>参数</th><th>当前成果</th><th>对比成果</th></tr></thead><tbody><tr v-for="p in parameters" :key="p.key"><td>{{p.label}} <small>{{p.key}}</small></td><td>{{n(p.left,6)}}</td><td>{{n(p.right,6)}}</td></tr><tr v-if="!parameters.length"><td colspan="3">数值参数一致；仍需核对模型、结构和起算条件。</td></tr></tbody></table></div>
 </section><p v-else class="note">选择一份对比成果，可同时检查流量、湖位以及计算条件的差异。</p>
</template>
<style scoped>
.comparison-audit h4{margin:22px 0 12px}.comparison-audit small{font-size:11px;color:#7d9096;font-weight:400}.comparison-audit td{font-variant-numeric:tabular-nums;font-size:12px;white-space:nowrap}.comparison-status{padding:3px 8px;font-size:10px;border-radius:3px;background:#ecf4f1;color:#36796a}.comparison-status.different{background:#fcf2df;color:#98752f}.comparison-status.unknown{background:#edf0f2;color:#7f8b90}
</style>
