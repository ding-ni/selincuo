<script setup>
import {ref,watch,onMounted,onBeforeUnmount} from 'vue'
import {toLake,toDem,lonLatToEast,DEPTH_CLASSES,roadGridSegments,ROAD_STATES,roadStateLabel,inEast} from './overflow-data.mjs'
const props=defineProps({data:Object,frame:Object,water:Object,risk:{type:Boolean,default:true},preset:{type:[String,Array],default:'overview'},flow:{type:Boolean,default:true},exposures:Object,impact:Object,archivedSurface:Number})
const host=ref(),labels=ref([]),tip=ref(null),exaggeration=ref(3),error=ref('')
let THREE,OrbitControls,renderer,scene,camera,controls,clock,observer,raf=0,disposed=false,needsRender=true
let terrain,siling,bange,silingTex,bangeTex,channel,channelCurve,particles,particleT,particleLane,ring,beacon,tween=null,water=null,lastRadius=-1,flowTex=null,halfWidth=.05,channelLength=1
const N_PARTICLES=1000
let sizeKm=[1,1],base=0,rippleTex=null,roadLines=null,roadHighlight=null,settlementPts=null,highlighted=[]
const RGB=DEPTH_CLASSES.map(([d,hex])=>[d,[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16))])
const W=()=>props.data.meta.east.width,H=()=>props.data.meta.east.height
function heightAt(u,v){const w=W(),h=H(),hs=props.data.heights,c=Math.min(w-1,Math.max(0,u*(w-1))),r=Math.min(h-1,Math.max(0,v*(h-1))),c0=Math.floor(c),r0=Math.floor(r),c1=Math.min(w-1,c0+1),r1=Math.min(h-1,r0+1),fc=c-c0,fr=r-r0
 return hs[r0*w+c0]*(1-fc)*(1-fr)+hs[r0*w+c1]*fc*(1-fr)+hs[r1*w+c0]*(1-fc)*fr+hs[r1*w+c1]*fc*fr}
const toY=dem=>(dem-base)*exaggeration.value/1000
function point(lon,lat,lift=0){const [u,v]=lonLatToEast(props.data.meta,lon,lat);return new THREE.Vector3((u-.5)*sizeKm[0],toY(heightAt(u,v))+lift,(v-.5)*sizeKm[1])}
function surface(dem){return toY(dem)+.004}
function ripples(){
 // Tileable wind-ripple normal map: sum of a few periodic waves, encoded as RGB normals.
 const n=256,c=document.createElement('canvas');c.width=c.height=n;const g=c.getContext('2d'),img=g.createImageData(n,n),T=2*Math.PI/n
 const waves=[[3,1,.9,.3],[1,4,.6,1.7],[5,-2,.35,2.4],[-2,6,.3,.9],[7,3,.18,4.1]]
 for(let y=0;y<n;y++)for(let x=0;x<n;x++){let dx=0,dy=0;for(const [kx,ky,a,ph] of waves){const d=Math.cos((kx*x+ky*y)*T+ph)*a;dx+=d*kx;dy+=d*ky}
  const nx=-dx*.05,ny=-dy*.05,l=Math.hypot(nx,ny,1),o=(y*n+x)*4;img.data[o]=(nx/l*.5+.5)*255;img.data[o+1]=(ny/l*.5+.5)*255;img.data[o+2]=(1/l*.5+.5)*255;img.data[o+3]=255}
 g.putImageData(img,0,0);const t=new THREE.CanvasTexture(c);t.wrapS=t.wrapT=THREE.RepeatWrapping;t.repeat.set(sizeKm[0]/1.6,sizeKm[1]/1.6);return t}
function waterPlane(tex){const g=new THREE.PlaneGeometry(sizeKm[0],sizeKm[1]);g.rotateX(-Math.PI/2)
 return new THREE.Mesh(g,new THREE.MeshStandardMaterial({map:tex,normalMap:rippleTex,normalScale:new THREE.Vector2(.55,.55),transparent:true,roughness:.12,metalness:.25,depthWrite:false}))}
