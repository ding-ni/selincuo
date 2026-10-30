import test from 'node:test'
import assert from 'node:assert/strict'
import {chartState,centeredZoom,dateIndex,rangeContains,rowKey,isolatedObservation} from '../frontend/src/chart-state.mjs'

test('each graph has independent horizontal, vertical and legend state',()=>{
 const lake=chartState('test:lake'),flow=chartState('test:flow')
 lake.zoom=[15,35];lake.yRange=[4540,4541];lake.selected['湖位']=false
 assert.deepEqual(flow.zoom,[0,100]);assert.equal(flow.yRange,null);assert.deepEqual(flow.selected,{})
 assert.equal(chartState('test:lake'),lake)
})

test('isolated valid observations remain visible without connecting gaps',()=>{
 const values=[null,4540,null,null,4541,4542,null]
 assert.equal(isolatedObservation(values,1),true)
 assert.equal(isolatedObservation(values,4),false)
 assert.equal(isolatedObservation(values,5),false)
 assert.equal(isolatedObservation(values,0),false)
})

test('locating the real selected date preserves the window width and its date',()=>{
 const rows=[{date:'2021-01-01'},{date:'2022-01-01'},{date:'2023-01-01'},{date:'2024-01-01'},{date:'2025-01-01'}]
 assert.equal(dateIndex(rows,'2024-01-01'),3)
 assert.equal(rangeContains(3,rows.length,[0,25]),false)
 assert.deepEqual(centeredZoom(3,rows.length,[0,25]),[62.5,87.5])
 assert.deepEqual(centeredZoom(4,rows.length,[0,25]),[75,100])
 assert.equal(rangeContains(dateIndex(rows,'2024-01-01'),rows.length,centeredZoom(3,rows.length,[0,25])),true)
 assert.equal(dateIndex(rows,'2026-01-01'),-1)
 assert.equal(rowKey({time:'2024-01-01T12:00:00'},true),'2024-01-01T12:00')
})
