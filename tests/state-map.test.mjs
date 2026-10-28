import test from 'node:test'
import assert from 'node:assert/strict'
import {objectUnitIds,selectedGeometry,geometryBounds,unitName} from '../frontend/src/state-map.mjs'

test('unit, river, partition and lake selection retain exact multi-piece geometry',()=>{
 const units=[{id:'a',partition:'duja',basin:'duja',elevationBandMin:4500,elevationBandMax:5000,kind:'land'},
 {id:'b',partition:'zajia_rest',basin:'zajia'},{id:'c',partition:'unresolved',basin:'unresolved'}]
 assert.deepEqual(objectUnitIds(units,{scope:'river',id:'zajia'}),['a','b'])
 assert.deepEqual(objectUnitIds(units,{scope:'river',id:'total'}),['a','b'])
 assert.deepEqual(objectUnitIds(units,{scope:'partition',id:'duja'}),['a'])
 const feature={properties:{id:'a'},geometry:{type:'MultiPolygon',coordinates:[[[[1,2],[2,3],[1,2]]],[[[4,5],[6,7],[4,5]]]]}}
 const lake={properties:{id:'lake'},geometry:{type:'Polygon',coordinates:[[[3,3],[4,4],[3,3]]]}}
 assert.deepEqual(selectedGeometry([feature],[lake],units,{scope:'unit',id:'a'}),[feature])
 assert.deepEqual(geometryBounds([feature]),[1,2,6,7])
 assert.deepEqual(selectedGeometry([feature],[lake],units,{scope:'lake',id:'lake'}),[lake])
 assert.equal(unitName(units[0]).includes('AGG'),false)
})
