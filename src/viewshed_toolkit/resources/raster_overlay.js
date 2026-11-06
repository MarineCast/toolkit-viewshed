// Display-only raster diagnostics. Native values never enter scientific table calculations.
globalThis.VIEWSHED_RASTERS=globalThis.VIEWSHED_RASTERS||{};
const rasterSite=document.getElementById('raster-site'),rasterLayer=document.getElementById('raster-layer'),rasterOpacity=document.getElementById('raster-opacity'),rasterCells=document.getElementById('raster-cells');
let activeRaster=null,rasterGroup=null,rasterLoading=0;
function toUTM(lon,lat){
 const a=6378137,f=1/298.257223563,e=f*(2-f),ep=e/(1-e),k=.9996,p=lat*Math.PI/180,l=(lon+123)*Math.PI/180;
 const s=Math.sin(p),c=Math.cos(p),t=Math.tan(p),N=a/Math.sqrt(1-e*s*s),T=t*t,C=ep*c*c,A=c*l;
 const M=a*((1-e/4-3*e*e/64-5*e**3/256)*p-(3*e/8+3*e*e/32+45*e**3/1024)*Math.sin(2*p)+(15*e*e/256+45*e**3/1024)*Math.sin(4*p)-35*e**3/3072*Math.sin(6*p));
 return[500000+k*N*(A+(1-T+C)*A**3/6+(5-18*T+T*T+72*C-58*ep)*A**5/120),k*(M+N*t*(A*A/2+(5-T+9*C+4*C*C)*A**4/24+(61-58*T+T*T+600*C-330*ep)*A**6/720))];
}
function fromUTM(x,y){
 const a=6378137,f=1/298.257223563,e=f*(2-f),ep=e/(1-e),k=.9996,e1=(1-Math.sqrt(1-e))/(1+Math.sqrt(1-e)),mu=(y/k)/(a*(1-e/4-3*e*e/64-5*e**3/256));
 const p=mu+(3*e1/2-27*e1**3/32)*Math.sin(2*mu)+(21*e1*e1/16-55*e1**4/32)*Math.sin(4*mu)+151*e1**3/96*Math.sin(6*mu)+1097*e1**4/512*Math.sin(8*mu);
 const s=Math.sin(p),c=Math.cos(p),t=Math.tan(p),N=a/Math.sqrt(1-e*s*s),R=a*(1-e)/(1-e*s*s)**1.5,T=t*t,C=ep*c*c,Q=(x-500000)/(N*k);
 return[-123+(Q-(1+2*T+C)*Q**3/6+(5-2*C+28*T-3*C*C+8*ep+24*T*T)*Q**5/120)/c*180/Math.PI,(p-N*t/R*(Q*Q/2-(5+3*T+10*C-4*C*C-9*ep)*Q**4/24+(61+90*T+298*C+45*T*T-252*ep-3*C*C)*Q**6/720))*180/Math.PI];
}
function unproject(x,y){return[(x-500)/(latScale*mapScale)+(D.bounds[0]+D.bounds[2])*.5,(350-y)/mapScale+(D.bounds[1]+D.bounds[3])*.5]}
function rasterValue(a,key,row,col){
 if(row<0||col<0||row>=a.height||col>=a.width)return null;
 if(key==='dem_original')return rasterValue(a,'dem',row,col)+rasterValue(a,'dem_original_delta',row,col);
 const layer=a.arrays[key],i=row*a.width+col;if(row<0||col<0||row>=a.height||col>=a.width)return null;
 if(layer.values)return layer.values[i];
 return layer.dtype==='uint8'?layer.bytes[i]:new DataView(layer.bytes.buffer,layer.bytes.byteOffset,layer.bytes.byteLength).getFloat32(i*4,true);
}
function rasterPixel(a,lon,lat){
 const[x,y]=toUTM(lon,lat),t=a.transform,col=Math.floor((x-t[2])/t[0]),row=Math.floor((y-t[5])/t[4]);
 if(row<0||col<0||row>=a.height||col>=a.width)return null;
 const get=k=>rasterValue(a,k,row,col),flags=get('flags');return{row,col,x,y,dem:get('dem'),dem_original:get('dem_original'),chm_raw:get('chm_raw'),chm_lower:get('chm_lower'),building:get('building'),joint:get('joint'),flags};
}
async function loadRaster(source){
 if(activeRaster?.source===source)return activeRaster;
 const config=D.raster_overlays?.sites[source];if(!config)throw Error('No qualified raster asset for this source');
 if(!globalThis.VIEWSHED_RASTERS[source])await new Promise((resolve,reject)=>{const s=document.createElement('script');s.src=config.path;s.onload=resolve;s.onerror=()=>reject(Error('Local raster asset missing. Extract the whole ZIP with raster_inputs/ beside the HTMLs.'));document.body.appendChild(s)});
 const raw=globalThis.VIEWSHED_RASTERS[source],a={...raw,arrays:Object.fromEntries(Object.entries(raw.arrays).map(([key,layer])=>[key,{...layer}]))};if(a.crs!=='EPSG:32610'||a.contract!=='native_30m_inspection_display_preview_v1')throw Error('Unsupported or stale raster grid');
 for(const layer of Object.values(a.arrays))if(!layer.bytes&&!layer.values){
  if(typeof DecompressionStream==='undefined')throw Error('This browser cannot decode native pixel values. Use a current browser with DecompressionStream support.');
  const packed=Uint8Array.from(atob(layer.zlib_base64),c=>c.charCodeAt(0));layer.bytes=new Uint8Array(await new Response(new Blob([packed]).stream().pipeThrough(new DecompressionStream('deflate'))).arrayBuffer());if(layer.bytes.length!==layer.native_bytes)throw Error('Native raster byte length mismatch');
 }
 return a;
}
function rawRaster(value){return value===null?'No data':String(value)}
function inspectRaster(lon,lat){
 const out=document.getElementById('raster-pixel');if(!activeRaster){out.textContent='Choose a raster layer to load native pixel inspection.';return null}
 const p=rasterPixel(activeRaster,lon,lat);if(!p){out.textContent='Outside the selected native raster grid; no values.';return null}
 const mark=(bit,yes,no)=>(p.flags&bit)?yes:no;
 out.textContent=`Native pixel row ${p.row}, column ${p.col} · ${activeRaster.native_pixel_m} m · EPSG:32610\nModel DEM ${rawRaster(p.dem)} m elevation; original projected DEM ${rawRaster(p.dem_original)} m elevation (declared DEM reference; modeled water flattened=0)\nCHM raw ${p.chm_raw===255?'NO DATA (stored255)':fmt(p.chm_raw)+' m AGL'}; model lower ${rawRaster(p.chm_lower)} m AGL; ${mark(1,'unknown canopy upper','canopy support resolved')}\nBuilding raster prediction floor ${rawRaster(p.building)} m AGL; ${mark(8,'published footprint touches pixel','no published footprint; absence unverified on land')}; ${mark(2,'unknown building upper','building support resolved')}; ${mark(16,'missing-height overlap','no flagged missing-height overlap')}\nJoint lower max ${rawRaster(p.joint)} m AGL; ${mark(4,'UNKNOWN upper support','resolved upper support')}\n${mark(32,'Qualified full-water footprint','Not qualified full-water footprint')} · ${mark(128,'target-water raster center','not a target-water raster center')} · ${mark(64,'BUILDING/WATER CONFLICT: raw prediction is not a proven floor; joint lower is0','no flagged building/water conflict')}\nValues are the unmodified base input grids. Selected observer applies private30m canopy clearance and central-cell grounding during LOS only; neighboring roofs are retained.`;
 return p;
}
function rasterColor(value,preview,missing){if(missing)return'rgba(210,60,170,.8)';const q=Math.max(0,Math.min(1,(value-preview.min)/Math.max(1,preview.max-preview.min)));return`rgb(${Math.round(235-q*205)},${Math.round(245-q*95)},${Math.round(220-q*170)})`}
function drawRaster(){
 if(!rasterGroup)return;rasterGroup.replaceChildren();if(!activeRaster||rasterLayer.value==='none')return;
 const a=activeRaster,key=rasterLayer.value,p=a.previews[key],b=p.bounds,[left,top]=project([b[0],b[3]]),[right,bottom]=project([b[2],b[1]]);rasterGroup.setAttribute('opacity',Number(rasterOpacity.value)/100);
 const corners=[[view[0],view[1]],[view[0]+view[2],view[1]+view[3]]].map(xy=>toUTM(...unproject(...xy))),t=a.transform;
 const c0=Math.max(0,Math.floor((Math.min(corners[0][0],corners[1][0])-t[2])/t[0])-2),c1=Math.min(a.width,Math.ceil((Math.max(corners[0][0],corners[1][0])-t[2])/t[0])+2),r0=Math.max(0,Math.floor((Math.max(corners[0][1],corners[1][1])-t[5])/t[4])-2),r1=Math.min(a.height,Math.ceil((Math.min(corners[0][1],corners[1][1])-t[5])/t[4])+2);
 const count=Math.max(0,r1-r0)*Math.max(0,c1-c0),native=count>0&&count<=6000;
 if(native){
  for(let r=r0;r<r1;r++)for(let c=c0;c<c1;c++){
   const flags=rasterValue(a,'flags',r,c),v=key==='unknown'?((flags&4)?1:0):key==='water'?((flags&32)?1:0):key==='targets'?((flags&128)?1:0):rasterValue(a,key,r,c),missing=key==='chm_raw'?v===255:key==='building'?(flags&2)!==0:false;
   const rings=[[[c,r],[c+1,r],[c+1,r+1],[c,r+1]].map(([col,row])=>fromUTM(t[2]+col*t[0],t[5]+row*t[4]))];node('path',{d:path(rings),fill:key==='unknown'?(v?'#8c4fb5':'transparent'):['water','targets'].includes(key)?(v?'#087ea4':'transparent'):rasterColor(v,p,missing),stroke:'none'},rasterGroup);
  }
 }else node('image',{x:left,y:top,width:right-left,height:bottom-top,href:p.image,preserveAspectRatio:'none'},rasterGroup);
 document.getElementById('raster-legend').textContent=`${p.label} · ${p.units} · color scale ${fmt(p.min)}–${fmt(p.max)} · magenta=missing height/support; purple=unknown-upper mask; blue=water/sample mask. ${native?'Native30m cells in this view.':'Display preview '+p.width+'×'+p.height+' nearest-neighbor WGS84 reprojection; not the native grid.'} Pixel inspection always uses exact native30m values. No display interpolation enters LOS.`;
}
function rasterPins(){
 if(!D.raster_overlays||rasterLayer.value==='none')return;
 const source=rasterSite.value,src=D.composite_sources[source];for(const p of src.samples){const[x,y]=project([p.lon,p.lat]);const pin=node('circle',{cx:x,cy:y,r:5,class:'raster-pin',fill:'#ed3c21',stroke:'#fff'});node('title',{},pin).textContent=p.sample_id+' · actual source point; central raster-cell ground +1.7m';}
 for(const[key,p]of Object.entries(D.points))if(D.raster_overlays.product_sites[key]===source){const[x,y]=project([p.lon,p.lat]);const pin=node('circle',{cx:x,cy:y,r:7,class:'raster-pin',fill:'#ffaf36',stroke:'#321'});node('title',{},pin).textContent=p.name+' · secondary exact observer';}
}
function focusRaster(){
 const coords=D.composite_sources[rasterSite.value].samples.map(p=>project([p.lon,p.lat])),xs=coords.map(p=>p[0]),ys=coords.map(p=>p[1]),left=Math.min(...xs),right=Math.max(...xs),top=Math.min(...ys),bottom=Math.max(...ys),width=Math.max(right-left,(bottom-top)/.7,5)*1.4;view=[(left+right-width)/2,(top+bottom-width*.7)/2,width,width*.7];applyView();
}
async function changeRaster(){
 const request=++rasterLoading;
 if(rasterLayer.value==='none'){activeRaster=null;render();document.getElementById('raster-pixel').textContent='Raster layers off; no active inspector.';document.getElementById('raster-legend').textContent='Raster layers off.';return}
 activeRaster=null;
 document.getElementById('raster-pixel').textContent='Loading selected raster; no active inspector.';
 document.getElementById('raster-legend').textContent='Loading shared local raster input values…';
 try{const a=await loadRaster(rasterSite.value);if(request!==rasterLoading)return;activeRaster=a;render();document.getElementById('raster-provenance').textContent=JSON.stringify(a.provenance,null,2);}catch(e){if(request===rasterLoading)document.getElementById('raster-legend').textContent=String(e.message)}
}
function setupRasterControls(){
 document.getElementById('raster-controls').hidden=!D.raster_overlays;if(!D.raster_overlays)return;
 rasterSite.replaceChildren();for(const[source,site]of Object.entries(D.raster_overlays.sites)){node('option',{value:source},rasterSite).textContent=site.name;}
 rasterSite.value=Object.keys(D.raster_overlays.sites)[0];rasterLayer.value='none';rasterOpacity.value='75';rasterCells.checked=true;
 rasterSite.addEventListener('change',()=>{if(D.kind==='point'){point.value=rasterSite.value;surface.value='retained'}changeRaster()});rasterLayer.addEventListener('change',changeRaster);rasterOpacity.addEventListener('input',drawRaster);rasterCells.addEventListener('change',render);document.getElementById('raster-focus').onclick=focusRaster;
 svg.addEventListener('pointermove',e=>{if(drag||rasterLayer.value==='none')return;const xy=screenToView(e.clientX,e.clientY);if(!xy){document.getElementById('raster-pixel').textContent='Outside the map image; no values.';return}inspectRaster(...unproject(...xy))});
 point.addEventListener('change',()=>{const source=D.raster_overlays.product_sites[point.value]??point.value;if(D.raster_overlays.sites[source]){rasterSite.value=source;if(rasterLayer.value!=='none')changeRaster()}});
}
