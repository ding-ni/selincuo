<script setup>
import {computed} from 'vue'
import {monitoringWindow} from './dashboard.mjs'

const props=defineProps({station:Object,day:String,days:{type:Number,default:90},compact:Boolean})
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
</script>

<template>
 <div class="monitoring-trend" :class="{'monitoring-trend-compact':compact}" role="img" :aria-label="(station?.name||'站点')+'最近'+days+'日实测过程'">
  <svg v-if="valid.length" viewBox="0 0 240 80" preserveAspectRatio="none" aria-hidden="true">
   <path class="monitoring-trend-grid" d="M0 8H240 M0 40H240 M0 72H240"/>
   <path class="monitoring-trend-line" :d="geometry.path"/>
   <circle v-for="(point,i) in geometry.isolated" :key="i" :cx="point.x" :cy="point.y" r="2" class="monitoring-trend-point"/>
  </svg>
  <span v-else>该时段无有效观测</span>
 </div>
</template>
