/* I.R.I.D.E. V12.1 — Volume DPC CAPPI.
   Radar-DPC, CC BY-SA 4.0.
   Volume migliorato; ISO, OLO e sezione DPC conservati.
   Fallback V11 in index.html. */
(()=>{
'use strict';
const $=id=>document.getElementById(id);
const viewer=$('viewer'),mode=$('mode'),original=$('scene');
if(!viewer||!mode||!original)return;

const foot=viewer.querySelector('.vfoot');
const oldFoot=foot.textContent;
const panel=document.createElement('div');
panel.style.cssText='display:none;flex:1;min-height:0;position:relative;background:#020509';
const canvas=document.createElement('canvas');
const section=document.createElement('canvas');
for(const c of [canvas,section]){
 c.style.cssText='width:100%;height:100%;display:block;touch-action:none';
 panel.append(c);
}
const ruler=document.createElement('canvas');
ruler.style.cssText='position:absolute;inset:0;width:100%;height:100%;pointer-events:none';
panel.append(ruler);
original.after(panel);

const controls=document.createElement('div');
controls.style.cssText='display:none;gap:8px;flex-wrap:wrap;align-items:center;padding:6px 10px;background:#17283a;font-size:11px';
controls.innerHTML='<label>Verticale ×<output id="cap-scale-value"></output> <input id="cap-scale" type="range" min="1" max="30" step="1" value="5"></label><label id="cap-cut-controls">Taglio <select id="cap-direction"><option value="ew">Ovest → Est</option><option value="ns">Nord → Sud</option></select> <input id="cap-offset" type="range" min="0" max="31" value="16"></label><span>15 blu · 25 verde · 30 giallo · 35 arancio · 45 magenta dBZ</span>';
panel.before(controls);

const opt=new Option('Sezione verticale DPC','3');
opt.disabled=true;
mode.add(opt);

const gl=canvas.getContext('webgl2',{
 alpha:false,antialias:false
});
if(!gl){
 opt.remove();controls.remove();panel.remove();return;
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
float levels[5]=float[5](15.,25.,30.,35.,45.);

vec3 palette(int k){
 if(k==0)return vec3(.224,.494,.910);
 if(k==1)return vec3(.141,.741,.439);
 if(k==2)return vec3(.957,.831,.243);
 if(k==3)return vec3(.941,.471,.196);
 return vec3(.929,.259,.784);
}
int klass(float v){
 return v>=45.?4:(v>=35.?3:(v>=30.?2:(v>=25.?1:0)));
}
vec2 sampleAt(vec3 p){
 float km=p.y/height*10.;
 if(km<1.||km>10.||abs(p.x)>2.25||abs(p.z)>2.25)
  return vec2(0.);
 vec3 q=vec3((p.x+2.25)/4.5,(km-.5)/10.,(p.z+2.25)/4.5);
 vec2 v=texture(data,q).rg;
 return vec2(v.r*127.5-33.,v.g>.999?1.:0.);
}
vec3 normalAt(vec3 p){
 vec3 e=vec3(.025,.025,.025);
 vec3 n=vec3(
  sampleAt(p+vec3(e.x,0,0)).x-sampleAt(p-vec3(e.x,0,0)).x,
  sampleAt(p+vec3(0,e.y,0)).x-sampleAt(p-vec3(0,e.y,0)).x,
  sampleAt(p+vec3(0,0,e.z)).x-sampleAt(p-vec3(0,0,e.z)).x
 );
 return normalize(n+vec3(.0001));
}

/* Colori continui della riflettività CAPPI. */
vec3 volumeColor(float v){
 vec3 blue=palette(0);
 vec3 green=palette(1);
 vec3 yellow=palette(2);
 vec3 orange=palette(3);
 vec3 magenta=palette(4);
 if(v<25.)return mix(blue,green,smoothstep(15.,25.,v));
 if(v<30.)return mix(green,yellow,smoothstep(25.,30.,v));
 if(v<35.)return mix(yellow,orange,smoothstep(30.,35.,v));
 return mix(orange,magenta,smoothstep(35.,45.,v));
}

/* V12.1: blu e verde più visibili;
   nuclei intensi progressivamente più compatti. */
float volumeDensity(float v){
 if(v<15.)return 0.;
 if(v<25.)return mix(.09,.19,smoothstep(15.,25.,v));
 if(v<30.)return mix(.19,.37,smoothstep(25.,30.,v));
 if(v<35.)return mix(.37,.75,smoothstep(30.,35.,v));
 if(v<45.)return mix(.75,1.45,smoothstep(35.,45.,v));
 return mix(1.45,1.9,smoothstep(45.,60.,v));
}

/* V12.1: riduce la nebbia nelle aree deboli,
   preserva i nuclei ed attenua i bordi del volume. */
float volumeStructure(vec3 p,float dbz){
 float dx=abs(sampleAt(p+vec3(.09,0.,0.)).x-
              sampleAt(p-vec3(.09,0.,0.)).x);
 float dz=abs(sampleAt(p+vec3(0.,0.,.09)).x-
              sampleAt(p-vec3(0.,0.,.09)).x);
 float detail=clamp(max(dx,dz)/12.,0.,1.);
 float core=smoothstep(22.,38.,dbz);
 float edge=min(2.25-abs(p.x),2.25-abs(p.z));
 float fade=smoothstep(0.,.20,edge);
 return fade*mix(.38+detail*.22,1.,core);
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
   result=f.g<.5?vec3(.12):v>=15.?palette(klass(v))*.75:bg;
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
  float step=(end-start)/192.;
  float trans=1.;
  vec3 sum=vec3(0.);
  vec2 prev=sampleAt(eye+ray*start);
  for(int i=1;i<=192;i++){
   vec3 p=eye+ray*(start+float(i)*step);
   vec2 v=sampleAt(p);

   /* V12.1: solo modalità Volume. */
   if(mode==0&&v.y>0.&&v.x>=15.){
    float density=volumeDensity(v.x)*volumeStructure(p,v.x);
    float alpha=1.-exp(-density*step*3.2);
    alpha=clamp(alpha,0.,.22);
    vec3 c=volumeColor(v.x);
    float light=.83+.17*clamp(
     dot(normalAt(p),normalize(vec3(-.4,.8,.6))),
     0.,1.
    );
    float contrast=mix(.94,1.12,smoothstep(27.,42.,v.x));
    sum+=trans*alpha*c*light*contrast;
    trans*=1.-alpha;
   }

   /* ISO: algoritmo originale invariato. */
   if(mode==1&&v.y>0.&&prev.y>0.){
    for(int j=0;j<5;j++){
     int k=v.x>=prev.x?j:4-j;
     float level=levels[k];
     if((v.x>=level)!=(prev.x>=level)){
      float f=clamp((level-prev.x)/(v.x-prev.x),0.,1.);
      vec3 hit=p-ray*step*(1.-f),n=normalAt(hit);
      float light=.55+.45*abs(dot(n,normalize(vec3(-.4,.8,.6))));
      float alpha=k==0?.12:k==1?.18:k==2?.32:k==3?.50:.65;
      sum+=trans*alpha*palette(k)*light;
      trans*=1.-alpha;
     }
    }
   }else if(mode==2&&v.y>0.&&v.x>=15.){
    int k=klass(v.x);
    float strength=k==0?.16:k==1?.30:k==2?.7:k==3?1.3:2.;
    float alpha=1.-exp(-strength*step*2.);
    sum+=trans*alpha*palette(k);
    trans*=1.-alpha;
   }
   prev=v;
   if(trans<.02)break;
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
 console.error('CAPPI V12.1 shader',e);
 opt.remove();controls.remove();panel.remove();return;
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
for(const key of [
 'data','floorData','resolution','eye',
 'right','up','forward','height','mode'
])uniforms[key]=gl.getUniformLocation(program,key);
const volumeTexture=gl.createTexture();
const floorTexture=gl.createTexture();
let active=null,sequence=0,note='';
let yaw=-.35,pitch=.34,distance=8,drag=null;
const fresh=t=>{
 const age=(Date.now()-Date.parse(t))/60000;
 return age>=-5&&age<=90;
};
const norm=v=>{
 const m=Math.hypot(...v)||1;
 return v.map(x=>x/m);
};
const cross=(a,b)=>[
 a[1]*b[2]-a[2]*b[1],
 a[2]*b[0]-a[0]*b[2],
 a[0]*b[1]-a[1]*b[0]
];
   
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
  packed[i+1]=code?255:0;
 }
 for(let z=0;z<n;z++)
 for(let x=0;x<n;x++){
  const code=value.floor[z][x];
  const i=(z*n+x)*2;
  floor[i]=code;
  floor[i+1]=code?255:0;
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
 const left=48,top=30;
 const pw=Math.max(1,w-65),ph=Math.max(1,h-75);
 const n=active.side;
 ctx.fillStyle='#020509';
 ctx.fillRect(0,0,w,h);
 const row=Number($('cap-offset').value);
 const ew=$('cap-direction').value==='ew';
 const palette=[
  '#397ee8','#24bd70','#f4d43e','#f07832','#ed42c8'
 ];
 for(let y=0;y<10;y++)
 for(let x=0;x<n;x++){
  const v=ew?active.layers[y][row][x]:
   active.layers[y][x][row];
  const dbz=(v-2)/2-32;
  ctx.fillStyle=!v?'#383e47':
   v===1||dbz<15?'#071421':
   palette[dbz>=45?4:dbz>=35?3:
    dbz>=30?2:dbz>=25?1:0];
  ctx.fillRect(left+x*pw/n,
   top+(9-y)*ph/10,pw/n+1,ph/10+1);
 }
 ctx.font='12px sans-serif';
 ctx.fillStyle='#c8e9f3';
 for(let y=0;y<10;y++)
  ctx.fillText(String(y+1),15,top+(9.5-y)*ph/10+4);
 ctx.fillText('km slm',5,18);
 ctx.fillText(ew?'Ovest':'Nord',left,h-20);
 ctx.fillText(ew?'Est':'Sud',w-35,h-20);
 ctx.fillText(
  `${active.width_km.toFixed(0)} km · grigio: dati mancanti`,
  left,18
 );
}

function draw(){
 const use=active&&fresh(active.timestamp);
 original.style.display=use?'none':'';
 panel.style.display=use?'block':'none';
 controls.style.display=use?'flex':'none';
 if(!use){
  foot.textContent=oldFoot+(note?' · '+note:'');
  return;
 }
 const cut=mode.value==='3';
 ruler.style.display=cut?'none':'block';
 canvas.style.display=cut?'none':'block';
 section.style.display=cut?'block':'none';
 $('cap-cut-controls').style.display=cut?'inline':'none';
 const scale=Number($('cap-scale').value);
 $('cap-scale-value').value=scale;
 const rect=panel.getBoundingClientRect();
 const ratio=Math.min(devicePixelRatio||1,1.25);
 const selected=cut?section:canvas;
 selected.width=Math.max(1,Math.round(rect.width*ratio));
 selected.height=Math.max(1,Math.round(rect.height*ratio));
 const time=new Date(active.timestamp).toLocaleTimeString(
  'it-IT',{
   hour:'2-digit',minute:'2-digit',
   timeZone:'Europe/Rome'
  }
 );
 const label=mode.value==='0'?'Volume V12.1':
  mode.value==='1'?'Iso-superficie':
  mode.value==='2'?'Olografico':'Sezione verticale';
 foot.textContent=`Radar-DPC · CC BY-SA 4.0 · CAPPI ${time} Italia · 1–10 km slm · ${label} · ${cut?'strati campionati':'interpolazione tra strati; verticale ×'+scale} · pavimento: massimo CAPPI`;
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
 ctx.font='12px sans-serif';
 ctx.fillStyle='#d8f4ff';
 ctx.strokeStyle='#8cb8cc';
 const dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0);
 const project=km=>{
  const v=[
   -2.38-eye[0],
   height*km/10-eye[1],
   2.38-eye[2]
  ];
  const depth=dot(v,forward);
  return [
   (1+dot(v,right)/(depth*.625)/
    (canvas.width/canvas.height))*canvas.width/2,
   (1-dot(v,up)/(depth*.625))*canvas.height/2
  ];
 };
 ctx.beginPath();
 const a=project(0),b=project(10);
 ctx.moveTo(...a);
 ctx.lineTo(...b);
 ctx.stroke();
 for(let km=0;km<=10;km+=2){
  const [x,y]=project(km);
  ctx.fillText(km+' km',x-38,y+4);
 }
}

async function json(url){
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),12000);
 try{
  const r=await fetch(url,{
   cache:'no-store',signal:controller.signal
  });
  if(!r.ok)throw Error('HTTP '+r.status);
  return await r.json();
 }finally{
  clearTimeout(timer);
 }
}

window.addEventListener('iride:cell',async event=>{
 const token=++sequence;
 const {cell,timestamp}=event.detail;
 active=null;
 opt.disabled=true;
 note='CAPPI in caricamento';
 requestAnimationFrame(draw);
 try{
  const base=new URL('docs/data/dpc3d/',document.baseURI);
  const m=await json(new URL('manifest.json',base));
  if(m.status!=='ok'||m.base_timestamp!==timestamp||
     !fresh(m.timestamp))
   throw Error('CAPPI non sincronizzati o non disponibili');
  const file=m.cells[cell.track_id];
  if(!file||!/^[A-Za-z0-9_-]+\.json$/.test(file))
   throw Error('CAPPI assenti per questa cella');
  const v=await json(new URL(file,base));
  if(token!==sequence)return;
  if(v.base_timestamp!==timestamp||
     v.track_id!==cell.track_id||
     v.timestamp!==m.timestamp||
     !fresh(v.timestamp))
   throw Error('CAPPI incoerenti');
  if(v.side!==32||
     !Number.isFinite(v.width_km)||
     v.width_km<=0||
     v.layers.length!==10)
   throw Error('Volume non valido');
  for(const layer of [...v.layers,v.floor]){
   if(layer.length!==32||
      layer.some(r=>r.length!==32||
       r.some(x=>!Number.isInteger(x)||x<0||x>246)))
    throw Error('Matrice CAPPI non valida');
  }
  upload(v);
  active=v;
  opt.disabled=false;
  note='';
  $('cap-scale').value=Math.max(1,Math.min(30,
   Math.round(v.width_km/20)));
  yaw=-.35;
  pitch=.34;
  distance=8;
  draw();
 }catch(e){
  if(token===sequence){
   note=e.message+' · riserva 2D';
   draw();
  }
 }
});

mode.addEventListener('change',()=>requestAnimationFrame(draw));
$('reset').addEventListener('click',()=>{
 yaw=-.35;
 pitch=.34;
 distance=8;
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
 distance=Math.max(4,Math.min(18,
  distance+e.deltaY*.008));
 draw();
},{passive:false});
let pinch=0;
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
  distance=Math.max(4,Math.min(18,distance*pinch/d));
 pinch=d;
 draw();
},{passive:false});
setInterval(()=>{
 if(active&&!fresh(active.timestamp)){
  active=null;
  opt.disabled=true;
  if(mode.value==='3')mode.value='1';
  note='CAPPI scaduti · riserva 2D';
  draw();
  mode.dispatchEvent(new Event('change'));
 }
},30000);
})();
    
