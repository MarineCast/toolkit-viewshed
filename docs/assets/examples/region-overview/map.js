/* Display the retained land-source maxima over one native H3 grid. */
(async () => {
  'use strict';
  const status = document.getElementById('status');
  const mapNode = document.getElementById('map');
  const place = document.getElementById('place');
  const reset = document.getElementById('reset');
  const layerOptions = document.getElementById('weight-options');
  const paletteOptions = document.getElementById('palette-options');
  const layerDescription = document.getElementById('layer-description');
  const ineligibleKey = document.getElementById('ineligible-key');
  const idle = '13,461 water cells · 26,094 land sources · select a cell for details.';
  const metrics = {
    combined: ['Combined', 'Highest combined viewing weight across land sources for each water cell.'],
    terrain: ['Terrain', 'Highest bare-earth viewing weight, with distance already included.'],
    vegetation: ['Vegetation', 'Highest fraction of terrain support retained after canopy, among sources with positive terrain support.'],
    distance: ['Distance', 'Highest cell-center distance weight. This diagnostic ignores obstructions.']
  };
  const palettes = {
    teal: ['#e0f4ed', '#08665b'],
    ocean: ['#d8ecfc', '#183a81'],
    amber: ['#fff2cb', '#8e3208'],
    plum: ['#efe5f9', '#612472']
  };

  const ramp = colors => `linear-gradient(90deg, ${colors.join(', ')})`;
  const rgb = color => color.slice(1).match(/../g).map(channel => parseInt(channel, 16));
  let metric = 'combined';
  let palette = 'teal';
  let ends = palettes[palette].map(rgb);

  try {
    if (typeof L === 'undefined') throw new Error('Leaflet unavailable');
    const map = L.map(mapNode, {
      preferCanvas: true, minZoom: 5, maxZoom: 16, zoomAnimation: false, fadeAnimation: false
    }).setView([48.8, -124.3], 6);
    const street = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>',
      keepBuffer: 1,
      updateWhenIdle: true
    }).addTo(map);
    street.on('tileerror', () => { document.getElementById('tile-status').hidden = false; });

    const [targets, coast, components] = await Promise.all(
      ['map-targets.geojson', 'map-coast.geojson', 'map-components.json'].map(async name => {
        const response = await fetch(name);
        if (!response.ok) throw new Error(`Map data unavailable: ${name}`);
        return response.json();
      })
    );
    if (targets.type !== 'FeatureCollection' || targets.features.length !== 13461 ||
        components.contract !== 'regional_land_component_maxima_v1' ||
        components.source_type !== 'land' ||
        Object.keys(components.rows).length !== targets.features.length ||
        new Set(targets.features.map(feature => feature.id)).size !== targets.features.length ||
        targets.features.some(feature => feature.properties.source_type !== 'land' ||
          !Object.hasOwn(components.rows, feature.id))) {
      throw new Error('Invalid target population');
    }

    map.createPane('coast');
    map.getPane('coast').style.zIndex = 150;
    L.geoJSON(coast, {
      pane: 'coast', interactive: false,
      style: { color: '#a5b1a9', weight: 0.6, fillColor: '#f6f4eb', fillOpacity: 1 },
      attribution: 'Coast: <a href="https://www.naturalearthdata.com/" target="_blank" rel="noopener">Natural Earth</a>'
    }).addTo(map);
    const offline = L.layerGroup();
    let selected = null;
    const popup = L.popup({
      maxWidth: 250, autoPanPaddingTopLeft: [44, 54], autoPanPaddingBottomRight: [12, 26]
    });

    function cellResult(feature) {
      const p = feature.properties;
      if (metric === 'combined') {
        return {
          maximum: p.maximum_valid_combined_weight, valid: p.valid_count,
          eligible: p.candidate_count, unknownTerrain: 0
        };
      }
      const record = components.rows[feature.id];
      const value = record[metric];
      return {
        maximum: value.maximum, valid: value.valid_count,
        eligible: metric === 'vegetation' ? value.eligible_count : p.candidate_count,
        unknownTerrain: metric === 'vegetation' ? p.candidate_count - record.terrain.valid_count : 0
      };
    }

    function style(feature) {
      const { maximum, eligible, unknownTerrain } = cellResult(feature);
      let fillColor;
      if (maximum === null) {
        fillColor = eligible === 0 && unknownTerrain === 0 ? '#f0e1c1' : '#f4d7e6';
      } else if (maximum === 0) {
        fillColor = '#dce0df';
      } else {
        const channels = ends[0].map((low, i) => Math.round(low + (ends[1][i] - low) * maximum));
        fillColor = `rgb(${channels.join(', ')})`;
      }
      return { fillColor, fillOpacity: 0.85, color: '#526d65', weight: 0.25, opacity: 0.35 };
    }

    function showCell(feature) {
      const { maximum, valid, eligible, unknownTerrain } = cellResult(feature);
      const display = maximum === null
        ? (eligible === 0 && unknownTerrain === 0 ? 'No positive terrain' : 'Unavailable')
        : (maximum === 0 ? '0 (computed zero)' : maximum.toPrecision(6));
      const box = document.createElement('section');
      const heading = document.createElement('h2');
      heading.textContent = `${metrics[metric][0]} weight`;
      const id = document.createElement('code');
      id.textContent = feature.id;
      const list = document.createElement('dl');
      const rows = [
        ['Highest weight', display],
        [metric === 'vegetation' ? 'Valid / eligible sources' : 'Valid / candidate sources', `${valid} / ${eligible}`],
        ['Missing eligible values', eligible - valid]
      ];
      if (metric === 'vegetation') {
        rows.push(['All candidate sources', feature.properties.candidate_count]);
        if (unknownTerrain) rows.push(['Unknown terrain eligibility', unknownTerrain]);
      }
      for (const [label, value] of rows) {
        const term = document.createElement('dt');
        term.textContent = label;
        const definition = document.createElement('dd');
        definition.textContent = String(value);
        list.append(term, definition);
      }
      box.append(heading, id, list);
      popup.setContent(box);
      status.textContent = `${feature.id} · ${metrics[metric][0]} ${display} · ${valid}/${eligible} valid sources`;
    }

    const weights = L.geoJSON(targets, { style }).addTo(map);
    const selectionStyle = { color: '#b64e15', weight: 2, opacity: 1 };
    weights.on('click', event => {
      if (selected) weights.resetStyle(selected);
      selected = event.layer;
      selected.setStyle(selectionStyle).bringToFront();
      showCell(selected.feature);
      popup.setLatLng(event.latlng).openOn(map);
    });
    L.control.layers(
      { 'Street map': street, 'Coastline (offline)': offline },
      { 'Viewing weights': weights }, { collapsed: true }
    ).addTo(map);
    L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(map);

    for (const input of paletteOptions.querySelectorAll('input')) {
      input.parentElement.style.setProperty('--swatch', ramp(palettes[input.value]));
    }
    function updateLegend() {
      layerDescription.textContent = metrics[metric][1];
      ineligibleKey.hidden = metric !== 'vegetation';
      document.documentElement.style.setProperty('--ramp', ramp(palettes[palette]));
      mapNode.dataset.weight = metric;
      mapNode.dataset.palette = palette;
      weights.setStyle(style);
      if (selected) {
        selected.setStyle(selectionStyle);
        showCell(selected.feature);
      }
    }
    layerOptions.addEventListener('change', event => {
      metric = event.target.value;
      updateLegend();
    });
    paletteOptions.addEventListener('change', event => {
      palette = event.target.value;
      ends = palettes[palette].map(rgb);
      updateLegend();
    });

    function clearSelection() {
      map.closePopup();
      if (selected) weights.resetStyle(selected);
      selected = null;
      status.textContent = idle;
    }
    function fullRegion() {
      clearSelection();
      map.fitBounds(weights.getBounds(), { padding: [18, 18] });
      place.value = 'region';
    }
    const places = { lime: [48.5158, -123.1525], everett: [48.0151, -122.2236], larrabee: [48.6601, -122.4645] };
    reset.addEventListener('click', fullRegion);
    place.addEventListener('change', () => {
      if (place.value === 'region') fullRegion();
      else {
        clearSelection();
        map.setView(places[place.value], 11);
      }
    });
    mapNode.addEventListener('keydown', event => {
      if (event.key === 'Home') {
        event.preventDefault();
        fullRegion();
      }
    });
    new ResizeObserver(() => {
      map.invalidateSize({ pan: false });
      if (map.hasLayer(popup)) popup.update();
    }).observe(mapNode);
    map.on('moveend zoomend', () => {
      mapNode.dataset.zoom = String(map.getZoom());
      mapNode.dataset.center = `${map.getCenter().lat.toFixed(6)},${map.getCenter().lng.toFixed(6)}`;
    });
    fullRegion();
    updateLegend();
    map.fire('moveend');
    mapNode.dataset.targetCount = String(targets.features.length);
    for (const control of [reset, place, layerOptions, paletteOptions]) control.disabled = false;
    document.body.dataset.ready = 'true';
  } catch (error) {
    status.textContent = 'The interactive map could not be loaded.';
    document.getElementById('error').hidden = false;
    console.error('Regional map:', error.message);
  }
})();
