<script setup>
import {computed,ref,nextTick} from 'vue'
import {monitoringWindow} from './dashboard.mjs'
import Chart from './Chart.vue'

const props=defineProps({station:Object,day:String,days:{type:Number,default:90},compact:Boolean,dark:Boolean})
const expanded=ref(false),dialog=ref()
const rows=computed(()=>monitoringWindow(props.station,props.day,props.days))
const valid=computed(()=>rows.value.filter(row=>row.value!=null))
const geometry=computed(()=>{
 if(!valid.value.length)return {path:'',isolated:[]}
 const values=valid.value.map(row=>row.value),low=Math.min(...values),high=Math.max(...values)
 // Constant water levels still occupy the centre of the graph.
 const span=Math.max(high-low,0.01),bottom=(low+high)/2-span/2
 let previous=false,path='';const isolated=[]
 rows.value.forEach((row,i)=>{
  if(row.value==null){previous=false;return}
  const x=rows.value.length>1?i/(rows.value.length-1)*240:120,y=72-(row.value-bottom)/span*64
  path+=(previous?'L':'M')+x.toFixed(2)+' '+y.toFixed(2)+' '
  if(rows.value[i-1]?.value==null&&rows.value[i+1]?.value==null)isolated.push({x,y})
  previous=true
 })
 return {path,isolated}
})
function enlarge(){expanded.value=true;nextTick(()=>dialog.value?.showModal())}
function close(){dialog.value?.close();expanded.value=false}
</script>

<template>
 <div class="monitoring-trend" :class="{'monitoring-trend-compact':compact}" :aria-label="(station?.name||'站点')+'最近'+days+'日实测过程'">
  <svg v-if="valid.length" viewBox="0 0 240 80" preserveAspectRatio="none" aria-hidden="true">
   <path class="monitoring-trend-grid" d="M0 8H240 M0 40H240 M0 72H240"/>
   <path class="monitoring-trend-line" :d="geometry.path"/>
  </svg>
  <span v-for="(point,i) in geometry.isolated" :key="i" class="monitoring-trend-pixel" :style="{left:(point.x/240*100)+'%',top:(point.y/80*100)+'%'}" aria-hidden="true"></span>
  <span v-if="!valid.length">该时段无有效观测</span>
  <button v-if="valid.length" type="button" class="monitoring-trend-expand" :aria-label="'放大'+(station?.name||'站点')+'实测过程'" @click.stop="enlarge">放大</button>
  <Teleport to="body"><dialog ref="dialog" class="monitoring-dialog" :class="{'monitoring-dialog-dark':dark}" @close="expanded=false"><div class="monitoring-dialog-heading"><strong>{{station?.name}} · 最近{{days}}日实测</strong><button type="button" aria-label="关闭站点放大图" @click="close">关闭</button></div><Chart v-if="expanded" expanded :rows="rows" :series="[{key:'value',label:'实测水位',observed:true,options:{lineStyle:{width:2,type:'solid'}}}]" unit="m" :day="day" :dark="dark" :context="'monitoring-recent:'+station?.id+':'+days"/></dialog></Teleport>
 </div>
</template>

<style scoped>
.monitoring-trend-pixel{position:absolute;width:4px;height:4px;border-radius:50%;background:var(--sc-accent,#65c2c6);transform:translate(-50%,-50%);pointer-events:none}.monitoring-trend-expand{position:absolute;right:3px;top:3px;z-index:1;font-size:10px;line-height:1.2;min-height:19px;padding:2px 5px;border:1px solid var(--sc-line,#cedde2);border-radius:3px;background:var(--sc-panel,#fff);color:var(--sc-ink,#315766);cursor:pointer}.monitoring-trend-compact .monitoring-trend-expand{top:auto;bottom:1px}.monitoring-dialog{width:min(1100px,94vw);height:min(700px,90vh);max-height:none;max-width:none;border:1px solid #b8ced4;border-radius:8px;padding:16px;background:#fff;color:#315766;box-shadow:0 12px 50px #0006}.monitoring-dialog::backdrop{background:#05111cb8}.monitoring-dialog-dark{--sc-panel:#122a39;--sc-line:#294554;--sc-ink:#e1ecf1;--sc-muted:#9bb4c2;background:var(--sc-panel);color:var(--sc-ink)}.monitoring-dialog-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}.monitoring-dialog-heading button{min-height:28px;padding:3px 10px;border:1px solid var(--sc-line,#cedde2);border-radius:4px;background:var(--sc-panel,#fff);color:var(--sc-ink,#315766);cursor:pointer}.monitoring-dialog .chart-expanded{height:calc(100% - 40px)}.monitoring-dialog .chart-expanded :deep(.chart){height:auto;min-height:0}
</style>
