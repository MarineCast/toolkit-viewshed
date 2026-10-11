const fs=require('fs'),vm=require('vm'),assert=require('assert'),root=process.argv[2];
const load=name=>{const html=fs.readFileSync(root+'/'+name,'utf8');return JSON.parse(html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/)[1])};
const D=load('viewpoints.html');for(const name of ['target_cells.html','source_cells.html'])assert.deepEqual(load(name).raster_overlays,D.raster_overlays);
class E{constructor(){this.value='';this.children=[];this.style={};this.textContent='';this.attrs={};}replaceChildren(){this.children=[]}setAttribute(k,v){this.attrs[k]=v}appendChild(c){this.children.push(c)}}
const els={},group=new E(),ctx=vm.createContext({D,document:{getElementById:id=>els[id]||(els[id]=new E())},atob,Blob,Response,DecompressionStream,fmt:x=>String(x),groupMock:group});
vm.runInContext(`const latScale=Math.cos((D.bounds[1]+D.bounds[3])*.5*Math.PI/180),geoW=(D.bounds[2]-D.bounds[0])*latScale,geoH=D.bounds[3]-D.bounds[1],mapScale=Math.min(960/geoW,660/geoH);const project=([x,y])=>[500+(x-(D.bounds[0]+D.bounds[2])*.5)*latScale*mapScale,350-(y-(D.bounds[1]+D.bounds[3])*.5)*mapScale];let view=[0,0,1000,700];function path(r){return JSON.stringify(r)}function node(tag,attrs,parent=groupMock){const e={tag,attrs,children:[],textContent:''};parent.children.push(e);return e}function applyView(){drawRaster()}`,ctx);
vm.runInContext(fs.readFileSync(__dirname+'/../src/viewshed_toolkit/resources/raster_overlay.js','utf8'),ctx);
(async()=>{const out=[];let prior=null;for(const[source,site]of Object.entries(D.raster_overlays.sites)){
 if(prior)vm.runInContext(`delete globalThis.VIEWSHED_RASTERS[${JSON.stringify(prior)}]`,ctx);
 vm.runInContext(fs.readFileSync(root+'/'+site.path,'utf8'),ctx);await vm.runInContext(`loadRaster(${JSON.stringify(source)})`,ctx);
 els['raster-site'].value=source;els['raster-opacity'].value='75';els['raster-layer'].value='chm_raw';vm.runInContext('rasterGroup=groupMock;focusRaster()',ctx);
 assert(group.children.length>0);assert(els['raster-legend'].textContent.includes('native30m values'));
 for(const layer of ['dem','dem_original','chm_raw','building','joint','unknown','water','targets']){
  els['raster-layer'].value=layer;vm.runInContext('drawRaster()',ctx);assert(group.children.length>0);assert(!/NaN|undefined/.test(els['raster-legend'].textContent));
 }
 vm.runInContext('rasterPins()',ctx);assert.equal(group.children.filter(c=>c.tag==='circle'&&c.attrs.r===5).length,5);
 for(const p of D.composite_sources[source].samples){const pixel=vm.runInContext(`inspectRaster(${p.lon},${p.lat})`,ctx);assert(pixel&&Number.isFinite(pixel.dem)&&Number.isFinite(pixel.dem_original));assert(els['raster-pixel'].textContent.includes('m AGL'));assert(els['raster-pixel'].textContent.includes('m elevation'));}
 const missing=vm.runInContext(`(()=>{const a=activeRaster;for(let r=0;r<a.height;r++)for(let c=0;c<a.width;c++)if(rasterValue(a,'chm_raw',r,c)===255){const t=a.transform;return fromUTM(t[2]+(c+.5)*t[0],t[5]+(r+.5)*t[4])}return null})()`,ctx);
 assert(missing);vm.runInContext(`inspectRaster(${missing[0]},${missing[1]})`,ctx);assert(els['raster-pixel'].textContent.includes('NO DATA (stored255)'));
 vm.runInContext('inspectRaster(-130,45)',ctx);assert(els['raster-pixel'].textContent.includes('Outside'));
 out.push({source,layers:8,native_source_points:D.composite_sources[source].samples.length,nodata_checked:true,actual_source_pins:5,projection_and_legend_checked:true});prior=source;
 }
 console.log(JSON.stringify({status:'passed',method:'Node VM synthetic DOM and actual compressed native assets; rendered visual QA unverified',results:out},null,2));
})().catch(e=>{console.error(e);process.exit(1)});