function removeMesh(o){if(o){scene.remove(o);o.traverse(x=>{x.geometry?.dispose();x.material?.dispose?.()})}}
function ribbon(pos,idx,a,b,half){
 const dx=b.x-a.x,dz=b.z-a.z,l=Math.hypot(dx,dz)||1,nx=-dz/l*half,nz=dx/l*half,v=pos.length/3
 pos.push(a.x+nx,a.y,a.z+nz,a.x-nx,a.y,a.z-nz,b.x+nx,b.y,b.z+nz,b.x-nx,b.y,b.z-nz)
 idx.push(v,v+1,v+2,v+1,v+3,v+2)
}
function roadMesh(pos,idx,material,colors){
 const g=new THREE.BufferGeometry()
 g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));g.setIndex(idx)
 if(colors)g.setAttribute('color',new THREE.Float32BufferAttribute(colors,3))
 return new THREE.Mesh(g,new THREE.MeshBasicMaterial({side:THREE.DoubleSide,...material}))
}
function buildExposures(){
 for(const o of [roadLines,roadHighlight,settlementPts])removeMesh(o)
 roadLines=roadHighlight=settlementPts=null
 const ex=props.exposures;if(!ex||!THREE)return
 const pos=[],idx=[],grid=props.data.meta.east
 const mapped=roadGridSegments(ex,grid)
 for(const {road,coords:[from,to]} of mapped.segments){
  const half=road.cls==='trunk'||road.cls==='primary'?.05:.025
  ribbon(pos,idx,point(...from,.012),point(...to,.012),half)
 }
 if(pos.length){roadLines=roadMesh(pos,idx,{color:0xe8dbc0});scene.add(roadLines)}
 // Settlements as small 3D pins.
 if(mapped.settlements.length){settlementPts=new THREE.Group()
  for(const {s} of mapped.settlements){const p=point(s.lon,s.lat,0),pin=new THREE.Group()
   const stem=new THREE.Mesh(new THREE.CylinderGeometry(.012,.012,.5,6),new THREE.MeshBasicMaterial({color:0x5b4636}));stem.position.y=.25
   const head=new THREE.Mesh(new THREE.SphereGeometry(.09,16,12),new THREE.MeshStandardMaterial({color:0xffffff,emissive:0x333333,roughness:.4}));head.position.y=.55
   pin.add(stem,head);pin.position.copy(p);pin.userData.id=s.id;settlementPts.add(pin)}
  scene.add(settlementPts)}
 colourExposures()}
function colourExposures(){
 const m=props.data.meta,hitSettle=new Set((props.impact?.settlements||[]).map(s=>s.id))
 removeMesh(roadHighlight);roadHighlight=null
 highlighted=(props.impact?.roads||[]).filter(r=>inEast(m,...r.mid))
 if(highlighted.length){
  const pos=[],idx=[],col=[],colors=Object.fromEntries(ROAD_STATES.map(([key,color])=>[key,new THREE.Color(color)]))
  for(const r of highlighted){
   const [from,to]=r.coords,a=point(...from,.035),b=point(...to,.035)
   const dem=props.archivedSurface??(water?.state[r.cell]===4?m.bange.surfaceDemM+(props.frame.bangeRiseM||0):toDem(m,props.frame.lake))
   a.y=Math.max(a.y,surface(dem)+.025);b.y=Math.max(b.y,surface(dem)+.025)
   ribbon(pos,idx,a,b,.095)
   const c=colors[r.status];for(let k=0;k<4;k++)col.push(c.r,c.g,c.b)
  }
  roadHighlight=roadMesh(pos,idx,{vertexColors:true,transparent:true,depthWrite:false},col)
  roadHighlight.renderOrder=4;scene.add(roadHighlight)
 }
 if(settlementPts)for(const pin of settlementPts.children){const on=hitSettle.has(pin.userData.id),head=pin.children[1];head.material.color.set(on?0xf5222d:0xffffff);head.material.emissive.set(on?0x7a0d07:0x333333)}
 needsRender=true}
