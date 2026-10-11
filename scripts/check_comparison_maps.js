const fs=require('fs'),vm=require('vm'),assert=require('assert');
const root=process.argv[2],out=[];
for(const [name,kind,size] of [['target_cells.html','target',null],['source_cells.html','source',null],['viewpoints.html','point',null]]){
const html=fs.readFileSync(root+'/'+name,'utf8'),D=JSON.parse(html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/)[1]),script=html.match(/<\/script><script>([\s\S]*?)<\/script>/)[1];
class Element{constructor(){this.attrs={};this.children=[];this.style={};this.listeners={};this.textContent='';this.value='';this.options=[];this.selectedIndex=0;this.hidden=false;}setAttribute(k,v){this.attrs[k]=v}appendChild(c){this.children.push(c)}replaceChildren(){this.children=[]}addEventListener(k,v){this.listeners[k]=v}getBoundingClientRect(){return{left:0,top:0,width:1000,height:700}}setPointerCapture(){}}
const els={};for(const id of ['payload','map','scene','tip','metric','stat','point','status','max','reset','stat-label','point-label','reference','scope','surface','surface-label','view-mode','view-label','coverage-headline','coverage-detail','totals','guide','bound-actions','show-coverage','show-lower','show-upper','legend-text','sampling'])els[id]=new Element();
els.payload.textContent=JSON.stringify(D);els.metric.options=['Canopy','Terrain','Distance','Vegetation','Combined','Lower','Upper'].map(text=>({text}));els.surface.value='retained';els['view-mode'].options=['known','coverage','resolved','lower','upper'].map(value=>({value}));els.stat.options=['pooled','sum','mean','available'].map(value=>({value}));
const ctx=vm.createContext({document:{getElementById:id=>els[id],createElementNS:()=>new Element()}});vm.runInContext(script,ctx);
const cells=()=>els.scene.children.filter(c=>c.attrs.class==='cell');
const finite=()=>{for(const id of ['status','coverage-headline','coverage-detail','totals','guide','scope','sampling'])assert(!/NaN|undefined/.test(els[id].textContent),id+' '+els[id].textContent);};
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
metricRows.forEach((r,i)=>{if(r.sample_comparisons_total>0&&(kind==='point'?r.weight_terrain===0:r.weight_vegetation_applicable_count===0))assert.equal(cells()[i].attrs['data-state'],'not_applicable');
 if(kind!=='point'&&r.weight_vegetation_sum===null)assert(!['resolved','resolved_zero'].includes(cells()[i].attrs['data-state']));});
let fabdem=0,transitions=0;
if(kind==='source'){els.metric.value='canopy_mean';els['view-mode'].value='known';els.metric.listeners.change();assert(els['coverage-detail'].textContent.includes('Source cells are colored'));assert(els.guide.textContent.includes('each source cell'));assert(!els.guide.textContent.includes('each target cell'));}
if(kind==='point')for(const exact of Object.keys(D.viewpoints).filter(p=>!D.composite_sources[p]))for(const scenario of ['-1','0','1'])for(const composite of composites){
 els.point.value=exact;els.surface.value=scenario;els.metric.value='terrain';els.point.listeners.change();assert(els.reference.textContent.includes('FABDEM'));assert.equal(vm.runInContext('metricName()',ctx),'weight_terrain');
 els.point.value=composite;els.point.listeners.change();finite();assert.equal(els.surface.value,'retained');assert.equal(vm.runInContext('metricName()',ctx),'ui_terrain_mean');assert.equal(els.reference.textContent,D.terrain_reference);assert(els['coverage-headline'].textContent.includes('arithmetic'));assert(els['coverage-detail'].textContent.includes('Bare-earth binary LOS'));assert.equal(cells().length,D.viewpoints[composite].rows.length);transitions++;
}

if(kind==='point')for(const source of Object.keys(D.viewpoints)){
 els.point.value=source;els.surface.value='retained';els.metric.value='canopy_mean';els.point.listeners.change();finite();assert.equal(cells().length,D.viewpoints[source].rows.length);
 if(D.composite_sources[source])assert.equal(els.scene.children.filter(c=>c.attrs.r===4).length,D.composite_sources[source].samples.length);
 else for(const surface of ['-1','0','1'])for(const metric of ['terrain','distance','vegetation','combined','lower','upper','canopy_mean']){els.surface.value=surface;els.metric.value=metric;els.surface.listeners.change();finite();if(metric==='terrain'){assert(els['coverage-headline'].textContent.includes('distance-integrated'));assert(els['coverage-detail'].textContent.includes('not an arithmetic'));}fabdem++;}
}
assert(!/fetch\(|XMLHttpRequest/.test(script));out.push({file:name,kind,cells:expected,interaction_selections:140,fabdem_selections:fabdem,scenario_to_composite_transitions:transitions,bound_view_interactions:boundViews,status:'passed'});
}
console.log(JSON.stringify({status:'passed',method:'Node VM synthetic DOM; rendered visual QA unverified',results:out},null,2));
