<script setup>
import {ref} from 'vue'
import ScreenMap from './ScreenMap.vue'
defineProps({stations:{type:Array,default:()=>[]}})
const emit=defineEmits(['station'])
const selected=ref(null)
</script>
<template><div class="lake-screen basin-reference basin-map"><ScreenMap variable="basin" :focus="selected?.id" :stations="stations.map(s=>({...s,located:s.lon!=null&&s.lat!=null}))" :show-stations="true" :show-ice="true" :show-rivers="true" :show-terrain="true" :show-names="true" :show-control="false" @feature="selected=$event" @select="emit('station',$event)"/><p v-if="selected" class="note">{{selected.name}} · {{selected.areaKm2?.toFixed(2)}} km²</p></div></template>