function dataTexture(){const w=W(),h=H(),t=new THREE.DataTexture(new Uint8Array(w*h*4),w,h,THREE.RGBAFormat);t.colorSpace=THREE.SRGBColorSpace;t.magFilter=THREE.LinearFilter;t.minFilter=THREE.LinearFilter;t.needsUpdate=true;return t}
function paint(){
 if(!silingTex)return
 water=props.water
 if(!water){for(const o of [siling,bange,channel,particles])if(o)o.visible=false;needsRender=true;return}
 siling.visible=bange.visible=true
 const w=W(),h=H(),s=silingTex.image.data,b=bangeTex.image.data
 s.fill(0);b.fill(0)
 for(let r=0;r<h;r++)for(let c=0;c<w;c++){const i=r*w+c,o=((h-1-r)*w+c)*4,st=water.state[i]
  if(st===1){s.set([30,104,150,232],o)}
  else if(st===2){const col=props.risk?RGB.find(x=>water.depth[i]<=x[0])[1]:[92,172,212];s.set([...col,240],o)}
  else if(st===3){b.set([34,112,160,232],o)}
  else if(st===4){const col=props.risk?RGB.find(x=>water.depth[i]<=x[0])[1]:[92,172,212];b.set([...col,240],o)}}
 silingTex.needsUpdate=true;bangeTex.needsUpdate=true
 const m=props.data.meta
 siling.position.y=surface(m.datum?props.frame.lake-m.datum.lakeMinusDemM:0)
 bange.position.y=surface(m.bange.surfaceDemM+(props.frame.bangeRiseM||0))
 const q=Math.max(0,props.frame.outflow||0)
 // Ribbon half-width follows the breach width (exaggerated ×1.6 for legibility at basin scale).
 const width=props.frame.width||props.data.meta.outlet.currentWidthM||60,radius=q>0?Math.max(.16,width/1000*1.2):0
 if(Math.abs(radius-lastRadius)>.002)buildChannel(radius)
 channel.visible=particles.visible=q>0;ring.visible=beacon.visible=true
 particles.geometry.setDrawRange(0,Math.round(N_PARTICLES*Math.min(1,Math.max(.25,q/70))))
 channel.material.opacity=.6+.3*Math.min(1,q/80)
 needsRender=true
}
function flowTexture(){
 // Streaky water surface: soft longitudinal streaks that the render loop advects downstream.
 const c=document.createElement('canvas');c.width=512;c.height=64;const g=c.getContext('2d')
 const grad=g.createLinearGradient(0,0,0,64);grad.addColorStop(0,'rgba(24,96,150,.35)');grad.addColorStop(.5,'rgba(38,140,205,.95)');grad.addColorStop(1,'rgba(24,96,150,.35)')
 g.fillStyle=grad;g.fillRect(0,0,512,64)
 for(let k=0;k<70;k++){const x=(k*137)%512,y=6+((k*53)%52),l=30+(k*29)%90;g.strokeStyle=`rgba(190,235,255,${.18+.35*((k*17)%10)/10})`;g.lineWidth=1+(k%3);g.beginPath();g.moveTo(x,y);g.bezierCurveTo(x+l*.3,y-2,x+l*.7,y+2,x+l,y);g.stroke()}
 const t=new THREE.CanvasTexture(c);t.wrapS=THREE.RepeatWrapping;t.wrapT=THREE.ClampToEdgeWrapping;t.colorSpace=THREE.SRGBColorSpace;return t
}
function buildChannel(radius){
 lastRadius=radius;halfWidth=radius
 if(channel)channel.geometry.dispose()
 let geom=new THREE.BufferGeometry()
 if(radius>0){
  const n=320,pos=[],uv=[],idx=[];let along=0,prev=null
  for(let i=0;i<=n;i++){const t=i/n,p=channelCurve.getPointAt(t),tg=channelCurve.getTangentAt(t),nx=-tg.z,nz=tg.x,l=Math.hypot(nx,nz)||1
   if(prev)along+=p.distanceTo(prev);prev=p
   pos.push(p.x+nx/l*radius,p.y+.006,p.z+nz/l*radius,p.x-nx/l*radius,p.y+.006,p.z-nz/l*radius);uv.push(along/.6,0,along/.6,1)
   if(i<n){const k=i*2;idx.push(k,k+1,k+2,k+1,k+3,k+2)}}
  channelLength=along
  geom.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));geom.setAttribute('uv',new THREE.Float32BufferAttribute(uv,2));geom.setIndex(idx)
 }
 if(channel)channel.geometry=geom
 else{flowTex=flowTexture();channel=new THREE.Mesh(geom,new THREE.MeshBasicMaterial({map:flowTex,color:0xffffff,transparent:true,opacity:.9,depthWrite:false,side:THREE.DoubleSide}));scene.add(channel)}
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
 const views={overview:[point(89.47,31.75),new THREE.Vector3(-14,30,36)],outlet:[point(o.lon,o.lat),new THREE.Vector3(-4.5,5.5,7.5)],bange:[point(89.52,31.745),new THREE.Vector3(-8,11,14)]}
 const [target,offset]=Array.isArray(name)?[point(...name),new THREE.Vector3(-3,4,5.5)]:views[name]||views.overview
 tween={t:0,fromP:camera.position.clone(),fromT:controls.target.clone(),toP:target.clone().add(offset),toT:target}
}
function project(){
 if(!camera||!host.value)return
 const m=props.data.meta,rect=host.value.getBoundingClientRect(),items=[['色林错',89.27,31.80,'lake'],['班戈错',89.545,31.755,'lake'],['溃口',m.outlet.lon,m.outlet.lat,'outlet'],...(m.stations['SLC-E']?[['东岸站',...m.stations['SLC-E'],'station']]:[]),...(m.stations.SLC?[['色林错主站',...m.stations.SLC,'station']]:[])]
 const hit=new Set((props.impact?.settlements||[]).map(s=>s.id));for(const s of props.exposures?.settlements||[])if(inEast(m,s.lon,s.lat))items.push([s.name,s.lon,s.lat,hit.has(s.id)?'hit':'settle'])
 labels.value=items.map(([name,lon,lat,kind])=>{const v=point(lon,lat,kind==='outlet'?1.25:kind==='hit'||kind==='settle'?.8:.15).project(camera);return {name,kind,x:(v.x+1)/2*rect.width,y:(1-v.y)/2*rect.height,hidden:v.z>1||v.z<-1}})
}
let pointer=null,raycaster
function inspect(event){
 if(!terrain||!water)return
 const r=renderer.domElement.getBoundingClientRect();pointer.set((event.clientX-r.left)/r.width*2-1,-(event.clientY-r.top)/r.height*2+1)
 raycaster.setFromCamera(pointer,camera)
 const road=roadHighlight&&raycaster.intersectObject(roadHighlight)[0]
 if(road){const row=highlighted[Math.floor(road.faceIndex/2)]
  tip.value={x:event.clientX-r.left,y:event.clientY-r.top,text:row.road.name,detail:roadStateLabel(row.status)+' · 地面水深 '+row.depth.toFixed(2)+' m'};return}
 const hit=raycaster.intersectObject(terrain)[0]
 if(!hit){tip.value=null;return}
 const u=hit.point.x/sizeKm[0]+.5,v=hit.point.z/sizeKm[1]+.5,c=Math.round(u*(W()-1)),row=Math.round(v*(H()-1)),i=row*W()+c,st=water.state[i]
 const dem=props.data.heights[i],m=props.data.meta
 tip.value={x:event.clientX-r.left,y:event.clientY-r.top,text:st===1?'色林错水域':st===3?'班戈错水域':st===2?'色林错新增淹没 · 水深 '+water.depth[i].toFixed(2)+' m':st===4?'班戈错新增淹没 · 水深 '+water.depth[i].toFixed(2)+' m':'陆地',elev:st===3?dem:toLake(m,dem)}
}
function loop(){
 raf=requestAnimationFrame(loop)
 if(document.hidden)return
 const dt=Math.min(.1,clock.getDelta());let active=false
 if(tween){tween.t=Math.min(1,tween.t+dt/0.9);const e=1-Math.pow(1-tween.t,3);camera.position.lerpVectors(tween.fromP,tween.toP,e);controls.target.lerpVectors(tween.fromT,tween.toT,e);if(tween.t>=1)tween=null;active=true}
 if(controls.update())active=true
 if(rippleTex&&props.frame){rippleTex.offset.x+=dt*.012;rippleTex.offset.y+=dt*.007}
 const q=Math.max(0,props.frame?.outflow||0)
 if(q>0&&props.flow&&particles.visible){
  // Visual speed follows the mean section velocity v = Q / (B·hc), hc = 2/3·H (broad-crested weir).
  const f=props.frame,head=Math.max(.02,(f.lake??0)-(f.crest??props.data.meta.outlet.currentCrestM)),v=q/((f.width||60)*Math.max(.02,2/3*head))
  const speed=(.035+.14*Math.min(1,v/2.5))*(8/Math.max(4,channelLength)),pos=particles.geometry.attributes.position,col=particles.geometry.attributes.color,count=particles.geometry.drawRange.count
  for(let k=0;k<count;k++){const lane=particleLane[k],t=particleT[k]=(particleT[k]+speed*dt*(1.15-.5*lane*lane))%1
   const p=channelCurve.getPointAt(t),tg=channelCurve.getTangentAt(t),l=Math.hypot(tg.x,tg.z)||1,w=halfWidth*lane*(1+1.8*Math.max(0,t-.9)/.1)
   pos.setXYZ(k,p.x-tg.z/l*w,p.y+.02+.01*Math.sin(t*90+k),p.z+tg.x/l*w)
   const fade=t<.04?t/.04:t>.9?Math.max(0,1-(t-.9)/.1):1,b=.55+.45*(1-Math.abs(lane));col.setXYZ(k,.42*b*fade,.78*b*fade,.95*fade)}
  pos.needsUpdate=true;col.needsUpdate=true
  if(flowTex)flowTex.offset.x-=dt*(.25+1.1*Math.min(1,v/2.5))
  const pulse=(performance.now()/1600)%1;ring.scale.setScalar(1+1.6*pulse);ring.material.opacity=.85*(1-pulse);active=true}
 if(active||needsRender){renderer.render(scene,camera);if(needsRender||tween)project();needsRender=false}
}
function sky(){const c=document.createElement('canvas');c.width=2;c.height=256;const g=c.getContext('2d'),grad=g.createLinearGradient(0,0,0,256);grad.addColorStop(0,'#bcd6e6');grad.addColorStop(.55,'#e4eef3');grad.addColorStop(1,'#f4f1ea');g.fillStyle=grad;g.fillRect(0,0,2,256);const t=new THREE.CanvasTexture(c);t.colorSpace=THREE.SRGBColorSpace;return t}
onMounted(async()=>{try{
 const [three,orbit]=await Promise.all([import('three'),import('three/addons/controls/OrbitControls.js')])
 if(disposed)return
 THREE=three;({OrbitControls}=orbit)
 const m=props.data.meta,b=m.east.bounds,lat=(b[1]+b[3])/2
 sizeKm=[(b[2]-b[0])*111.32*Math.cos(lat*Math.PI/180),(b[3]-b[1])*110.95];base=m.east.elevationRange[0]
 scene=new THREE.Scene();scene.background=sky();scene.fog=new THREE.Fog(0xe6eef2,70,170)
 camera=new THREE.PerspectiveCamera(38,1,.05,500)
 renderer=new THREE.WebGLRenderer({antialias:true,powerPreference:'low-power'});renderer.setPixelRatio(Math.min(devicePixelRatio,1.75));renderer.outputColorSpace=THREE.SRGBColorSpace
 host.value.appendChild(renderer.domElement)
 controls=new OrbitControls(camera,renderer.domElement);controls.enableDamping=true;controls.dampingFactor=.08;controls.maxPolarAngle=Math.PI*.47;controls.minDistance=2;controls.maxDistance=140
 controls.addEventListener('change',()=>{needsRender=true})
 scene.add(new THREE.HemisphereLight(0xeaf4ff,0x7a6a55,1.05));const sun=new THREE.DirectionalLight(0xfff1dc,1.9);sun.position.set(-40,32,22);scene.add(sun)
 const texture=await new THREE.TextureLoader().loadAsync(props.data.base+m.east.texture)
 if(disposed){texture.dispose();return}
 texture.colorSpace=THREE.SRGBColorSpace;texture.anisotropy=Math.min(8,renderer.capabilities.getMaxAnisotropy())
 const g=new THREE.PlaneGeometry(sizeKm[0],sizeKm[1],W()-1,H()-1);g.rotateX(-Math.PI/2)
 terrain=new THREE.Mesh(g,new THREE.MeshStandardMaterial({map:texture,roughness:1,metalness:0}));scene.add(terrain)
 rippleTex=ripples();silingTex=dataTexture();bangeTex=dataTexture();siling=waterPlane(silingTex);bange=waterPlane(bangeTex);scene.add(siling,bange)
 ring=new THREE.Mesh(new THREE.RingGeometry(.22,.3,48).rotateX(-Math.PI/2),new THREE.MeshBasicMaterial({color:0xff9a3c,transparent:true,opacity:.85,side:THREE.DoubleSide,depthWrite:false}));scene.add(ring)
 beacon=new THREE.Mesh(new THREE.CylinderGeometry(.018,.018,1.2,8),new THREE.MeshBasicMaterial({color:0xf07a2a}));scene.add(beacon)
 particleT=new Float32Array(N_PARTICLES).map((_,k)=>((k*0.6180339887)%1));particleLane=new Float32Array(N_PARTICLES).map((_,k)=>(((k*7919)%1000)/500-1)*.92)
 const pg=new THREE.BufferGeometry();pg.setAttribute('position',new THREE.Float32BufferAttribute(new Float32Array(N_PARTICLES*3),3));pg.setAttribute('color',new THREE.Float32BufferAttribute(new Float32Array(N_PARTICLES*3),3))
 const glow=document.createElement('canvas');glow.width=glow.height=64;const gg=glow.getContext('2d'),rad=gg.createRadialGradient(32,32,0,32,32,32);rad.addColorStop(0,'rgba(230,250,255,1)');rad.addColorStop(.35,'rgba(120,215,255,.7)');rad.addColorStop(1,'rgba(60,170,240,0)');gg.fillStyle=rad;gg.fillRect(0,0,64,64)
 const glowTex=new THREE.CanvasTexture(glow)
 particles=new THREE.Points(pg,new THREE.PointsMaterial({size:5,map:glowTex,vertexColors:true,transparent:true,opacity:1,depthWrite:false,depthTest:false,blending:THREE.AdditiveBlending,sizeAttenuation:false}));particles.renderOrder=5;scene.add(particles)
 raycaster=new THREE.Raycaster();pointer=new THREE.Vector2();clock=new THREE.Clock()
 setHeights();buildExposures()
 const start=point(89.47,31.75);camera.position.copy(start).add(new THREE.Vector3(-14,30,36));controls.target.copy(start);controls.update()
 observer=new ResizeObserver(()=>{if(!host.value)return;const w=host.value.clientWidth,h=host.value.clientHeight;renderer.setSize(w,h);camera.aspect=w/Math.max(1,h);camera.updateProjectionMatrix();needsRender=true});observer.observe(host.value)
 renderer.domElement.addEventListener('pointermove',inspect);renderer.domElement.addEventListener('pointerleave',()=>tip.value=null)
 if(props.preset!=='overview')presetView(props.preset)
 loop()
}catch(e){error.value='三维场景无法加载：'+e.message}})
watch(()=>props.water,paint)
watch(()=>props.risk,paint)
watch(exaggeration,()=>{if(terrain){setHeights();buildExposures()}})
watch(()=>props.exposures,()=>{if(terrain)buildExposures()})
watch(()=>props.impact,()=>{if(terrain)colourExposures()})
watch(()=>props.preset,v=>{if(camera)presetView(v)})
onBeforeUnmount(()=>{disposed=true;cancelAnimationFrame(raf);observer?.disconnect();controls?.dispose()
 scene?.traverse(o=>{o.geometry?.dispose?.();const mats=Array.isArray(o.material)?o.material:o.material?[o.material]:[];for(const mt of mats){mt.map?.dispose?.();mt.dispose()}})
 silingTex?.dispose();bangeTex?.dispose();rippleTex?.dispose();scene?.background?.dispose?.();renderer?.dispose();renderer?.domElement?.remove()})
