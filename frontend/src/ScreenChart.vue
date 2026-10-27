<script setup>
import Chart from './Chart.vue'
defineProps({rows:{default:()=>[]},series:{default:()=>[]},unit:{default:''},day:String,dark:Boolean,zero:Boolean,context:String})
const emit=defineEmits(['day'])
</script>
<template><Chart compact :rows="rows" :series="series" :unit="unit" :day="day" :dark="dark" :zero="zero" :context="context" @day="emit('day',$event)"/></template>
