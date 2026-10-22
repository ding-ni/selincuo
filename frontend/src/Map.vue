<script setup>
import {ref} from 'vue'
import ScreenMap from './ScreenMap.vue'
const selected=ref(null)
</script>
<template><div class="lake-screen basin-reference"><ScreenMap variable="basin" :focus="selected?.id" :stations="[]" :show-ice="true" :show-rivers="true" :show-terrain="true" :show-names="true" :show-control="false" @feature="selected=$event"/><p v-if="selected" class="note">{{selected.name}} · {{selected.areaKm2?.toFixed(2)}} km²</p></div></template>
