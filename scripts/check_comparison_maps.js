const fs=require('fs'),vm=require('vm'),assert=require('assert');
const root=process.argv[2],out=[];
for(const [name,kind,size] of [['target_cells.html','target',null],['source_cells.html','source',null],['viewpoints.html','point',null]]){
const html=fs.readFileSync(root+'/'+name,'utf8'),D=JSON.parse(html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/)[1]),script=html.match(/<\/script><script>([\s\S]*?)<\/script>/)[1];
class TextNode{constructor(text){this.nodeType=3;this.textContent=text;}get childNodes(){return[]}}
class Element{constructor(){this.nodeType=1;this.attrs={};this.children=[];this.style={};this.listeners={};this.listenerLists={};this.value='';this.options=[];this.selectedIndex=0;this.hidden=false;}get childNodes(){return this.children}get textContent(){return this.children.map(c=>c.textContent).join('')}set textContent(text){this.children=[new TextNode(text)]}setAttribute(k,v){this.attrs[k]=v}appendChild(c){this.children.push(c)}prepend(c){this.children.unshift(c)}replaceChildren(){this.children=[]}addEventListener(k,v){const list=this.listenerLists[k]||(this.listenerLists[k]=[]);if(!list.includes(v))list.push(v);this.listeners[k]=e=>{for(const callback of [...list])callback(e)}}getBoundingClientRect(){return{left:0,top:0,width:1000,height:700}}setPointerCapture(){}}
// Parse the real packaged footer into text/anchor nodes. Like DOM textContent,
// setting Element.textContent removes descendants, so lost links are observable.
const footer=new Element(),badge=new Element();
const paragraphs=[...html.matchAll(/<footer>[\s\S]*?<\/footer>/g)][0][0].match(/<p>[\s\S]*?<\/p>/g).map(body=>{
 const p=new Element();for(const part of body.slice(3,-4).split(/(<a\s[^>]*>[\s\S]*?<\/a>)/g)){
  if(part.startsWith('<a ')){const a=new Element();a.setAttribute('href',part.match(/href="([^"]+)"/)[1]);a.appendChild(new TextNode(part.replace(/<[^>]*>/g,'')));p.appendChild(a);}else p.appendChild(new TextNode(part.replace(/<[^>]*>/g,'')));
 }footer.appendChild(p);return p;
});
const anchors=()=>paragraphs.flatMap(p=>p.children.filter(c=>c.attrs&&c.attrs.href));
const originalAnchors=anchors(),originalHrefs=originalAnchors.map(a=>a.attrs.href);

const els={};for(const id of ['payload','map','scene','tip','metric','stat','point','status','max','reset','stat-label','point-label','reference','scope','surface','surface-label','view-mode','view-label','coverage-headline','coverage-detail','totals','guide','bound-actions','show-coverage','show-lower','show-upper','legend-text','sampling','raster-site','raster-layer','raster-opacity','raster-cells','raster-controls','raster-pixel','raster-legend','raster-provenance','raster-focus'])els[id]=new Element();
els.payload.textContent=JSON.stringify(D);els.metric.options=['Canopy','Terrain','Distance','Vegetation','Combined','Lower','Upper'].map(text=>({text}));els.surface.value='retained';els['view-mode'].options=['known','coverage','resolved','lower','upper'].map(value=>({value}));els.stat.options=['pooled','sum','mean','available'].map(value=>({value}));
const ctx=vm.createContext({document:{getElementById:id=>els[id],createElementNS:()=>new Element(),createElement:()=>new Element(),querySelector:s=>s==='.badge'?badge:s==='footer'?footer:paragraphs[0],querySelectorAll:()=>paragraphs}});vm.runInContext(script,ctx);
assert.deepEqual(anchors().map(a=>a.attrs.href),originalHrefs);assert.deepEqual(anchors(),originalAnchors);assert(originalHrefs.includes('PILOT_MAP_MANIFEST.json'));assert(originalHrefs.some(h=>h.includes('doi.org')));assert(originalHrefs.some(h=>h.includes('creativecommons.org')));
const cells=()=>els.scene.children.filter(c=>c.attrs.class==='cell');
const finite=()=>{for(const id of ['status','coverage-headline','coverage-detail','totals','guide','scope','sampling'])assert(!/NaN|undefined/.test(els[id].textContent),id+' '+els[id].textContent);};
if(D.raster_overlays){assert.equal(els['raster-site'].children.length,Object.keys(D.raster_overlays.sites).length);assert(!els['raster-controls'].hidden);if(kind==='point')assert(els.point.listenerLists.change.length>=2);}
// Non-10:7 viewport: xMidYMid meet leaves 250px horizontal letterboxes.
els.map.getBoundingClientRect=()=>({left:10,top:20,width:1500,height:700});
let xy=vm.runInContext('screenToView(1010,370)',ctx);assert.equal(xy[0],750);assert.equal(xy[1],350);
assert.equal(vm.runInContext('screenToView(110,370)',ctx),null);
els.map.getBoundingClientRect=()=>({left:0,top:0,width:1000,height:1000});
xy=vm.runInContext('screenToView(500,325)',ctx);assert.equal(xy[0],500);assert.equal(xy[1],175);
assert.equal(vm.runInContext('screenToView(500,50)',ctx),null);
els.map.getBoundingClientRect=()=>({left:0,top:0,width:1000,height:700});
if(D.raster_overlays){els['raster-focus'].onclick();const before=vm.runInContext('view[2]',ctx);els.map.listeners.wheel({clientX:500,clientY:350,deltaY:-1,preventDefault(){}});assert(vm.runInContext('view[2]',ctx)<before);}
vm.runInContext('view=[0,0,1000,700];applyView()',ctx);
assert.equal(D.kind,kind);const composites=Object.keys(D.composite_sources);const expected=kind==='point'?D.viewpoints[composites[0]].rows.length:kind==='source'?composites.length:new Set(composites.flatMap(p=>D.viewpoints[p].rows.map(r=>r.target_h3))).size;assert.equal(cells().length,expected);finite();
for(const metric of ['canopy_mean','terrain','distance','vegetation','combined','lower','upper'])for(const mode of ['known','coverage','resolved','lower','upper'])for(const stat of ['pooled','sum','mean','available']){
 els.metric.value=metric;els['view-mode'].value=mode;els.stat.value=stat;els.metric.listeners.change();finite();const c=cells()[0];c.listeners.pointermove({clientX:50,clientY:50});assert(!/NaN|undefined/.test(els.tip.textContent),els.tip.textContent);
}
// Bound metrics visibly lock View; Combined restores every supported View choice.
let boundViews=0;
for(const bound of ['lower','upper'])for(const requested of ['known','coverage','resolved','lower','upper']){
 els.metric.value=bound;els['view-mode'].value=requested;els.metric.listeners.change();
 assert(els['view-mode'].disabled);assert.equal(els['view-mode'].value,bound);assert.equal(vm.runInContext('mode()',ctx),bound);boundViews++;
}
for(const requested of ['coverage','resolved','lower','upper']){
 els.metric.value='combined';els['view-mode'].value=requested;els.metric.listeners.change();
 assert(!els['view-mode'].disabled);assert.equal(els['view-mode'].value,requested);assert.equal(vm.runInContext('mode()',ctx),requested);boundViews++;
}
// A known-only selector is meaningful only for arithmetic canopy LOS.
els.metric.value='vegetation';els['view-mode'].value='known';els.stat.value='sum';els.metric.listeners.change();
assert.equal(els['view-mode'].value,'coverage');assert(els['view-mode'].options.find(o=>o.value==='known').disabled);
const metricRows=kind==='point'?D.viewpoints[els.point.value].rows:D.rows;
metricRows.forEach((r,i)=>{if(r.sample_comparisons_total>0&&(kind==='point'?r.weight_terrain===0:r[(D.primary_visibility==='above_ground_obstruction'?'weight_obstruction':'weight_vegetation')+'_applicable_count']===0))assert.equal(cells()[i].attrs['data-state'],'not_applicable');
 if(kind!=='point'&&r[(D.primary_visibility==='above_ground_obstruction'?'weight_obstruction':'weight_vegetation')+'_sum']===null)assert(!['resolved','resolved_zero'].includes(cells()[i].attrs['data-state']));});
let fabdem=0,transitions=0;
if(kind==='source'){els.metric.value='canopy_mean';els['view-mode'].value='known';els.metric.listeners.change();assert(els['coverage-detail'].textContent.includes('Source cells are colored'));assert(els.guide.textContent.includes('each source cell'));assert(!els.guide.textContent.includes('each target cell'));}
if(kind==='point')for(const exact of Object.keys(D.viewpoints).filter(p=>!D.composite_sources[p]))for(const scenario of ['-1','0','1'])for(const composite of composites){
 els.point.value=exact;els.surface.value=scenario;els.metric.value='terrain';els.point.listeners.change();assert(els.reference.textContent.includes('FABDEM'));assert.equal(vm.runInContext('metricName()',ctx),'weight_terrain');
 els.point.value=composite;els.point.listeners.change();finite();assert.equal(els.surface.value,'retained');assert.equal(vm.runInContext('metricName()',ctx),'ui_terrain_mean');assert.equal(els.reference.textContent,D.terrain_reference);assert(els['coverage-headline'].textContent.includes('arithmetic'));assert(els['coverage-detail'].textContent.includes('Bare-earth binary LOS'));assert.equal(cells().length,D.viewpoints[composite].rows.length);transitions++;
}

if(kind==='point')for(const source of Object.keys(D.viewpoints)){
 els.point.value=source;els.surface.value='retained';els.metric.value='canopy_mean';els.point.listeners.change();finite();assert.equal(cells().length,D.viewpoints[source].rows.length);

 if(!D.composite_sources[source]&&D.primary_visibility==='above_ground_obstruction'){
  D.viewpoints[source].rows.forEach((r,i)=>{cells()[i].listeners.pointermove({clientX:50,clientY:50});assert(els.tip.textContent.includes('Joint obstruction provenance: '+(r.obstruction_status??'unavailable comparison')));assert(els.tip.textContent.includes('Canopy-only combined provenance: '+(r.combined_status??'unavailable comparison')));});
 }
 if(D.composite_sources[source])assert.equal(els.scene.children.filter(c=>c.attrs.r===4).length,D.composite_sources[source].samples.length);
 else for(const surface of ['-1','0','1'])for(const metric of ['terrain','distance','vegetation','combined','lower','upper','canopy_mean']){els.surface.value=surface;els.metric.value=metric;els.surface.listeners.change();finite();if(metric==='terrain'){assert(els['coverage-headline'].textContent.includes('distance-integrated'));assert(els['coverage-detail'].textContent.includes('not an arithmetic'));}fabdem++;}
}
assert(!/fetch\(|XMLHttpRequest/.test(script));out.push({file:name,kind,cells:expected,interaction_selections:140,fabdem_selections:fabdem,scenario_to_composite_transitions:transitions,bound_view_interactions:boundViews,footer_links_preserved:originalHrefs.length,joint_mode:D.primary_visibility==='above_ground_obstruction',status:'passed'});
}
console.log(JSON.stringify({status:'passed',method:'Node VM synthetic DOM; rendered visual QA unverified',results:out},null,2));
