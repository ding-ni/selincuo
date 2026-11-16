<script setup>
import {ref,watch,onMounted,onBeforeUnmount} from 'vue'
import {waterState,toLake,lonLatToEast,DEPTH_CLASSES} from './overflow-data.mjs'
const props=defineProps({data:Object,frame:Object,risk:{type:Boolean,default:true},preset:{type:String,default:'overview'},flow:{type:Boolean,default:true}})
const host=ref(),labels=ref([]),tip=ref(null),exaggeration=ref(3),error=ref('')
let THREE,OrbitControls,renderer,scene,camera,controls,clock,observer,raf=0,disposed=false,needsRender=true
let terrain,siling,bange,silingTex,bangeTex,channel,channelCurve,particles,particleT,ring,beacon,tween=null,water=null,lastRadius=-1
let sizeKm=[1,1],base=0
const RGB=DEPTH_CLASSES.map(([d,hex])=>[d,[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16))])
const W=()=>props.data.meta.east.width,H=()=>props.data.meta.east.height
function heightAt(u,v){const w=W(),h=H(),hs=props.data.heights,c=Math.min(w-1,Math.max(0,u*(w-1))),r=Math.min(h-1,Math.max(0,v*(h-1))),c0=Math.floor(c),r0=Math.floor(r),c1=Math.min(w-1,c0+1),r1=Math.min(h-1,r0+1),fc=c-c0,fr=r-r0
 return hs[r0*w+c0]*(1-fc)*(1-fr)+hs[r0*w+c1]*fc*(1-fr)+hs[r1*w+c0]*(1-fc)*fr+hs[r1*w+c1]*fc*fr}
