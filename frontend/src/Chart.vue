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
const root=ref();let chart,observer,memory,pixelRatio=1,ratioMedia,resizeFrame=0
const memoryKey=()=>props.context||props.series.map(s=>s.key+':'+s.label).join('|')
function render(){
 if(!chart)return
 memory=chartState(props.context||props.series.map(s=>s.key+':'+s.label).join('|'))
 const ink=props.dark?'#bdd0dc':'#516e7b',grid=props.dark?'#294450':'#e0e9ed'
 const short=props.compact&&chart.getHeight()<140,tickFont=12
 const selected=Object.fromEntries(props.series.map(s=>[s.label,memory.selected[s.label]!==false]))
 chart.setOption({animation:false,color:['#208c96','#ce9041','#6486bd','#8da873','#ae81a6'],textStyle:{fontFamily:'Microsoft YaHei UI, Microsoft YaHei, sans-serif',fontSize:12,color:ink},
 tooltip:{trigger:'axis',confine:true,backgroundColor:props.dark?'#132d3a':'#fff',borderColor:grid,textStyle:{color:ink,fontSize:13},valueFormatter:v=>v==null?'缺测':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:3})},
 legend:{type:'scroll',top:4,left:'auto',right:8,itemWidth:16,itemHeight:8,textStyle:{color:ink,fontSize:tickFont},selected},
 grid:{left:12,right:30,top:short?28:props.compact?40:64,bottom:short?8:props.compact?20:64,containLabel:true},
 xAxis:{type:'category',boundaryGap:props.series.some(s=>s.type==='bar'),data:props.rows.map(r=>(r.date||r.time||'').slice(0,props.hourly?16:10)),axisLabel:{color:ink,fontSize:tickFont,hideOverlap:true,showMinLabel:true,showMaxLabel:true,alignMinLabel:'left',alignMaxLabel:'right',formatter:value=>props.compact?value.slice(0,7):value},axisLine:{lineStyle:{color:grid}},axisTick:{show:false}},
 yAxis:{type:'value',name:props.unit,nameLocation:'end',nameGap:short?7:12,nameTextStyle:{color:ink,fontSize:tickFont,align:'left'},scale:!props.zero,axisLabel:{color:ink,fontSize:tickFont,hideOverlap:true,formatter:v=>Math.abs(v)>=1e8?(v/1e8)+'亿':Math.abs(v)>=1e4?(v/1e4)+'万':String(v)},splitNumber:short?2:props.compact?3:5,splitLine:{lineStyle:{color:grid,type:'dashed'}}},
 dataZoom:props.compact?[]:[{type:'inside',start:memory.zoom[0],end:memory.zoom[1]},{type:'slider',height:18,bottom:6,start:memory.zoom[0],end:memory.zoom[1],borderColor:grid}],
 series:props.series.map((s,i)=>({id:s.id||s.key,name:s.label,type:s.type||'line',data:props.rows.map(r=>r[s.key]??null),showSymbol:false,connectNulls:false,lineStyle:{width:2,type:s.observed||s.key.startsWith('observed')?'dashed':'solid'},barMaxWidth:16,...s.options,...(i===0&&props.day?{markLine:{symbol:'none',silent:true,label:{show:false},lineStyle:{color:'#bc9251',type:'dashed'},data:[{xAxis:props.day}]}}:{})}))
 },{replaceMerge:['series','dataZoom','legend']})
}
function initialise(){
 pixelRatio=window.devicePixelRatio||1
 chart=echarts.init(root.value,null,{renderer:'canvas',devicePixelRatio:pixelRatio})
 const remember=e=>{Object.assign(memory.selected,e.selected);saveChartState(memoryKey(),memory)}
 for(const event of ['legendselectchanged','legendselected','legendunselected'])chart.on(event,remember)
 chart.on('datazoom',()=>{const z=chart.getOption().dataZoom?.[0];if(z)memory.zoom=[z.start,z.end];saveChartState(memoryKey(),memory)})
 chart.on('click',e=>{if(e.name)emit('day',e.name.slice(0,props.hourly?16:10))})
 render()
}
function watchRatio(){
 ratioMedia?.removeEventListener('change',ratioChanged)
 ratioMedia=window.matchMedia(`(resolution: ${window.devicePixelRatio||1}dppx)`)
 ratioMedia.addEventListener('change',ratioChanged)
}
function ratioChanged(){
 if(!chart)return
 if((window.devicePixelRatio||1)!==pixelRatio){chart.dispose();initialise()}
 watchRatio()
}
function resize(){
 cancelAnimationFrame(resizeFrame)
 resizeFrame=requestAnimationFrame(()=>{if(!chart)return;if((window.devicePixelRatio||1)!==pixelRatio)ratioChanged();else{chart.resize();render()}})
}
onMounted(()=>{initialise();watchRatio();window.addEventListener('resize',resize);observer=new ResizeObserver(resize);observer.observe(root.value)})
watch(()=>[props.rows,props.series,props.unit,props.context,props.day,props.dark,props.hourly],render,{deep:true})
onBeforeUnmount(()=>{observer?.disconnect();window.removeEventListener('resize',resize);ratioMedia?.removeEventListener('change',ratioChanged);cancelAnimationFrame(resizeFrame);chart?.dispose()})
</script>
<template><div class="chart-container" :class="{compact}"><div ref="root" class="chart" role="img" :data-day="day||undefined" :aria-label="series.map(s=>s.label).join('、')+'过程图'+(day?' · '+day:'')"></div><span v-if="!rows.length" class="chart-empty">该时段无资料</span></div></template>
