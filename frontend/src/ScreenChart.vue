<script setup>
import {ref,watch,onMounted,onBeforeUnmount} from 'vue'
import * as echarts from 'echarts/core'
import {LineChart,BarChart} from 'echarts/charts'
import {GridComponent,TooltipComponent,LegendComponent,MarkLineComponent} from 'echarts/components'
import {CanvasRenderer} from 'echarts/renderers'
echarts.use([LineChart,BarChart,GridComponent,TooltipComponent,LegendComponent,MarkLineComponent,CanvasRenderer])
const props=defineProps({rows:{default:()=>[]},series:{default:()=>[]},unit:{default:''},day:String,dark:Boolean,zero:Boolean})
const emit=defineEmits(['day'])
const el=ref();let chart,resize
function render(){if(!chart)return;const ink=props.dark?'#9bb4c3':'#78908e',line=props.dark?'#28424d':'#e2ebe9';chart.setOption({animation:false,color:['#289ca4','#d3a354','#7190ba','#94b174','#ad82a8'],textStyle:{fontFamily:'Microsoft YaHei UI',fontSize:10,color:ink},legend:{top:2,left:4,itemWidth:14,itemHeight:6,textStyle:{fontSize:10,color:ink}},tooltip:{trigger:'axis',backgroundColor:props.dark?'#19313d':'#fff',borderColor:line,textStyle:{color:props.dark?'#e1edf0':'#365f64',fontSize:11},valueFormatter:v=>v==null?'缺测':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3})},grid:{left:48,right:14,top:35,bottom:25},xAxis:{type:'category',data:props.rows.map(r=>(r.date||r.time||'').slice(0,10)),axisLabel:{fontSize:9,color:ink,formatter:v=>(props.rows[0]?.date||props.rows[0]?.time||'').slice(0,4)!==(props.rows.at(-1)?.date||props.rows.at(-1)?.time||'').slice(0,4)?v.slice(0,7):v.slice(5)},axisLine:{lineStyle:{color:line}},axisTick:{show:false}},yAxis:{type:'value',name:props.unit,nameTextStyle:{fontSize:9,color:ink},scale:!props.zero,axisLabel:{color:ink,fontSize:9},splitNumber:3,splitLine:{lineStyle:{color:line,type:'dashed'}}},series:props.series.map((s,i)=>({name:s.label,type:s.type||'line',data:props.rows.map(r=>r[s.key]??null),showSymbol:false,connectNulls:false,lineStyle:{width:1.7},areaStyle:i===0&&s.area?{opacity:.07}:undefined,barMaxWidth:8,...(i===0&&props.day?{markLine:{symbol:'none',silent:true,label:{show:false},lineStyle:{color:props.dark?'#dfb66e':'#aa7c39',width:1,type:'dashed'},data:[{xAxis:props.day}]}}:{})}))},true)}
onMounted(()=>{chart=echarts.init(el.value);resize=new ResizeObserver(()=>chart.resize());resize.observe(el.value);chart.on('click',p=>{if(p.name)emit('day',p.name)});render()})
watch(()=>[props.rows,props.series,props.unit,props.day,props.dark],render,{deep:true});onBeforeUnmount(()=>{resize?.disconnect();chart?.dispose()})
</script>
<template><div class="screen-chart" ref="el" role="img" :aria-label="series.map(s=>s.label).join('、')+'日过程图'"></div></template>
