<script setup>
import {ref,watch,onMounted,onBeforeUnmount} from 'vue'
import * as echarts from 'echarts/core'
import {LineChart,BarChart} from 'echarts/charts'
import {GridComponent,TooltipComponent,LegendComponent,DataZoomComponent} from 'echarts/components'
import {CanvasRenderer} from 'echarts/renderers'
echarts.use([LineChart,BarChart,GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,CanvasRenderer])
const props=defineProps({rows:{default:()=>[]},series:{default:()=>[]},unit:{default:''}})
const root=ref();let chart,observer
function render(){if(!chart)return;chart.setOption({animation:false,color:['#187c75','#d79a40','#527bb7','#a66fa5','#7f9f54'],tooltip:{trigger:'axis',valueFormatter:v=>v==null?'缺测':Number(v).toFixed(3)},legend:{top:0,textStyle:{color:'#536c73'}},grid:{left:68,right:25,top:48,bottom:65},xAxis:{type:'category',data:props.rows.map(r=>(r.date||r.time||'').slice(0,10)),axisLabel:{color:'#819197'},axisLine:{lineStyle:{color:'#dce6e8'}}},yAxis:{type:'value',name:props.unit,scale:true,splitLine:{lineStyle:{color:'#eef2f3'}}},dataZoom:[{type:'inside'},{type:'slider',height:18,bottom:6,borderColor:'#e1e9e9',fillerColor:'#187c7522'}],series:props.series.map(s=>({name:s.label,type:s.type||'line',data:props.rows.map(r=>r[s.key]??null),showSymbol:false,connectNulls:false,lineStyle:{width:1.8},...s.options}))},true)}
onMounted(()=>{chart=echarts.init(root.value);observer=new ResizeObserver(()=>chart.resize());observer.observe(root.value);render()});watch(()=>[props.rows,props.series,props.unit],render,{deep:true});onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div v-if="!rows.length" class="empty">当前没有可展示的过程资料</div><div ref="root" class="chart" :class="{hidden:!rows.length}" role="img" :aria-label="series.map(s=>s.label).join('、')+'过程图'"></div></template>
