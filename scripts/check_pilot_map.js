const fs=require('fs'),vm=require('vm'),assert=require('assert'),file=process.argv[2],html=fs.readFileSync(file,'utf8'),data=JSON.parse(html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/)[1]),script=html.match(/<\/script><script>([\s\S]*?)<\/script>/)[1];
const waits={},els={},rect={left:10,top:20,width:1440,height:900};
class E{constructor(){this.attrs={};this.children=[];this.listeners={};this.style={};this.textContent='';this.hidden=false}setAttribute(k,v){this.attrs[k]=String(v)}appendChild(e){this.children.push(e);return e}replaceChildren(){this.children=[]}addEventListener(k,v){(this.listeners[k]||(this.listeners[k]=[])).push(v)}fire(k,e={}){for(const f of this.listeners[k]||[])f(e)}getBoundingClientRect(){return rect}setPointerCapture(){this.captured=true}getContext(){return{createImageData:(w,h)=>({data:new Uint8ClampedArray(w*h*4)}),putImageData(){}}}toDataURL(){return'data:image/png;base64,mock'}}
const document={getElementById:id=>els[id]||(els[id]=new E()),createElementNS:()=>new E(),createElement:()=>new E(),body:{appendChild(s){(waits[s.src]||(waits[s.src]=[])).push(s.onload)}}};
els.payload=new E();els.payload.textContent=JSON.stringify(data);
const ctx=vm.createContext({document,window:{addEventListener(){}},atob,Blob,Response,DecompressionStream});vm.runInContext(script,ctx);
const run=s=>vm.runInContext(s,ctx),all=e=>[e,...e.children.flatMap(all)],find=id=>all(els['legend-body']).find(e=>e.attrs['data-layer']===id),select=(id,on)=>{const e=find(id);assert(e,id);e.checked=on;e.fire('change')},groups=()=>els.scene.children.filter(e=>e.attrs['data-layer']).map(e=>e.attrs['data-layer']);
function complete(source){const site=data.raster_overlays.sites[source];vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(file),site.path),'utf8'),ctx);for(const f of waits[site.path]||[])f();delete waits[site.path]}
const tick=()=>new Promise(r=>setTimeout(r,0));
(async()=>{
 assert(!/<select|<nav|<header/.test(html));assert(html.includes('height:100dvh'));assert(html.includes('overflow:hidden'));assert(html.includes('overscroll-behavior:contain'));
 assert.deepEqual(groups(),['target:obstruction']);assert.equal(run('sources.length'),3);
 select('target:distance',true);select('source:terrain',true);select('composite:combined',true);assert.equal(groups().length,4);const weight=els.scene.children.find(e=>e.attrs['data-layer']==='target:distance').children[0];weight.fire('click',{clientX:500,clientY:350,stopPropagation(){}});assert(all(els['popup-content']).some(e=>e.textContent.startsWith('Complete mean:')));
 select('target:obstruction',false);assert(!groups().includes('target:obstruction'));
 els['legend-toggle'].onclick();assert(els['legend-body'].hidden);assert.equal(els['legend-toggle'].attrs['aria-expanded'],'false');els['legend-toggle'].onclick();assert(!els['legend-body'].hidden);
 const before=run('view.join()');let stopped=false;els.legend.fire('wheel',{stopPropagation(){stopped=true}});assert(stopped);assert.equal(run('view.join()'),before);
 // Resize across actual laptop and phone aspect ratios; SVG viewBox fills each.
 for(const[w,h]of [[1440,900],[1366,768],[390,844],[844,390]]){rect.width=w;rect.height=h;run('resize()');const v=run('[...view]');assert(Math.abs(v[3]/v[2]-h/w)<1e-12);const xy=run(`screenToView(${rect.left+w/2},${rect.top+h/2})`);assert(Math.abs(xy[0]-(v[0]+v[2]/2))<1e-8);assert(Math.abs(xy[1]-(v[1]+v[3]/2))<1e-8)}
 // Reproduce letterboxing deliberately, independently of resize observer.
 rect.width=1500;rect.height=700;run('view=[0,0,1000,700]');assert.equal(run('screenToView(110,370)'),null);const xy=run('screenToView(1010,370)');assert.equal(xy[0],750);assert.equal(xy[1],350);
 rect.width=1440;rect.height=900;run('resize();home()');
 const ids=Object.keys(data.composite_sources),sourceShapes=()=>els.scene.children.filter(e=>e.attrs.class==='source-shape');sourceShapes()[0].fire('click',{stopPropagation(){}});assert.equal(run('selectedSource'),ids[0]);assert(!els.popup.hidden);
 els.map.fire('pointerdown',{clientX:rect.left+rect.width/2,clientY:rect.top+rect.height/2,pointerId:1});assert(!els.map.captured);els.map.fire('pointerup');
 const pin=els.scene.children.find(e=>e.attrs.class==='source-pin');pin.fire('click',{stopPropagation(){}});assert.equal(run('selectedSource'),ids[0]);
 els.focus.onclick();const width=run('view[2]');els.map.fire('wheel',{clientX:rect.left+rect.width/2,clientY:rect.top+rect.height/2,deltaY:-1,preventDefault(){}});assert(run('view[2]')<width);
 run('home()');select('raster:dem',true);const loadingA=run('syncRaster()');assert.equal(run('activeRaster'),null);const first=run('requestId');run(`selectSource(${JSON.stringify(ids[1])},false)`);const loadingB=run('syncRaster()');const second=run('requestId');assert(second>first);
 complete(ids[1]);await loadingB; // Actual native loader completes B before A.
 assert.equal(run('activeRaster.source'),ids[1]);
 complete(ids[0]);await loadingA;assert.equal(run('activeRaster.source'),ids[1]);
 // All layers Off during an unresolved load cancels activation.
 run(`delete VIEWSHED_RASTERS[${JSON.stringify(ids[2])}];selectSource(${JSON.stringify(ids[2])},false)`);const loadingOff=run('syncRaster()');select('raster:dem',false);complete(ids[2]);await loadingOff;assert.equal(run('activeRaster'),null);
 // Multiple rasters remain selected together; inspector matches selected site.
 select('raster:dem',true);select('raster:chm_raw',true);select('raster:building',true);await run('syncRaster()');assert.equal(run('activeRaster.source'),ids[2]);assert(groups().includes('raster:dem'));assert(groups().includes('raster:chm_raw'));assert(groups().includes('raster:building'));
 const p=data.composite_sources[ids[2]].samples[0],pixel=run(`rasterPixel(activeRaster,${p.lon},${p.lat})`);assert(pixel&&Number.isFinite(pixel.dem));const message=run(`rasterText(rasterPixel(activeRaster,${p.lon},${p.lat}))`).join(' ');assert(message.includes('m elevation'));assert(message.includes('m AGL'));assert(message.includes('Native row'));
 const position=run(`project([${p.lon},${p.lat}])`),v=run('[...view]'),event={clientX:rect.left+(position[0]-v[0])/v[2]*rect.width,clientY:rect.top+(position[1]-v[1])/v[3]*rect.height};els.map.fire('pointermove',event);assert(els.readout.textContent.includes('row'+pixel.row+', col'+pixel.col));els.map.fire('click',event);assert(all(els['popup-content']).some(e=>e.textContent.includes('Native row '+pixel.row)));
 rect.width=1500;rect.height=700;run('view=[0,0,1000,700]');els.map.fire('pointermove',{clientX:110,clientY:370});assert.equal(els.readout.textContent,'Outside map image');
 const zero=run("metric({sample_comparisons_total:5,weight_distance:0},'distance')"),empty=run("metric({sample_comparisons_total:0,weight_distance:0},'distance')"),unknown=run("metric({sample_comparisons_total:5,weight_obstruction_combined:null},'combined')");assert.equal(zero,0);assert.equal(empty,null);assert.equal(unknown,null);
 assert(!/fetch\(|XMLHttpRequest/.test(script));console.log(JSON.stringify({status:'passed',multiple_layers:true,legend_collapse:true,source_geometry_and_pin_click:true,viewport_sizes:[[1440,900],[1366,768],[390,844],[844,390]],aspect_hover:true,letterbox_outside_rejected:true,real_loader_reversed_completion:true,off_during_load:true,legend_scroll_without_zoom:true,source_focus_then_zoom:true,native_values_and_units:true,unknown_not_zero:true,method:'Node VM actual renderer, synthetic DOM and native assets; rendered visual QA unverified'},null,2));
})().catch(e=>{console.error(e);process.exit(1)});
