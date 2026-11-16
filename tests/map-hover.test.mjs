import {test} from 'node:test'
import assert from 'node:assert/strict'
import {mapHover} from '../frontend/src/map-hover.mjs'
import {linePath,barRects,formatValue} from '../frontend/src/sparkline.mjs'

test('grid hover shows business value without internal identifiers',()=>{
 const value=mapHover('grid',{id:'88.5,30.1',lon:88.5,lat:30.1,value:0},{frame:{variable:'precipitation',unit:'mm/d',day:'2026-10-20'}})
 assert.equal(value.title,'气象格点')
 assert.equal(value.value,0)
 assert.equal(value.unit,'mm')
 assert.match(value.subtitle,/30\.10°N/)
 assert.doesNotMatch(JSON.stringify(value),/88\.5,30\.1|AGG-|xlsx/)
})

test('unit and snow hover use partition names and keep missing as null',()=>{
 const unit=mapHover('unit',{id:'duja-4500-land',partition:'duja',kind:'land',elevationBandMin:4500,elevationBandMax:5000},{frame:{variable:'temperature',unit:'℃',day:'2026-10-20',unitValues:[{id:'duja-4500-land',value:-3}]}})
 assert.equal(unit.title,'杜加站以上')
 assert.equal(unit.value,-3)
 assert.match(unit.subtitle,/4500–5000 m/)
 const snow=mapHover('snow',{id:'u',partition:'boqu'},{snow:{day:'2026-10-20',units:[{id:'u',value:null}]}})
 assert.equal(snow.value,null)
 assert.equal(formatValue('snow',snow.value),'—')
})

test('sparkline helpers tolerate gaps',()=>{
 assert.equal(linePath([1,null]),null)
 const path=linePath([1,2,null,4]);assert.ok(path.line.startsWith('M'))
 const bars=barRects([0,2,null]);assert.equal(bars.length,3);assert.ok(bars[2].missing);assert.equal(bars[1].y,30-27)
})
