<script setup>
import {ref,watch,onMounted,onBeforeUnmount} from 'vue'
import {number} from './api.mjs'
const props=defineProps({frame:Object})
const host=ref(null),error=ref(''),point=ref(null),vertical=ref(3)
let THREE,OrbitControls,renderer,scene,camera,controls,raf,observer,terrainMesh,waterMesh,raycaster,pointer
function clear(){for(const mesh of [terrainMesh,waterMesh])if(mesh){scene.remove(mesh);mesh.geometry.dispose();mesh.material.dispose()}terrainMesh=null;waterMesh=null}
function draw(){
 if(!scene||!props.frame)return
 clear();const t=props.frame.terrain,{width:w,height:h}=t,b=t.bounds
 const sx=(b[2]-b[0])*111.195*Math.cos((b[1]+b[3])/2*Math.PI/180)/(w-1),sz=(b[3]-b[1])*111.195/(h-1)
 const zs=t.elevations.flat().filter((_,i)=>t.valid[Math.floor(i/w)][i%w]),base=Math.min(...zs),high=Math.max(...zs),vertices=[],colors=[],water=[],waterIndices=[],indices=[]
 const wet=(y,x)=>t.valid[y][x]&&(t.lakeMask[y][x]||props.frame.depths[y][x]>0)
 for(let y=0;y<h;y++)for(let x=0;x<w;x++){
  const z=t.lakeMask[y][x]?props.frame.level+(props.frame.demOffsetM||0):t.elevations[y][x],c=new THREE.Color().setHSL(.15+(1-(z-base)/Math.max(1,high-base))*.1,.2,.42+(z-base)/Math.max(1,high-base)*.3)
  vertices.push((x-(w-1)/2)*sx,(z-base)/1000*vertical.value,(y-(h-1)/2)*sz);colors.push(c.r,c.g,c.b)
  water.push((x-(w-1)/2)*sx,((t.lakeMask[y][x]?props.frame.level+(props.frame.demOffsetM||0):z+props.frame.depths[y][x])-base)/1000*vertical.value+.006,(y-(h-1)/2)*sz)
  if(x<w-1&&y<h-1){const a=y*w+x,b=a+1,c=a+w,d=c+1;if(t.valid[y][x]&&t.valid[y][x+1]&&t.valid[y+1][x]&&t.valid[y+1][x+1])indices.push(a,c,b,b,c,d);if(wet(y,x)&&wet(y,x+1)&&wet(y+1,x)&&wet(y+1,x+1))waterIndices.push(a,c,b,b,c,d)}
 }
 const geo=new THREE.BufferGeometry();geo.setAttribute('position',new THREE.Float32BufferAttribute(vertices,3));geo.setAttribute('color',new THREE.Float32BufferAttribute(colors,3));geo.setIndex(indices);geo.computeVertexNormals();terrainMesh=new THREE.Mesh(geo,new THREE.MeshStandardMaterial({vertexColors:true,roughness:1,side:THREE.DoubleSide}));scene.add(terrainMesh)
 const wg=new THREE.BufferGeometry();wg.setAttribute('position',new THREE.Float32BufferAttribute(water,3));wg.setIndex(waterIndices);wg.computeVertexNormals();waterMesh=new THREE.Mesh(wg,new THREE.MeshStandardMaterial({color:0x38a7ca,transparent:true,opacity:.72,roughness:.3,side:THREE.DoubleSide}));scene.add(waterMesh)
 if(!camera.userData.positioned){const span=Math.max(w*sx,h*sz);camera.position.set(span*.5,span*.7,span*.7);camera.far=span*20;camera.updateProjectionMatrix();controls.target.set(0,0,0);camera.userData.positioned=true}controls.update()
}
function inspect(event){if(!terrainMesh)return;const r=renderer.domElement.getBoundingClientRect();pointer.set((event.clientX-r.left)/r.width*2-1,-(event.clientY-r.top)/r.height*2+1);raycaster.setFromCamera(pointer,camera);const hits=raycaster.intersectObject(terrainMesh);if(!hits.length){point.value=null;return}const p=hits[0].point,t=props.frame.terrain,w=t.width,h=t.height,b=t.bounds,sx=(b[2]-b[0])*111.195*Math.cos((b[1]+b[3])/2*Math.PI/180)/(w-1),sz=(b[3]-b[1])*111.195/(h-1),x=Math.round(p.x/sx+(w-1)/2),y=Math.round(p.z/sz+(h-1)/2);if(y>=0&&y<h&&x>=0&&x<w)point.value={elevation:t.elevations[y][x],depth:t.lakeMask[y][x]?null:props.frame.depths[y][x],lake:t.lakeMask[y][x]}}
onMounted(async()=>{try{THREE=await import('three');({OrbitControls}=await import('three/addons/controls/OrbitControls.js'));scene=new THREE.Scene();scene.background=new THREE.Color(0xe4eceb);camera=new THREE.PerspectiveCamera(45,1,.01,10000);renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(Math.min(devicePixelRatio,2));host.value.appendChild(renderer.domElement);controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;raycaster=new THREE.Raycaster();pointer=new THREE.Vector2();scene.add(new THREE.HemisphereLight(0xffffff,0x586758,2.2));const sun=new THREE.DirectionalLight(0xffffff,2.1);sun.position.set(-100,200,80);scene.add(sun);observer=new ResizeObserver(()=>{if(!host.value)return;const w=host.value.clientWidth,h=host.value.clientHeight;renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix()});observer.observe(host.value);renderer.domElement.addEventListener('pointermove',inspect);draw();const loop=()=>{raf=requestAnimationFrame(loop);controls.update();renderer.render(scene,camera)};loop()}catch(e){error.value='三维视图不可用：'+e.message}})
watch(()=>props.frame,draw);watch(vertical,draw)
onBeforeUnmount(()=>{cancelAnimationFrame(raf);observer?.disconnect();controls?.dispose();clear();renderer?.domElement.removeEventListener('pointermove',inspect);renderer?.dispose()})
</script>
<template><div class="terrain-view"><div ref="host" class="terrain-canvas"></div><p v-if="error" class="terrain-error">{{error}}</p><div class="terrain-caption"><span>局部地形 · 拖动旋转 / 滚轮缩放</span><label>高程夸张 <input type="range" min="1" max="8" step=".5" v-model.number="vertical">{{vertical}}×</label></div><div v-if="point" class="terrain-inspect">地形 {{number(point.elevation,1)}} m<br>{{point.lake?'原湖区：无湖底水深资料':'新增淹没水深 '+number(point.depth,2)+' m'}}</div></div></template>
<style scoped>.terrain-view{position:relative;border:1px solid var(--border,#b9cccf);border-radius:10px;overflow:hidden}.terrain-canvas{height:460px;min-height:300px}.terrain-canvas:deep(canvas){display:block}.terrain-caption{position:absolute;bottom:0;left:0;right:0;display:flex;justify-content:space-between;gap:15px;align-items:center;padding:10px 15px;background:#163640dd;color:#fff;font-size:12px}.terrain-caption label{display:flex;gap:8px;align-items:center}.terrain-caption input{width:85px}.terrain-inspect{position:absolute;top:12px;right:12px;background:#fffef5ed;color:#163640;padding:10px;border-radius:6px;font-size:12px;line-height:1.7}.terrain-error{position:absolute;top:20px;left:20px;color:#a62e35}</style>
