const fs=require('fs'),vm=require('vm'),assert=require('assert');
const root=process.argv[2],out=[];
for(const [name,kind,size] of [['target_cells.html','target',null],['source_cells.html','source',null],['viewpoints.html','point',null]]){
const html=fs.readFileSync(root+'/'+name,'utf8'),D=JSON.parse(html.match(/<script id="payload" type="application\/json">([\s\S]*?)<\/script>/)[1]),script=html.match(/<\/script><script>([\s\S]*?)<\/script>/)[1];
class Element{constructor(){this.attrs={};this.children=[];this.style={};this.listeners={};this.textContent='';this.value='';this.options=[];this.selectedIndex=0;this.hidden=false;}setAttribute(k,v){this.attrs[k]=v}appendChild(c){this.children.push(c)}replaceChildren(){this.children=[]}addEventListener(k,v){this.listeners[k]=v}getBoundingClientRect(){return{left:0,top:0,width:1000,height:700}}setPointerCapture(){}}
const els={};for(const id of ['payload','map','scene','tip','metric','stat','point','status','max','reset','stat-label','point-label','reference','scope','surface','surface-label','view-mode','view-label','coverage-headline','coverage-detail','totals','guide','bound-actions','show-coverage','show-lower','show-upper','legend-text','sampling'])els[id]=new Element();
els.payload.textContent=JSON.stringify(D);els.metric.options=['Canopy','Terrain','Distance','Vegetation','Combined','Lower','Upper'].map(text=>({text}));els.surface.value='retained';
vm.runInContext(script,vm.createContext({document:{getElementById:id=>els[id],createElementNS:()=>new Element()}}));
const cells=()=>els.scene.children.filter(c=>c.attrs.class==='cell');
const finite=()=>{for(const id of ['status','coverage-headline','coverage-detail','totals','guide','scope','sampling'])assert(!/NaN|undefined/.test(els[id].textContent),id+' '+els[id].textContent);};
assert.equal(D.kind,kind);const composites=Object.keys(D.composite_sources);const expected=kind==='point'?D.viewpoints[composites[0]].rows.length:kind==='source'?composites.length:new Set(composites.flatMap(p=>D.viewpoints[p].rows.map(r=>r.target_h3))).size;assert.equal(cells().length,expected);finite();
for(const metric of ['canopy_mean','terrain','distance','vegetation','combined','lower','upper'])for(const mode of ['known','coverage','resolved','lower','upper'])for(const stat of ['pooled','sum','mean','available']){
 els.metric.value=metric;els['view-mode'].value=mode;els.stat.value=stat;els.metric.listeners.change();finite();const c=cells()[0];c.listeners.pointermove({clientX:50,clientY:50});assert(!/NaN|undefined/.test(els.tip.textContent),els.tip.textContent);
}
let fabdem=0;
if(kind==='point')for(const source of Object.keys(D.viewpoints)){
 els.point.value=source;els.surface.value='retained';els.metric.value='canopy_mean';els.point.listeners.change();finite();assert.equal(cells().length,D.viewpoints[source].rows.length);
 if(D.composite_sources[source])assert.equal(els.scene.children.filter(c=>c.attrs.r===4).length,D.composite_sources[source].samples.length);
 else for(const surface of ['-1','0','1'])for(const metric of ['terrain','distance','vegetation','combined','lower','upper','canopy_mean']){els.surface.value=surface;els.metric.value=metric;els.surface.listeners.change();finite();if(metric==='terrain'){assert(els['coverage-headline'].textContent.includes('distance-integrated'));assert(els['coverage-detail'].textContent.includes('not an arithmetic'));}fabdem++;}
}
assert(!/fetch\(|XMLHttpRequest/.test(script));out.push({file:name,kind,cells:expected,interaction_selections:140,fabdem_selections:fabdem,status:'passed'});
}
console.log(JSON.stringify({status:'passed',method:'Node VM synthetic DOM; rendered visual QA unverified',results:out},null,2));