</script>
<template>
<div class="scene">
 <div ref="host" class="scene-canvas"></div>
 <template v-for="(l,k) in labels" :key="l.kind+k"><span v-if="!l.hidden" :class="['scene-label',l.kind]" :style="{left:l.x+'px',top:l.y+'px'}">{{l.name}}</span></template>
 <div v-if="tip" class="scene-tip" :style="{left:Math.min(tip.x+14,9999)+'px',top:tip.y+14+'px'}"><b>{{tip.text}}</b><span>{{tip.detail??((tip.text.startsWith('班戈错')?'地面高程':'相当湖位')+' '+tip.elev.toFixed(2)+' m')}}</span></div>
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
.scene-label.settle{font-size:11.5px;font-weight:600;color:#3b2a1c;background:#fffffff0;padding:1px 7px;border-radius:9px;text-shadow:none;box-shadow:0 1px 4px #0002}
.scene-label.hit{font-size:11.5px;font-weight:600;color:#fff;background:#b8321f;padding:1px 7px;border-radius:9px;text-shadow:none;box-shadow:0 1px 4px #0003}
.scene-tip{position:absolute;pointer-events:none;background:#10303dee;color:#fff;padding:7px 10px;border-radius:6px;font-size:12px;display:grid;gap:2px;white-space:nowrap}
.scene-tip span{color:#bcd4dc}
.scene-exag{position:absolute;right:14px;bottom:72px;display:flex;align-items:center;gap:8px;background:#ffffffd9;padding:5px 10px;border-radius:6px;font-size:12px;color:#365563}
.scene-exag input{width:90px;min-height:0}
.scene-error{position:absolute;inset:auto 20px 50% 20px;text-align:center;color:#a23a2c}
</style>
