<script setup>
import {ref,watch,onMounted,onBeforeUnmount} from 'vue'
import * as echarts from 'echarts/core'
import {LineChart,BarChart} from 'echarts/charts'
import {GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,MarkLineComponent} from 'echarts/components'
import {CanvasRenderer} from 'echarts/renderers'
import {chartState,saveChartState} from './chart-state.mjs'
echarts.use([LineChart,BarChart,GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,MarkLineComponent,CanvasRenderer])
const props=defineProps({rows:{default:()=>[]},series:{default:()=>[]},unit:{default:''},context:{default:''},compact:Boolean,dark:Boolean,day:String,zero:Boolean,hourly:Boolean})
const emit=defineEmits(['day'])
const root=ref();let chart,observer,memory
const memoryKey=()=>props.context||props.series.map(s=>s.key+':'+s.label).join('|')
function render(){
 if(!chart)return
 memory=chartState(props.context||props.series.map(s=>s.key+':'+s.label).join('|'))
 const ink=props.dark?'#bdd0dc':'#516e7b',grid=props.dark?'#294450':'#e0e9ed'
 const selected=Object.fromEntries(props.series.map(s=>[s.label,memory.selected[s.label]!==false]))
 chart.setOption({animation:false,color:['#208c96','#ce9041','#6486bd','#8da873','#ae81a6'],textStyle:{fontFamily:'Microsoft YaHei UI, Microsoft YaHei, sans-serif',fontSize:12,color:ink},
 tooltip:{trigger:'axis',confine:true,backgroundColor:props.dark?'#132d3a':'#fff',borderColor:grid,textStyle:{color:ink,fontSize:13},valueFormatter:v=>v==null?'缺测':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3})},
 legend:{type:'scroll',top:4,left:4,right:4,itemWidth:16,itemHeight:8,textStyle:{color:ink,fontSize:12},selected},
 grid:{left:8,right:16,top:props.compact?43:64,bottom:props.compact?20:64,containLabel:true},
 xAxis:{type:'category',boundaryGap:props.series.some(s=>s.type==='bar'),data:props.rows.map(r=>(r.date||r.time||'').slice(0,props.hourly?16:10)),axisLabel:{color:ink,fontSize:12,hideOverlap:true},axisLine:{lineStyle:{color:grid}},axisTick:{show:false}},
 yAxis:{type:'value',name:props.unit,nameLocation:'end',nameGap:12,nameTextStyle:{color:ink,fontSize:12,align:'left'},scale:!props.zero,axisLabel:{color:ink,fontSize:12,formatter:v=>Math.abs(v)>=1e8?(v/1e8)+'亿':Math.abs(v)>=1e4?(v/1e4)+'万':String(v)},splitNumber:props.compact?3:5,splitLine:{lineStyle:{color:grid,type:'dashed'}}},
 dataZoom:props.compact?[]:[{type:'inside',start:memory.zoom[0],end:memory.zoom[1]},{type:'slider',height:18,bottom:6,start:memory.zoom[0],end:memory.zoom[1],borderColor:grid}],
 series:props.series.map((s,i)=>({id:s.id||s.key,name:s.label,type:s.type||'line',data:props.rows.map(r=>r[s.key]??null),showSymbol:false,connectNulls:false,lineStyle:{width:2,type:s.observed||s.key.startsWith('observed')?'dashed':'solid'},barMaxWidth:16,...s.options,...(i===0&&props.day?{markLine:{symbol:'none',silent:true,label:{show:false},lineStyle:{color:'#bc9251',type:'dashed'},data:[{xAxis:props.day}]}}:{})}))
 },{replaceMerge:['series','dataZoom']})
}
onMounted(()=>{chart=echarts.init(root.value);const remember=e=>{Object.assign(memory.selected,e.selected);saveChartState(memoryKey(),memory)};for(const event of ['legendselectchanged','legendselected','legendunselected'])chart.on(event,remember);chart.on('datazoom',()=>{const z=chart.getOption().dataZoom?.[0];if(z)memory.zoom=[z.start,z.end];saveChartState(memoryKey(),memory)});chart.on('click',e=>{if(e.name)emit('day',e.name.slice(0,props.hourly?16:10))});observer=new ResizeObserver(()=>chart.resize());observer.observe(root.value);render()})
watch(()=>[props.rows,props.series,props.unit,props.context,props.day,props.dark,props.hourly],render,{deep:true})
onBeforeUnmount(()=>{observer?.disconnect();chart?.dispose()})
</script>
<template><div class="chart-container" :class="{compact}"><div ref="root" class="chart" role="img" :data-day="day||undefined" :aria-label="series.map(s=>s.label).join('、')+'过程图'+(day?' · '+day:'')"></div><span v-if="!rows.length" class="chart-empty">该时段无资料</span></div></template>
