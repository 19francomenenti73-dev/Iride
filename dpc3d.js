
/* I.R.I.D.E. V13 — Radar-DPC CAPPI 3D
   Dati reali, CC BY-SA 4.0.
   Volume, ISO, Olografico, Sezione verticale.
   Nessuna riflettivita' sintetica. */
(()=>{
'use strict';
const $=id=>document.getElementById(id);
const viewer=$('viewer'),mode=$('mode'),old=$('scene');
if(!viewer||!mode||!old)return;
const foot=viewer.querySelector('.vfoot');
if(!foot)return;
const oldFoot=foot.textContent;
const panel=document.createElement('div');
panel.style.cssText=
 'display:none;flex:1;min-height:0;position:relative;background:#020509';
old.after(panel);
const canvas=document.createElement('canvas');
const section=document.createElement('canvas');
for(const c of [canvas,section]){
 c.style.cssText=
  'width:100%;height:100%;display:block;touch-action:none';
 panel.append(c);
}
const ruler=document.createElement('canvas');
ruler.style.cssText=
 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none';
panel.append(ruler);
const controls=document.createElement('div');
controls.style.cssText=
 'display:none;gap:8px;flex-wrap:wrap;align-items:center;padding:6px 10px;background:#17283a;font-size:11px';
controls.innerHTML=
 '<label>Verticale ×<output id="cap-scale-value"></output> '+
 '<input id="cap-scale" type="range" min="1" max="30" value="5"></label>'+
 '<label id="cap-cut-controls">Taglio '+
 '<select id="cap-direction"><option value="ew">Ovest → Est</option>'+
 '<option value="ns">Nord → Sud</option></select> '+
 '<input id="cap-offset" type="range" min="0" max="31" value="16"></label>'+
 '<span>15 blu · 25 verde · 30 giallo · 35 arancio · 45 magenta dBZ</span>';
panel.before(controls);
const option=new Option('Sezione verticale DPC','3');
option.disabled=true;
mode.add(option);
const gl=canvas.getContext('webgl2',{
 alpha:false,antialias:false,preserveDrawingBuffer:false
});
if(!gl){
 option.remove();controls.remove();panel.remove();return;
}
const vertex=`#version 300 es
in vec2 a;
out vec2 uv;
void main(){
 uv=a*.5+.5;
 gl_Position=vec4(a,0.,1.);
}`;
const fragment=`#version 300 es
precision highp float;
precision highp sampler3D;
in vec2 uv;
out vec4 color;
uniform sampler3D data;
uniform sampler2D floorData;
uniform vec2 resolution;
uniform vec3 eye,right,up,forward;
uniform float height;
uniform int mode;
const vec3 bg=vec3(.008,.018,.031);

vec3 palette(float v){
 vec3 b=vec3(.22,.49,.91);
 vec3 g=vec3(.14,.74,.44);
 vec3 y=vec3(.96,.83,.24);
 vec3 r=vec3(.94,.47,.20);
 vec3 m=vec3(.93,.26,.78);
 if(v<25.)return mix(b,g,smoothstep(15.,25.,v));
 if(v<30.)return mix(g,y,smoothstep(25.,30.,v));
 if(v<35.)return mix(y,r,smoothstep(30.,35.,v));
 return mix(r,m,smoothstep(35.,45.,v));
}
vec2 sampleAt(vec3 p){
 float km=p.y/height*10.;
 if(km<1.||km>10.||abs(p.x)>2.25||abs(p.z)>2.25)
  return vec2(0.);
 vec3 q=vec3((p.x+2.25)/4.5,(km-.5)/10.,
             (p.z+2.25)/4.5);
 vec2 v=texture(data,q).rg;
 return vec2(v.r*127.5-33.,v.g);
}
float density(float v){
 if(v<15.)return 0.;
 if(v<25.)return mix(.07,.17,smoothstep(15.,25.,v));
 if(v<30.)return mix(.17,.38,smoothstep(25.,30.,v));
 if(v<35.)return mix(.38,.78,smoothstep(30.,35.,v));
 if(v<45.)return mix(.78,1.55,smoothstep(35.,45.,v));
 return mix(1.55,2.0,smoothstep(45.,60.,v));
}
vec3 normalAt(vec3 p){
 vec3 e=vec3(.06,0.,0.);
 float x=sampleAt(p+e).x-sampleAt(p-e).x;
 e=vec3(0.,.06,0.);
 float y=sampleAt(p+e).x-sampleAt(p-e).x;
 e=vec3(0.,0.,.06);
 float z=sampleAt(p+e).x-sampleAt(p-e).x;
 return normalize(vec3(x,y,z)+vec3(.0001));
}
void main(){
 vec2 s=uv*2.-1.;
 s.x*=resolution.x/resolution.y;
 vec3 ray=normalize(forward+.625*(s.x*right+s.y*up));
 vec3 result=bg;
 float ft=-1.;
 if(ray.y<-.0001)ft=-eye.y/ray.y;
 if(ft>0.){
  vec3 p=eye+ft*ray;
  if(abs(p.x)<=2.25&&abs(p.z)<=2.25){
   vec2 f=texture(floorData,(p.xz+2.25)/4.5).rg;
   float v=f.r*127.5-33.;
   result=f.g<.5?vec3(.055,.065,.08):
          v>=15.?palette(v)*.66:bg;
  }
 }
 vec3 inv=1./ray;
 vec3 a=(vec3(-2.25,height*.1,-2.25)-eye)*inv;
 vec3 b=(vec3(2.25,height,2.25)-eye)*inv;
 vec3 lo=min(a,b),hi=max(a,b);
 float start=max(0.,max(lo.x,max(lo.y,lo.z)));
 float end=min(hi.x,min(hi.y,hi.z));
 if(ft>start)end=min(end,ft);
 if(end>start){
  float step=(end-start)/160.;
  float trans=1.;
  vec3 sum=vec3(0.);
  vec2 prev=sampleAt(eye+ray*start);
  for(int i=1;i<=160;i++){
   vec3 p=eye+ray*(start+float(i)*step);
   vec2 v=sampleAt(p);
   float edge=min(2.25-abs(p.x),2.25-abs(p.z));
   float fade=smoothstep(0.,.18,edge);
   if(v.y>.98&&v.x>=15.){
    vec3 c=palette(v.x);
    float alpha=0.;
    if(mode==0){
     float core=smoothstep(24.,40.,v.x);
     float d=density(v.x)*mix(.65,1.15,core);
     alpha=1.-exp(-d*step*3.3*fade);
     alpha=min(alpha,.23);
     float light=.82+.18*abs(dot(
      normalAt(p),normalize(vec3(-.4,.8,.6))));
     c*=light;
    }else if(mode==1){
     float surface=0.;
     for(int k=0;k<5;k++){
      float level=k==0?15.:k==1?25.:
                  k==2?30.:k==3?35.:45.;
      if(prev.y>.98&&
         ((prev.x<level&&v.x>=level)||
          (prev.x>=level&&v.x<level))){
       float strength=k==0?.13:k==1?.22:
                      k==2?.38:k==3?.55:.72;
       surface=max(surface,strength);
      }
     }
     alpha=surface*fade;
     c*=.60+.40*abs(dot(
      normalAt(p),normalize(vec3(-.4,.8,.6))));
    }else{
     float gradient=length(vec2(
      sampleAt(p+vec3(.08,0.,0.)).x-
      sampleAt(p-vec3(.08,0.,0.)).x,
      sampleAt(p+vec3(0.,0.,.08)).x-
      sampleAt(p-vec3(0.,0.,.08)).x
     ));
     float rim=smoothstep(2.,14.,gradient);
     float glow=mix(.20,1.0,rim);
     alpha=(1.-exp(-density(v.x)*step*1.9))
           *glow*fade;
     c=mix(c,c*vec3(.55,1.25,1.45),.36);
    }
    sum+=trans*alpha*c;
    trans*=1.-alpha;
   }
   prev=v;
   if(trans<.015)break;
  }
  result=sum+trans*result;
 }
 color=vec4(result,1.);
}`;
function shader(type,source){
 const s=gl.createShader(type);
 gl.shaderSource(s,source);
 gl.compileShader(s);
 if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))
  throw Error(gl.getShaderInfoLog(s));
 return s;
}
let program;
try{
 program=gl.createProgram();
 gl.attachShader(program,shader(gl.VERTEX_SHADER,vertex));
 gl.attachShader(program,shader(gl.FRAGMENT_SHADER,fragment));
 gl.linkProgram(program);
 if(!gl.getProgramParameter(program,gl.LINK_STATUS))
  throw Error(gl.getProgramInfoLog(program));
}catch(e){
 console.error('IRIDE V13 shader',e);
 option.remove();controls.remove();panel.remove();return;
}
gl.useProgram(program);
const buffer=gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([
 -1,-1,1,-1,-1,1,1,1
]),gl.STATIC_DRAW);
const loc=gl.getAttribLocation(program,'a');
gl.enableVertexAttribArray(loc);
gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
const uniforms={};
for(const name of [
 'data','floorData','resolution','eye',
 'right','up','forward','height','mode'
])uniforms[name]=gl.getUniformLocation(program,name);
const volumeTexture=gl.createTexture();
const floorTexture=gl.createTexture();
let active=null,sequence=0,note='';
let yaw=-.35,pitch=.24,distance=6,drag=null,pinch=0;
const norm=v=>{
 const m=Math.hypot(...v)||1;
 return v.map(x=>x/m);
};
const cross=(a,b)=>[
 a[1]*b[2]-a[2]*b[1],
 a[2]*b[0]-a[0]*b[2],
 a[0]*b[1]-a[1]*b[0]
];
const fresh=t=>{
 const age=(Date.now()-Date.parse(t))/60000;
 return age>=-5&&age<=90;
};
   
