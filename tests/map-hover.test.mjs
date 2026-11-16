import {test} from 'node:test'
import assert from 'node:assert/strict'
import {mapHover} from '../frontend/src/map-hover.mjs'

test('raw cell retains zero and separates applied values',()=>{
 const value=mapHover('grid',{id:'g',value:0},{frame:{variable:'precipitation',unit:'mm/d',day:'2026-10-20',unitValues:[{id:'u',value:8}]},spatial:{grids:[{id:'g'}],aggregation:[{unitId:'u',members:[{gridIndex:0}]}]}})
 assert.match(value.source,/格点原值/)
 assert.equal(value.rows[0].value,'0 mm/d')
 assert.match(value.rows[1].label,/降水订正后/)
 assert.equal(value.rows[1].value,'8.000 mm/d')
})

test('unit temperature uses one corrected model value and missing stays missing',()=>{
 const value=mapHover('grid',{id:'g',value:-8},{frame:{variable:'temperature',unit:'℃',day:'2026-10-20',unitValuesRepresent:'model-elevation-corrected',unitValues:[{id:'u',value:-3}]},spatial:{grids:[{id:'g'}],aggregation:[{unitId:'u',members:[{gridIndex:0}]}]}})
 assert.match(value.note,/未作高程订正/)
 assert.match(value.rows[1].label,/高程订正后/)
 assert.equal(value.rows[1].value,'-3.000 ℃')
 const unit=mapHover('unit',{id:'u'},{frame:{variable:'temperature',unit:'℃',unitValuesRepresent:'model-elevation-corrected',unitValues:[{id:'u',value:-3}]}})
 assert.match(unit.source,/高程订正后/)
 assert.equal(mapHover('snow',{id:'u'},{snow:{day:'2026-10-20',units:[{id:'u',value:null}]}}).rows[0].value,'缺测')
})
