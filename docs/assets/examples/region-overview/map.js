/* Leaflet displays retained native H3 cells; no weights are calculated in the browser. */
(async () => {
  'use strict';
  const status = document.getElementById('status');
  const error = document.getElementById('error');
  try {
    if (typeof L === 'undefined') throw new Error('Leaflet unavailable');
    const map = L.map('map', { preferCanvas: true, minZoom: 5, maxZoom: 16, zoomSnap: 1,
      zoomDelta: 1, attributionControl: true, zoomAnimation: false, fadeAnimation: false });
    map.setView([48.8, -124.3], 6);
    const street = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>',
      keepBuffer: 1, updateWhenIdle: true
    }).addTo(map);
    street.on('tileerror', () => { document.getElementById('tile-status').hidden = false; });
    map.createPane('coast'); map.getPane('coast').style.zIndex = 150;
    const [targets, coast] = await Promise.all(['map-targets.geojson','map-coast.geojson'].map(async name => {
      const response = await fetch(name);
      if (!response.ok) throw new Error('Map data unavailable');
      return response.json();
    }));
    if (targets.type !== 'FeatureCollection' || targets.features.length !== 13461 ||
        new Set(targets.features.map(f => f.id)).size !== targets.features.length ||
        targets.features.some(f => f.properties.source_type !== 'land')) throw new Error('Invalid target population');
    L.geoJSON(coast, { pane: 'coast', interactive: false,
      style: { color:'#a5b1a9', weight:0.6, fillColor:'#f6f4eb', fillOpacity:1 },
      attribution: 'Coast: <a href="https://www.naturalearthdata.com/" target="_blank" rel="noopener">Natural Earth</a>'
    }).addTo(map);
    // Empty base layer leaves the locally bundled coast visible underneath.
    const offline = L.layerGroup();
    let selected = null;
    const weights = L.geoJSON(targets, {
      style: feature => ({fillColor:feature.properties.fill, fillOpacity:0.85, color:'#526d65', weight:0.25, opacity:0.35}),
      onEachFeature: (feature, layer) => {
        const p = feature.properties;
        const value = p.maximum_valid_combined_weight;
        const displayed = value === null ? 'Unavailable' : value === 0 ? '0 (computed zero)' : Number(value).toPrecision(6);
        const box = document.createElement('section');
        const heading = document.createElement('h2'); heading.textContent = 'Water target'; box.append(heading);
        const id = document.createElement('code'); id.textContent = p.target_h3; box.append(id);
        const list = document.createElement('dl');
        for (const [label, text] of [['Maximum combined weight', displayed],['Valid / candidate sources',`${p.valid_count} / ${p.candidate_count}`],['Missing candidates',p.missing_count],['Coverage',p.state]]) {
          const dt = document.createElement('dt'); dt.textContent=label;
          const dd = document.createElement('dd'); dd.textContent=String(text); list.append(dt,dd);
        }
        box.append(list);
        const note=document.createElement('p'); note.textContent='Strongest land-source support; not a detection probability.'; box.append(note);
        layer.bindPopup(box, {maxWidth:320});
        layer.on('click', () => {
          if (selected) weights.resetStyle(selected);
          selected=layer; layer.setStyle({color:'#d76d20',weight:2,opacity:1}); layer.bringToFront();
          status.textContent=`${p.target_h3} · combined ${displayed} · ${p.valid_count}/${p.candidate_count} valid land sources`;
        });
      }
    }).addTo(map);
    L.control.layers({'Street map':street,'Coastline (offline)':offline},{'Combined weights':weights},{collapsed:true}).addTo(map);
    L.control.scale({imperial:false,position:'bottomleft'}).addTo(map);
    const bounds=weights.getBounds();
    const place=document.getElementById('place');
    const reset=document.getElementById('reset');
    function fullRegion() {
      map.closePopup(); if(selected) weights.resetStyle(selected); selected=null;
      map.fitBounds(bounds,{padding:[18,18]}); place.value='region';
      status.textContent='13,461 water cells · 26,094 land sources · select a cell to inspect its weight.';
    }
    reset.addEventListener('click',fullRegion);
    const places={lime:[48.5158,-123.1525],everett:[48.0151,-122.2236],larrabee:[48.6601,-122.4645]};
    place.addEventListener('change',()=>{if(place.value==='region') fullRegion(); else map.setView(places[place.value],11);});
    document.getElementById('map').addEventListener('keydown',event=>{if(event.key==='Home'){event.preventDefault();fullRegion();}});
    new ResizeObserver(()=>map.invalidateSize({pan:false})).observe(document.getElementById('map'));
    reset.disabled=false;place.disabled=false;fullRegion();
    document.body.dataset.ready='true';
    document.getElementById('map').dataset.targetCount=String(targets.features.length);
    map.on('moveend zoomend',()=>{
      const node=document.getElementById('map'); node.dataset.zoom=String(map.getZoom());
      node.dataset.center=`${map.getCenter().lat.toFixed(6)},${map.getCenter().lng.toFixed(6)}`;
    });
    map.fire('moveend');
  } catch (e) {
    status.textContent='The interactive map could not be loaded.'; error.hidden=false;
    console.error('Regional map:', e.message);
  }
})();