function upload(value){
 const n=value.side;
 const packed=new Uint8Array(n*10*n*2);
 const floor=new Uint8Array(n*n*2);
 for(let z=0;z<n;z++)
 for(let y=0;y<10;y++)
 for(let x=0;x<n;x++){
  const code=value.layers[y][z][x];
  const i=((z*10+y)*n+x)*2;
  packed[i]=code;
  packed[i+1]=code>=2?255:0;
 }
 for(let z=0;z<n;z++)
 for(let x=0;x<n;x++){
  const code=value.floor[z][x];
  const i=(z*n+x)*2;
  floor[i]=code;
  floor[i+1]=code>=2?255:0;
 }
 gl.pixelStorei(gl.UNPACK_ALIGNMENT,1);
 for(const [unit,target,tex] of [
  [0,gl.TEXTURE_3D,volumeTexture],
  [1,gl.TEXTURE_2D,floorTexture]
 ]){
  gl.activeTexture(gl.TEXTURE0+unit);
  gl.bindTexture(target,tex);
  gl.texParameteri(target,gl.TEXTURE_MIN_FILTER,
   unit?gl.NEAREST:gl.LINEAR);
  gl.texParameteri(target,gl.TEXTURE_MAG_FILTER,
   unit?gl.NEAREST:gl.LINEAR);
  gl.texParameteri(target,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
  gl.texParameteri(target,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
  if(!unit){
   gl.texParameteri(target,gl.TEXTURE_WRAP_R,gl.CLAMP_TO_EDGE);
   gl.texImage3D(target,0,gl.RG8,n,10,n,0,
    gl.RG,gl.UNSIGNED_BYTE,packed);
  }else{
   gl.texImage2D(target,0,gl.RG8,n,n,0,
    gl.RG,gl.UNSIGNED_BYTE,floor);
  }
 }
}

function sectionDraw(){
 const ctx=section.getContext('2d');
 const w=section.width,h=section.height;
 const left=48,top=32;
 const pw=Math.max(1,w-66),ph=Math.max(1,h-78);
 const n=active.side;
 const row=Number($('cap-offset').value);
 const ew=$('cap-direction').value==='ew';
 const colors=[
  '#397ee8','#24bd70','#f4d43e',
  '#f07832','#ed42c8'
 ];
 ctx.fillStyle='#020509';
 ctx.fillRect(0,0,w,h);
 ctx.fillStyle='#071421';
 ctx.fillRect(left,top,pw,ph);
 for(let y=0;y<10;y++)
 for(let x=0;x<n;x++){
  const code=ew?active.layers[y][row][x]:
   active.layers[y][x][row];
  if(code<2)continue;
  const dbz=(code-2)/2-32;
  if(dbz<15)continue;
  ctx.fillStyle=colors[
   dbz>=45?4:dbz>=35?3:
   dbz>=30?2:dbz>=25?1:0
  ];
  ctx.fillRect(
   left+x*pw/n,top+(9-y)*ph/10,
   pw/n+0.5,ph/10+0.5
  );
 }
 ctx.font='12px sans-serif';
 ctx.fillStyle='#c8e9f3';
 ctx.strokeStyle='rgba(160,205,225,.22)';
 for(let y=0;y<10;y++){
  const yy=top+(9.5-y)*ph/10;
  ctx.fillText(String(y+1),15,yy+4);
 }
 ctx.fillText('km slm',5,18);
 ctx.fillText(ew?'Ovest':'Nord',left,h-18);
 ctx.fillText(ew?'Est':'Sud',w-35,h-18);
 ctx.fillText(
  `${active.width_km.toFixed(0)} km · 10 livelli CAPPI`,
  left,18
 );
}

function draw(){
 const use=active&&fresh(active.timestamp);
 old.style.display=use?'none':'';
 panel.style.display=use?'block':'none';
 controls.style.display=use?'flex':'none';
 if(!use){
  foot.textContent=oldFoot+(note?' · '+note:'');
  return;
 }
 const cut=mode.value==='3';
 canvas.style.display=cut?'none':'block';
 section.style.display=cut?'block':'none';
 ruler.style.display=cut?'none':'block';
 $('cap-cut-controls').style.display=cut?'inline':'none';
 const scale=Number($('cap-scale').value);
 $('cap-scale-value').value=scale;
 const rect=panel.getBoundingClientRect();
 const ratio=Math.min(window.devicePixelRatio||1,1.25);
 const selected=cut?section:canvas;
 selected.width=Math.max(1,Math.round(rect.width*ratio));
 selected.height=Math.max(1,Math.round(rect.height*ratio));
 const time=new Date(active.timestamp).toLocaleTimeString(
  'it-IT',{
   hour:'2-digit',minute:'2-digit',
   timeZone:'Europe/Rome'
  }
 );
 const label=mode.value==='0'?'Volume V13':
  mode.value==='1'?'Iso-superficie V13':
  mode.value==='2'?'Olografico V13':'Sezione verticale';
 foot.textContent=
  `Radar-DPC · CC BY-SA 4.0 · CAPPI ${time} Italia · `+
  `1–10 km slm · ${label} · `+
  (cut?'livelli osservati':
   `verticale ×${scale} · pavimento: massimo CAPPI`);
 if(cut){
  sectionDraw();
  return;
 }
 const height=45/active.width_km*scale;
 const target=[0,height*.45,0];
 const eye=[
  distance*Math.cos(pitch)*Math.sin(yaw),
  target[1]+distance*Math.sin(pitch),
  distance*Math.cos(pitch)*Math.cos(yaw)
 ];
 const forward=norm(target.map((x,i)=>x-eye[i]));
 const right=norm(cross(forward,[0,1,0]));
 const up=cross(right,forward);
 gl.useProgram(program);
 gl.viewport(0,0,canvas.width,canvas.height);
 gl.uniform1i(uniforms.data,0);
 gl.uniform1i(uniforms.floorData,1);
 gl.uniform2f(uniforms.resolution,
  canvas.width,canvas.height);
 for(const [name,v] of Object.entries({
  eye,right,up,forward
 }))gl.uniform3fv(uniforms[name],v);
 gl.uniform1f(uniforms.height,height);
 gl.uniform1i(uniforms.mode,Number(mode.value));
 gl.drawArrays(gl.TRIANGLE_STRIP,0,4);

 ruler.width=canvas.width;
 ruler.height=canvas.height;
 const ctx=ruler.getContext('2d');
 const dot=(a,b)=>a.reduce(
  (sum,v,i)=>sum+v*b[i],0
 );
 const project=km=>{
  const v=[
   2.38-eye[0],
   height*km/10-eye[1],
   2.38-eye[2]
  ];
  const depth=dot(v,forward);
  if(depth<=0.1)return null;
  return [
   (1+dot(v,right)/(depth*.625)/
    (canvas.width/canvas.height))*canvas.width/2,
   (1-dot(v,up)/(depth*.625))*canvas.height/2
  ];
 };
 const a=project(0),b=project(10);
 if(a&&b){
  ctx.strokeStyle='#8cb8cc';
  ctx.fillStyle='#d8f4ff';
  ctx.font='12px sans-serif';
  ctx.beginPath();
  ctx.moveTo(...a);
  ctx.lineTo(...b);
  ctx.stroke();
  for(let km=0;km<=10;km+=2){
   const p=project(km);
   if(p&&p[0]>=0&&p[0]<canvas.width-30)
    ctx.fillText(km+' km',p[0]+5,p[1]+4);
  }
 }
}

async function json(url){
 const controller=new AbortController();
 const timer=setTimeout(
  ()=>controller.abort(),12000
 );
 try{
  const response=await fetch(url,{
   cache:'no-store',
   signal:controller.signal
  });
  if(!response.ok)
   throw Error('HTTP '+response.status);
  return await response.json();
 }finally{
  clearTimeout(timer);
 }
}

window.addEventListener('iride:cell',async event=>{
 const token=++sequence;
 const {cell,timestamp}=event.detail;
 active=null;
 option.disabled=true;
 note='CAPPI in caricamento';
 requestAnimationFrame(draw);
 try{
  const base=new URL(
   'docs/data/dpc3d/',document.baseURI
  );
  const manifest=await json(
   new URL('manifest.json',base)
  );
  if(manifest.status!=='ok'||
     manifest.base_timestamp!==timestamp||
     !fresh(manifest.timestamp))
   throw Error('CAPPI non sincronizzati');
  const file=manifest.cells[cell.track_id];
  if(!file||!/^[A-Za-z0-9_-]+\.json$/.test(file))
   throw Error('CAPPI assenti per questa cella');
  const value=await json(new URL(file,base));
  if(token!==sequence)return;
  if(value.base_timestamp!==timestamp||
     value.track_id!==cell.track_id||
     value.timestamp!==manifest.timestamp||
     !fresh(value.timestamp))
   throw Error('CAPPI incoerenti');
  if(value.side!==32||
     !Number.isFinite(value.width_km)||
     value.width_km<=0||
     !Array.isArray(value.layers)||
     value.layers.length!==10||
     !Array.isArray(value.floor))
   throw Error('Volume non valido');
  for(const layer of [...value.layers,value.floor]){
   if(layer.length!==32||
      layer.some(row=>
       !Array.isArray(row)||row.length!==32||
       row.some(code=>
        !Number.isInteger(code)||
        code<0||code>246)))
    throw Error('Matrice CAPPI non valida');
  }
  upload(value);
  active=value;
  option.disabled=false;
  note='';
  $('cap-scale').value=Math.max(
   1,Math.min(30,Math.round(value.width_km/20))
  );
  yaw=-.35;
  pitch=.24;
  const h=45/value.width_km*
   Number($('cap-scale').value);
  distance=Math.max(5.5,Math.min(12,5+h*.32));
  draw();
 }catch(error){
  if(token===sequence){
   note=error.message+' · riserva 2D';
   draw();
  }
 }
});

mode.addEventListener('change',draw);
$('reset').addEventListener('click',()=>{
 yaw=-.35;
 pitch=.24;
 const h=active?45/active.width_km*
  Number($('cap-scale').value):0;
 distance=Math.max(5.5,Math.min(12,5+h*.32));
 draw();
});
for(const id of [
 'cap-scale','cap-direction','cap-offset'
])$(id).addEventListener('input',draw);
new ResizeObserver(()=>{
 if(active)draw();
}).observe(panel);
new MutationObserver(()=>{
 if(!viewer.classList.contains('open')){
  sequence++;
  active=null;
  note='';
  draw();
 }
}).observe(viewer,{
 attributes:true,attributeFilter:['class']
});
canvas.addEventListener('pointerdown',e=>{
 drag=[e.clientX,e.clientY];
 canvas.setPointerCapture(e.pointerId);
});
canvas.addEventListener('pointermove',e=>{
 if(!drag)return;
 yaw-=(e.clientX-drag[0])*.006;
 pitch=Math.max(.05,Math.min(1.3,
  pitch+(e.clientY-drag[1])*.004));
 drag=[e.clientX,e.clientY];
 draw();
});
for(const name of [
 'pointerup','pointercancel','lostpointercapture'
])canvas.addEventListener(name,()=>{drag=null;});
canvas.addEventListener('wheel',e=>{
 e.preventDefault();
 distance=Math.max(3.5,Math.min(20,
  distance+e.deltaY*.008));
 draw();
},{passive:false});
canvas.addEventListener('touchstart',e=>{
 if(e.touches.length===2){
  drag=null;
  pinch=Math.hypot(
   e.touches[0].clientX-e.touches[1].clientX,
   e.touches[0].clientY-e.touches[1].clientY
  );
 }
},{passive:true});
canvas.addEventListener('touchmove',e=>{
 if(e.touches.length!==2)return;
 e.preventDefault();
 drag=null;
 const d=Math.hypot(
  e.touches[0].clientX-e.touches[1].clientX,
  e.touches[0].clientY-e.touches[1].clientY
 );
 if(pinch&&d)
  distance=Math.max(3.5,Math.min(
   20,distance*pinch/d
  ));
 pinch=d;
 draw();
},{passive:false});
setInterval(()=>{
 if(active&&!fresh(active.timestamp)){
  active=null;
  option.disabled=true;
  if(mode.value==='3')mode.value='1';
  note='CAPPI scaduti · riserva 2D';
  draw();
  mode.dispatchEvent(new Event('change'));
 }
},30000);
})();
   