const toY=dem=>(dem-base)*exaggeration.value/1000
function point(lon,lat,lift=0){const [u,v]=lonLatToEast(props.data.meta,lon,lat);return new THREE.Vector3((u-.5)*sizeKm[0],toY(heightAt(u,v))+lift,(v-.5)*sizeKm[1])}
function surface(dem){return toY(dem)+.004}
function waterPlane(tex){const g=new THREE.PlaneGeometry(sizeKm[0],sizeKm[1]);g.rotateX(-Math.PI/2);return new THREE.Mesh(g,new THREE.MeshStandardMaterial({map:tex,transparent:true,roughness:.16,metalness:.1,depthWrite:false}))}
function dataTexture(){const w=W(),h=H(),t=new THREE.DataTexture(new Uint8Array(w*h*4),w,h,THREE.RGBAFormat);t.colorSpace=THREE.SRGBColorSpace;t.magFilter=THREE.LinearFilter;t.minFilter=THREE.LinearFilter;t.needsUpdate=true;return t}
function paint(){
 if(!props.frame||!silingTex)return
 water=waterState(props.data,props.frame)
 const w=W(),h=H(),s=silingTex.image.data,b=bangeTex.image.data
 s.fill(0);b.fill(0)
 for(let r=0;r<h;r++)for(let c=0;c<w;c++){const i=r*w+c,o=((h-1-r)*w+c)*4,st=water.state[i]
  if(st===1){s.set([30,104,150,232],o)}
  else if(st===2){const col=props.risk?RGB.find(x=>water.depth[i]<=x[0])[1]:[92,172,212];s.set([...col,240],o)}
  else if(st===3){b.set([34,112,160,232],o)}}
 silingTex.needsUpdate=true;bangeTex.needsUpdate=true
 const m=props.data.meta
 siling.position.y=surface(m.datum?props.frame.lake-m.datum.lakeMinusDemM:0)
 bange.position.y=surface(m.bange.surfaceDemM+(props.frame.bangeRiseM||0))
 const q=Math.max(0,props.frame.outflow||0),radius=q>0?.018+.06*Math.sqrt(Math.min(1,q/120)):0
 if(Math.abs(radius-lastRadius)>.002)buildChannel(radius)
 channel.visible=particles.visible=q>0;ring.visible=beacon.visible=true
 needsRender=true
}
function buildChannel(radius){
 lastRadius=radius
 if(channel){channel.geometry.dispose()}
 const geom=radius>0?new THREE.TubeGeometry(channelCurve,260,radius,8,false):new THREE.BufferGeometry()
 if(channel)channel.geometry=geom
 else{channel=new THREE.Mesh(geom,new THREE.MeshStandardMaterial({color:0x5cc7ea,emissive:0x1d7da3,emissiveIntensity:.55,transparent:true,opacity:.92,roughness:.3}));scene.add(channel)}
}
function buildCurve(){
 const pts=props.data.meta.outlet.channel.map(([lon,lat])=>point(lon,lat,.03))
 channelCurve=new THREE.CatmullRomCurve3(pts,false,'centripetal')
}
function setHeights(){
 const pos=terrain.geometry.attributes.position,hs=props.data.heights
 for(let i=0;i<pos.count;i++)pos.setY(i,toY(hs[i]))
 pos.needsUpdate=true;terrain.geometry.computeVertexNormals()
 buildCurve();lastRadius=-1
 const o=props.data.meta.outlet,top=point(o.lon,o.lat,0)
 ring.position.copy(top).add(new THREE.Vector3(0,.02,0));beacon.position.copy(top).add(new THREE.Vector3(0,.6,0))
 paint()
}
function presetView(name){
 const m=props.data.meta,o=m.outlet
 const views={overview:[point(89.42,31.76),new THREE.Vector3(-14,30,36)],outlet:[point(o.lon,o.lat),new THREE.Vector3(-4.5,5.5,7.5)],bange:[point(89.52,31.745),new THREE.Vector3(-8,11,14)]}
 const [target,offset]=views[name]||views.overview
 tween={t:0,fromP:camera.position.clone(),fromT:controls.target.clone(),toP:target.clone().add(offset),toT:target}
}
function project(){
 if(!camera||!host.value)return
 const m=props.data.meta,rect=host.value.getBoundingClientRect(),items=[['色林错',89.27,31.80,'lake'],['班戈错',89.545,31.755,'lake'],['溃口',m.outlet.lon,m.outlet.lat,'outlet'],...(m.stations['SLC-E']?[['东岸站',...m.stations['SLC-E'],'station']]:[]),...(m.stations.SLC?[['色林错主站',...m.stations.SLC,'station']]:[])]
 labels.value=items.map(([name,lon,lat,kind])=>{const v=point(lon,lat,kind==='outlet'?1.25:.15).project(camera);return {name,kind,x:(v.x+1)/2*rect.width,y:(1-v.y)/2*rect.height,hidden:v.z>1||v.z<-1}})
}
let pointer=null,raycaster
function inspect(event){
 if(!terrain||!water)return
 const r=renderer.domElement.getBoundingClientRect();pointer.set((event.clientX-r.left)/r.width*2-1,-(event.clientY-r.top)/r.height*2+1)
 raycaster.setFromCamera(pointer,camera);const hit=raycaster.intersectObject(terrain)[0]
 if(!hit){tip.value=null;return}
 const u=hit.point.x/sizeKm[0]+.5,v=hit.point.z/sizeKm[1]+.5,c=Math.round(u*(W()-1)),row=Math.round(v*(H()-1)),i=row*W()+c,st=water.state[i]
 const dem=props.data.heights[i],m=props.data.meta
 tip.value={x:event.clientX-r.left,y:event.clientY-r.top,text:st===1?'色林错水域':st===3?'班戈错水域':st===2?'新增淹没 · 水深 '+water.depth[i].toFixed(2)+' m':'陆地',elev:st===3?dem:toLake(m,dem)}
}
function loop(){
 raf=requestAnimationFrame(loop)
 if(document.hidden)return
 const dt=Math.min(.1,clock.getDelta());let active=false
 if(tween){tween.t=Math.min(1,tween.t+dt/0.9);const e=1-Math.pow(1-tween.t,3);camera.position.lerpVectors(tween.fromP,tween.toP,e);controls.target.lerpVectors(tween.fromT,tween.toT,e);if(tween.t>=1)tween=null;active=true}
 if(controls.update())active=true
 const q=Math.max(0,props.frame?.outflow||0)
 if(q>0&&props.flow&&particles.visible){const speed=.035+.22*Math.min(1,q/120),pos=particles.geometry.attributes.position
  for(let k=0;k<particleT.length;k++){particleT[k]=(particleT[k]+speed*dt*(.7+.6*((k*7919)%13)/13))%1;const p=channelCurve.getPointAt(particleT[k]);pos.setXYZ(k,p.x,p.y+.03,p.z)}
  pos.needsUpdate=true;const pulse=(performance.now()/1600)%1;ring.scale.setScalar(1+1.6*pulse);ring.material.opacity=.85*(1-pulse);active=true}
 if(active||needsRender){renderer.render(scene,camera);project();needsRender=false}
}
function sky(){const c=document.createElement('canvas');c.width=2;c.height=256;const g=c.getContext('2d'),grad=g.createLinearGradient(0,0,0,256);grad.addColorStop(0,'#bcd6e6');grad.addColorStop(.55,'#e4eef3');grad.addColorStop(1,'#f4f1ea');g.fillStyle=grad;g.fillRect(0,0,2,256);const t=new THREE.CanvasTexture(c);t.colorSpace=THREE.SRGBColorSpace;return t}
onMounted(async()=>{try{
 THREE=await import('three');({OrbitControls}=await import('three/addons/controls/OrbitControls.js'))
 const m=props.data.meta,b=m.east.bounds,lat=(b[1]+b[3])/2
 sizeKm=[(b[2]-b[0])*111.32*Math.cos(lat*Math.PI/180),(b[3]-b[1])*110.95];base=m.east.elevationRange[0]
 scene=new THREE.Scene();scene.background=sky();scene.fog=new THREE.Fog(0xe6eef2,70,170)
 camera=new THREE.PerspectiveCamera(38,1,.05,500)
 renderer=new THREE.WebGLRenderer({antialias:true,powerPreference:'low-power'});renderer.setPixelRatio(Math.min(devicePixelRatio,1.75));renderer.outputColorSpace=THREE.SRGBColorSpace
 host.value.appendChild(renderer.domElement)
 controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.dampingFactor=.08;controls.maxPolarAngle=Math.PI*.47;controls.minDistance=2;controls.maxDistance=140
 controls.addEventListener('change',()=>{needsRender=true})
 scene.add(new THREE.HemisphereLight(0xeaf4ff,0x7a6a55,1.05));const sun=new THREE.DirectionalLight(0xfff1dc,1.9);sun.position.set(-40,32,22);scene.add(sun)
 const texture=await new THREE.TextureLoader().loadAsync(props.data.base+m.east.texture);texture.colorSpace=THREE.SRGBColorSpace;texture.anisotropy=Math.min(8,renderer.capabilities.getMaxAnisotropy())
 const g=new THREE.PlaneGeometry(sizeKm[0],sizeKm[1],W()-1,H()-1);g.rotateX(-Math.PI/2)
 terrain=new THREE.Mesh(g,new THREE.MeshStandardMaterial({map:texture,roughness:1,metalness:0}));scene.add(terrain)
 silingTex=dataTexture();bangeTex=dataTexture();siling=waterPlane(silingTex);bange=waterPlane(bangeTex);scene.add(siling,bange)
 ring=new THREE.Mesh(new THREE.RingGeometry(.22,.3,48).rotateX(-Math.PI/2),new THREE.MeshBasicMaterial({color:0xff9a3c,transparent:true,opacity:.85,side:THREE.DoubleSide,depthWrite:false}));scene.add(ring)
 beacon=new THREE.Mesh(new THREE.CylinderGeometry(.018,.018,1.2,8),new THREE.MeshBasicMaterial({color:0xf07a2a}));scene.add(beacon)
 particleT=new Float32Array(320).map((_,k)=>k/320)
 const pg=new THREE.BufferGeometry();pg.setAttribute('position',new THREE.Float32BufferAttribute(new Float32Array(320*3),3))
 particles=new THREE.Points(pg,new THREE.PointsMaterial({color:0xeefcff,size:.12,transparent:true,opacity:.95,depthWrite:false,blending:THREE.AdditiveBlending}));scene.add(particles)
 raycaster=new THREE.Raycaster();pointer=new THREE.Vector2();clock=new THREE.Clock()
 setHeights()
 const start=point(89.42,31.76);camera.position.copy(start).add(new THREE.Vector3(-14,30,36));controls.target.copy(start);controls.update()
 observer=new ResizeObserver(()=>{if(!host.value)return;const w=host.value.clientWidth,h=host.value.clientHeight;renderer.setSize(w,h);camera.aspect=w/Math.max(1,h);camera.updateProjectionMatrix();needsRender=true});observer.observe(host.value)
 renderer.domElement.addEventListener('pointermove',inspect);renderer.domElement.addEventListener('pointerleave',()=>tip.value=null)
 if(props.preset!=='overview')presetView(props.preset)
 loop()
}catch(e){error.value='三维场景无法加载：'+e.message}})
watch(()=>props.frame,paint,{deep:true})
watch(()=>props.risk,paint)
watch(exaggeration,()=>{if(terrain)setHeights()})
watch(()=>props.preset,v=>{if(camera)presetView(v)})
onBeforeUnmount(()=>{disposed=true;cancelAnimationFrame(raf);observer?.disconnect();controls?.dispose()
 scene?.traverse(o=>{o.geometry?.dispose?.();const mats=Array.isArray(o.material)?o.material:o.material?[o.material]:[];for(const mt of mats){mt.map?.dispose?.();mt.dispose()}})
 silingTex?.dispose();bangeTex?.dispose();scene?.background?.dispose?.();renderer?.dispose();renderer?.domElement?.remove()})
</script>
<template>
<div class="scene">
 <div ref="host" class="scene-canvas"></div>
 <template v-for="l in labels" :key="l.name"><span v-if="!l.hidden" :class="['scene-label',l.kind]" :style="{left:l.x+'px',top:l.y+'px'}">{{l.name}}</span></template>
 <div v-if="tip" class="scene-tip" :style="{left:Math.min(tip.x+14,9999)+'px',top:tip.y+14+'px'}"><b>{{tip.text}}</b><span>{{tip.text.startsWith('班戈错')?'DEM 高程':'相当湖位'}} {{tip.elev.toFixed(2)}} m</span></div>
 <label class="scene-exag">高程夸张<input type="range" min="1" max="8" step=".5" v-model.number="exaggeration"><b>{{exaggeration}}×</b></label>
 <p v-if="error" class="scene-error">{{error}}</p>
</div>
</template>
<style scoped>
.scene{position:relative;width:100%;height:100%;overflow:hidden;background:#e4eef3}
.scene-canvas{position:absolute;inset:0}
.scene-canvas :deep(canvas){display:block;width:100%!important;height:100%!important;cursor:grab}
.scene-canvas :deep(canvas:active){cursor:grabbing}
.scene-label{position:absolute;transform:translate(-50%,-100%);pointer-events:none;white-space:nowrap;font-size:13px;font-weight:600;color:#123b4b;text-shadow:0 0 3px #fff,0 0 6px #fff}
.scene-label.lake{font-size:16px;letter-spacing:.15em;color:#0f4560}
.scene-label.outlet{color:#fff;background:#e0702c;padding:2px 8px;border-radius:10px;text-shadow:none;font-size:12px;box-shadow:0 2px 6px #0003}
.scene-label.station{font-weight:500;font-size:12px;color:#334f5a}
.scene-tip{position:absolute;pointer-events:none;background:#10303dee;color:#fff;padding:7px 10px;border-radius:6px;font-size:12px;display:grid;gap:2px;white-space:nowrap}
.scene-tip span{color:#bcd4dc}
.scene-exag{position:absolute;right:14px;bottom:62px;display:flex;align-items:center;gap:8px;background:#ffffffd9;padding:5px 10px;border-radius:6px;font-size:12px;color:#365563}
.scene-exag input{width:90px;min-height:0}
.scene-error{position:absolute;inset:auto 20px 50% 20px;text-align:center;color:#a23a2c}
</style>
