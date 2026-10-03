/* Presentation of validated, precomputed records only. No LOS engine runs here. */
(() => {
  "use strict";
  const cache = new Map();
  let active = null;
  const factors = {
    bare: [
      "Ground only: unweighted LOS",
      "line_of_sight_support",
      "line_of_sight_state",
    ],
    canopy: [
      "Ground + trees: unweighted LOS",
      "physical_viewability",
      "physical_viewability_state",
    ],
    distance: [
      "Distance diagnostic",
      "distance_detection_weight",
      "distance_detection_state",
    ],
    integrated: [
      "Ground + distance support",
      "distance_weighted_los_support",
      "distance_weighted_los_state",
    ],
    retention: [
      "Retained after vegetation",
      "vegetation_attenuation",
      "vegetation_state",
    ],
    combined: [
      "Combined modeled support",
      "distance_adjusted_viewability",
      "distance_adjusted_viewability_state",
    ],
  };
  const lessonFactor = {
    inputs: "combined",
    samples: "bare",
    distance: "distance",
    terrain: "bare",
    canopy: "canopy",
    combined: "combined",
    inverse: "combined",
  };
  const svgNS = "http://www.w3.org/2000/svg";
  function node(tag, attributes = {}, content = null, svg = false) {
    const element = svg
      ? document.createElementNS(svgNS, tag)
      : document.createElement(tag);
    for (const [key, value] of Object.entries(attributes))
      element.setAttribute(key, value);
    if (content !== null) element.textContent = content;
    return element;
  }
  function format(value) {
    if (value === 0) return "0 (modeled zero)";
    if (value === null || value === undefined) return "Unavailable";
    return value < 0.001 ? value.toExponential(2) : value.toFixed(3);
  }
  function label(pair, factor) {
    if (!pair) return "No candidate result";
    const [, field, stateField] = factors[factor];
    const state = pair[stateField];
    if (state === "not_applicable") return "Not applicable";
    if (state === "no_baseline_support_neutral")
      return "Neutral: no baseline support";
    if (state === "source_unavailable") return "Unavailable";
    return format(pair[field]);
  }
  async function sha(bytes) {
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(hash), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
  }
  async function load(url) {
    if (!cache.has(url))
      cache.set(
        url,
        (async () => {
          const response = await fetch(new URL("manifest.json", url));
          if (!response.ok) throw new Error("Bundle manifest is unavailable");
          const manifest = await response.json();
          if (manifest.export_contract !== "san_juan_lessons_v1")
            throw new Error("Unsupported bundle contract");
          const names = [
            "pairs.json",
            "cells.geojson",
            "observer-samples.geojson",
            "inputs/coast.geojson",
            "profiles.json",
            "lessons.json",
            "indexes.json",
            "coverage.json",
            "distance-curve.json",
          ];
          const items = await Promise.all(
            names.map(async (name) => {
              const result = await fetch(new URL(name, url));
              if (!result.ok)
                throw new Error("Bundle asset is unavailable: " + name);
              const bytes = await result.arrayBuffer();
              if ((await sha(bytes)) !== manifest.files[name])
                throw new Error("Bundle integrity check failed: " + name);
              return [name, JSON.parse(new TextDecoder().decode(bytes))];
            }),
          );
          return { manifest, ...Object.fromEntries(items) };
        })().catch((error) => {
          cache.delete(url);
          throw error;
        }),
      );
    return cache.get(url);
  }
  function controller(root, owner, data, url) {
    const abort = new AbortController();
    const options = { signal: abort.signal };
    const columnar = data["pairs.json"];
    const pairs = columnar.rows.map((row) =>
      Object.fromEntries(
        columnar.columns.map((key, index) => [key, row[index]]),
      ),
    );
    const byId = new Map(pairs.map((pair) => [pair.id, pair]));
    const cells = new Map(
      data["cells.geojson"].features.map((feature) => [feature.id, feature]),
    );
    const cellIds = [...cells.keys()].sort();
    const targetIds = cellIds.filter(
      (cell) =>
        data["cells.geojson"].features.find((f) => f.id === cell).properties
          .target,
    );
    const sourceIds = (role) =>
      cellIds.filter((cell) => cells.get(cell).properties.roles.includes(role));
    const defaults = byId.get(data.manifest.defaults.pair_id);
    const lessons = data["lessons.json"];
    const initial = {
      generation: data.manifest.generation_id,
      scenario: "baseline",
      lesson: "inputs",
      source_type: defaults.source_type,
      source_h3: defaults.source_h3,
      target_h3: defaults.target_h3,
      factor: "combined",
      direction: "forward",
      layer: "boundaries",
    };
    let state = { ...initial };
    let zoom = 1;
    const profileByPair = new Map(
      data["profiles.json"].map((profile) => [profile.pair_id, profile]),
    );
    const area = (cell, source = false) =>
      cell === (source ? defaults.source_h3 : defaults.target_h3)
        ? source
          ? "Observer area A"
          : "Water area B"
        : (source ? "Observer area " : "Water area ") +
          (cellIds.indexOf(cell) + 1);
    const card = node("div", { class: "vs-explorer-card", tabindex: "-1" });
    const heading = node("h3", {}, "Explore the same modeled results");
    const badge = node(
      "p",
      { class: "coverage-badge" },
      `Real baseline generation · ${(100 * data["coverage.json"].rasters.chm.missing_land_fraction).toFixed(2)}% missing canopy across mapped land inputs · zero-height fallback. Static support, not detection probability.`,
    );
    const controls = node("div", { class: "vs-controls" });
    const select = (name, text, values) => {
      const wrapper = node("label", {}, text + " ");
      const input = node("select", {
        "aria-label": text,
        "data-control": name,
      });
      for (const [value, title] of values)
        input.append(node("option", { value }, title));
      input.addEventListener(
        "change",
        () => update({ [name]: input.value }),
        options,
      );
      wrapper.append(input);
      controls.append(wrapper);
      return input;
    };
    const direction = select("direction", "Question", [
      ["forward", "What water could this observer area see?"],
      ["inverse", "Which observer areas could see this water?"],
    ]);
    const role = select("source_type", "Observer role", [
      ["land", "Land observer areas"],
      ["water", "Water observer areas"],
    ]);
    const source = select("source_h3", "Observer area", []);
    const target = select(
      "target_h3",
      "Water area",
      targetIds.map((cell) => [cell, area(cell)]),
    );
    const factor = select(
      "factor",
      "Show",
      Object.entries(factors).map(([key, value]) => [key, value[0]]),
    );
    const actions = node("div", { class: "vs-actions" });
    const button = (text, callback, key = null) => {
      const result = node(
        "button",
        { type: "button", ...(key ? { "data-action": key } : {}) },
        text,
      );
      result.addEventListener("click", callback, options);
      actions.append(result);
      return result;
    };
    button("Ground only", () => update({ factor: "bare" }), "ground-only");
    button(
      "Ground + tree heights",
      () => update({ factor: "canopy" }),
      "ground-trees",
    );
    button(
      "Land/water boundaries",
      () => update({ layer: "boundaries" }),
      "boundaries",
    );
    button(
      "Ground elevation",
      () => update({ layer: "ground" }),
      "ground-layer",
    );
    button(
      "Tree heights + missing data",
      () => update({ layer: "canopy_height" }),
      "canopy-layer",
    );
    button(
      "Zoom in",
      () => {
        zoom = Math.min(4, zoom * 1.4);
        render();
      },
      "zoom-in",
    );
    button(
      "Zoom out",
      () => {
        zoom = Math.max(1, zoom / 1.4);
        render();
      },
      "zoom-out",
    );
    button(
      "Reset example",
      () => {
        state = { ...initial };
        zoom = 1;
        advanced.open = false;
        accessiblePairs.open = false;
        render();
      },
      "reset",
    );
    const back = button(
      "Previous lesson",
      () =>
        chooseLesson(
          Math.max(0, lessons.findIndex((l) => l.id === state.lesson) - 1),
        ),
      "previous",
    );
    const next = button(
      "Next lesson",
      () =>
        chooseLesson(
          Math.min(
            lessons.length - 1,
            lessons.findIndex((l) => l.id === state.lesson) + 1,
          ),
        ),
      "next",
    );
    const visual = node("div", { class: "vs-visual" });
    const status = node("output", {
      class: "vs-status",
      "aria-live": "polite",
      "aria-atomic": "true",
    });
    const summary = node("div", { class: "vs-summary" });
    const legend = node(
      "p",
      { class: "vs-legend" },
      "Fixed scale: 0–1. Blue: positive; pale gray: modeled zero; outline: no candidate; hatch: unavailable, not applicable, or neutral canopy. No smoothing. Samples are modeled positions, not actual people.",
    );
    const technical = node("details");
    technical.append(
      node("summary", {}, "Technical pair identity and recorded assumptions"),
    );
    const technicalText = node("pre");
    technical.append(technicalText);
    const tableWrap = node("div", { class: "example-table" });
    const table = node("table");
    table.append(
      node("caption", {}, "Canonical pair records for this question"),
    );
    const head = node("thead");
    const headRow = node("tr");
    for (const title of [
      "Choose pair",
      "Ground LOS",
      "Ground + trees LOS",
      "Combined support",
    ])
      headRow.append(node("th", { scope: "col" }, title));
    head.append(headRow);
    table.append(head);
    const tbody = node("tbody");
    table.append(tbody);
    tableWrap.append(table);
    const advanced = node("details", { class: "vs-advanced" });
    advanced.append(
      node("summary", {}, "Change areas, role, factor or query direction"),
      controls,
    );
    const accessiblePairs = node("details");
    accessiblePairs.append(
      node("summary", {}, "Choose another pair with the accessible table"),
      tableWrap,
    );
    card.append(
      heading,
      badge,
      status,
      visual,
      actions,
      legend,
      summary,
      advanced,
      technical,
      accessiblePairs,
    );
    const selectedPair = () =>
      byId.get(`${state.source_type}:${state.source_h3}:${state.target_h3}`);
    function chooseLesson(index) {
      const lesson = lessons[index];
      const pair = byId.get(lesson.pair_ids[0]);
      state = {
        ...state,
        lesson: lesson.id,
        source_type: pair.source_type,
        source_h3: pair.source_h3,
        target_h3: pair.target_h3,
        factor: lessonFactor[lesson.id],
        direction: lesson.id === "inverse" ? "inverse" : "forward",
        layer: "boundaries",
      };
      zoom = 1;
      render();
      card.focus({ preventScroll: true });
      card.scrollIntoView({ block: "start", behavior: "auto" });
    }
    function update(change) {
      state = { ...state, ...change };
      if (!sourceIds(state.source_type).includes(state.source_h3))
        state.source_h3 = sourceIds(state.source_type)[0];
      render();
    }
    function geometryPath(geometry, project) {
      const polygons =
        geometry.type === "Polygon"
          ? [geometry.coordinates]
          : geometry.coordinates;
      return polygons
        .map((polygon) =>
          polygon
            .map(
              (ring) =>
                "M" +
                ring.map((point) => project(point).join(",")).join(" L") +
                " Z",
            )
            .join(" "),
        )
        .join(" ");
    }
    function map(rows) {
      const scene = node(
        "svg",
        {
          viewBox: "0 0 720 420",
          class: "vs-map",
          role: "group",
          "aria-label": direction.options[direction.selectedIndex].text,
        },
        null,
        true,
      );
      const defs = node("defs", {}, null, true);
      const pattern = node(
        "pattern",
        {
          id: "vs-state-hatch",
          width: "8",
          height: "8",
          patternUnits: "userSpaceOnUse",
        },
        null,
        true,
      );
      pattern.append(
        node("rect", { width: "8", height: "8", fill: "#e4e9ed" }, null, true),
        node(
          "path",
          { d: "M0,8 L8,0", stroke: "#647a84", "stroke-width": "1.5" },
          null,
          true,
        ),
      );
      defs.append(pattern);
      scene.append(defs);
      const fixed =
        state.direction === "forward" ? state.source_h3 : state.target_h3;
      const ring = cells.get(fixed).geometry.coordinates[0];
      const center = [
        ring.slice(0, -1).reduce((sum, p) => sum + p[0], 0) / (ring.length - 1),
        ring.slice(0, -1).reduce((sum, p) => sum + p[1], 0) / (ring.length - 1),
      ];
      const project = (point) => [
        360 + (point[0] - center[0]) * 3500 * zoom,
        210 - (point[1] - center[1]) * 5200 * zoom,
      ];
      scene.append(
        node(
          "rect",
          { width: "720", height: "420", class: "vs-sea" },
          null,
          true,
        ),
      );
      for (const feature of data["inputs/coast.geojson"].features)
        scene.append(
          node(
            "path",
            {
              d: geometryPath(feature.geometry, project),
              class: "vs-land",
              "fill-rule": "evenodd",
            },
            null,
            true,
          ),
        );
      const rowByCell = new Map(
        rows.map((pair) => [
          pair[state.direction === "forward" ? "target_h3" : "source_h3"],
          pair,
        ]),
      );
      const mapCells =
        state.direction === "forward"
          ? targetIds
          : sourceIds(state.source_type);
      for (const cell of mapCells) {
        const pair = rowByCell.get(cell);
        let fill = "none";
        if (pair && state.lesson !== "inputs") {
          const [, field, stateField] = factors[state.factor];
          const value = pair[field];
          const status = pair[stateField];
          fill = [
            "not_applicable",
            "source_unavailable",
            "no_baseline_support_neutral",
          ].includes(status)
            ? "url(#vs-state-hatch)"
            : value === 0
              ? "#e4e9ed"
              : value === null
                ? "url(#vs-state-hatch)"
                : `rgb(${Math.round(213 - 185 * value)},${Math.round(237 - 123 * value)},${Math.round(244 - 100 * value)})`;
        }
        const path = node(
          "path",
          {
            d: geometryPath(cells.get(cell).geometry, project),
            fill,
            class: "vs-cell",
            tabindex:
              cell ===
              (state.direction === "forward"
                ? state.target_h3
                : state.source_h3)
                ? "0"
                : "-1",
            role: "button",
            "data-cell": cell,
            "aria-label": `${area(cell, state.direction === "inverse")}: ${label(pair, state.factor)}`,
            "aria-pressed": String(
              cell ===
                (state.direction === "forward"
                  ? state.target_h3
                  : state.source_h3),
            ),
          },
          null,
          true,
        );
        const choose = () =>
          update({
            [state.direction === "forward" ? "target_h3" : "source_h3"]: cell,
          });
        path.addEventListener("click", choose, options);
        path.addEventListener(
          "keydown",
          (event) => {
            if (["Enter", " "].includes(event.key)) {
              event.preventDefault();
              choose();
            } else if (
              ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(
                event.key,
              )
            ) {
              event.preventDefault();
              const keyboardCells = mapCells.filter((c) => rowByCell.has(c));
              const delta = ["ArrowLeft", "ArrowUp"].includes(event.key)
                ? -1
                : 1;
              const nextCell =
                keyboardCells[
                  (keyboardCells.indexOf(cell) + delta + keyboardCells.length) %
                    keyboardCells.length
                ];
              update({
                [state.direction === "forward" ? "target_h3" : "source_h3"]:
                  nextCell,
              });
              visual
                .querySelector(`[data-cell="${nextCell}"]`)
                ?.focus({ preventScroll: true });
            }
          },
          options,
        );
        scene.append(path);
      }
      for (const [cell, color] of [
        [state.source_h3, "#172d37"],
        [state.target_h3, "#126e88"],
      ])
        scene.append(
          node(
            "path",
            {
              d: geometryPath(cells.get(cell).geometry, project),
              fill: "none",
              class: cell === state.source_h3 ? "vs-source-outline" : "vs-target-outline",
              stroke: color,
              "stroke-width": "3",
              "pointer-events": "none",
            },
            null,
            true,
          ),
        );
      for (const observer of data["observer-samples.geojson"].features)
        if (
          observer.properties.source_h3 === state.source_h3 &&
          observer.properties.source_type === state.source_type
        ) {
          const [x, y] = project(observer.geometry.coordinates);
          scene.append(
            node(
              "circle",
              {
                cx: x,
                cy: y,
                r: "4",
                class: "vs-observer",
                "pointer-events": "none",
              },
              null,
              true,
            ),
          );
        }
      return scene;
    }
    function profileView(profile) {
      const wrap = node("figure", { class: "vs-profile" });
      const svg = node(
        "svg",
        {
          viewBox: "0 0 720 250",
          role: "img",
          "aria-label":
            "One explanatory real-surface profile, not a cell aggregate or engine diagnostic",
        },
        null,
        true,
      );
      const max =
        Math.max(
          ...profile.samples.map((p) =>
            Math.max(p.ground_m, p.canopy_surface_m, p.ray_m),
          ),
        ) *
          1.1 +
        2;
      const length = profile.samples.at(-1).distance_m;
      for (const [field, color] of [
        ["ground_m", "#797c6d"],
        ["canopy_surface_m", "#12827f"],
        ["ray_m", "#3b5662"],
      ])
        svg.append(
          node(
            "polyline",
            {
              points: profile.samples
                .map(
                  (p) =>
                    `${40 + (p.distance_m / length) * 650},${205 - (p[field] / max) * 175}`,
                )
                .join(" "),
              fill: "none",
              stroke: color,
              "stroke-width": "2",
            },
            null,
            true,
          ),
        );
      svg.append(
        node(
          "text",
          { x: "40", y: "20", class: "vs-plot-text" },
          `Height, metres (0–${max.toFixed(0)})`,
          true,
        ),
        node(
          "text",
          { x: "40", y: "238", class: "vs-plot-text" },
          `Observer to actual water endpoint: ${(length / 1000).toFixed(2)} km`,
          true,
        ),
      );
      wrap.append(
        svg,
        node(
          "figcaption",
          {},
          "Gray: ground; teal: ground + trees; dark: explanatory ray. Real 100 m surfaces sampled for display at 50 m. This path does not establish the cell aggregate.",
        ),
      );
      return wrap;
    }
    function distanceView(pair) {
      const figure = node("figure", { class: "vs-profile" });
      const svg = node(
        "svg",
        {
          viewBox: "0 0 720 290",
          role: "img",
          "aria-label":
            "Configured distance attenuation curve with the selected real pair's centroid diagnostic",
        },
        null,
        true,
      );
      svg.append(
        node(
          "polyline",
          {
            points: data["distance-curve.json"]
              .map(
                (p) =>
                  `${40 + (p.distance_km / 5) * 650},${235 - p.weight * 190}`,
              )
              .join(" "),
            fill: "none",
            stroke: "#198296",
            "stroke-width": "3",
          },
          null,
          true,
        ),
      );
      if (pair)
        svg.append(
          node(
            "circle",
            {
              cx: 40 + (pair.distance_km / 5) * 650,
              cy: 235 - pair.distance_detection_weight * 190,
              r: "6",
              fill: "#16889e",
            },
            null,
            true,
          ),
        );
      svg.append(
        node(
          "text",
          { x: "40", y: "25", class: "vs-plot-text" },
          "Diagnostic support 0–1; assumed curve, not sighting probability",
          true,
        ),
        node(
          "text",
          { x: "40", y: "275", class: "vs-plot-text" },
          "Centroid distance 0–5 km",
          true,
        ),
      );
      figure.append(
        svg,
        node(
          "figcaption",
          {},
          "Precomputed production curve. The final kernel applies it at observer-to-water distances; the centroid diagnostic is not multiplied twice.",
        ),
      );
      return figure;
    }
    function render() {
      const focus = document.activeElement?.dataset?.cell;
      const pairFocus = document.activeElement?.dataset?.pair;
      root.dataset.state = JSON.stringify(state);
      const lesson = owner.querySelector(`#lesson-${state.lesson}`);
      owner.querySelectorAll(".lesson").forEach((item) => {
        item.classList.toggle("vs-active-lesson", item === lesson);
        item
          .querySelector("figure")
          ?.classList.toggle("vs-static-fallback", item === lesson);
      });
      (lesson || root).append(card);
      heading.textContent = `${state.lesson === "explore" ? "Explore other areas" : "Lesson " + (lessons.findIndex((l) => l.id === state.lesson) + 1)} · ${state.direction === "forward" ? "What water could this observer area see?" : "Which observer areas could see this water area?"}`;
      direction.value = state.direction;
      role.value = state.source_type;
      target.value = state.target_h3;
      factor.value = state.factor;
      const available = sourceIds(state.source_type);
      if (source.dataset.role !== state.source_type) {
        source.replaceChildren(
          ...available.map((cell) =>
            node("option", { value: cell }, area(cell, true)),
          ),
        );
        source.dataset.role = state.source_type;
      }
      source.value = state.source_h3;
      const pair = selectedPair();
      const key =
        state.source_type +
        ":" +
        (state.direction === "forward" ? state.source_h3 : state.target_h3);
      const rows = (data["indexes.json"][state.direction][key] || []).map(
        (id) => byId.get(id),
      );
      const metricTitle =
        state.source_type === "water" && state.factor === "canopy"
          ? "Unweighted water LOS; canopy not applicable"
          : factors[state.factor][0];
      factor.querySelector('option[value="canopy"]').textContent =
        state.source_type === "water"
          ? "Unweighted water LOS (canopy not applicable)"
          : factors.canopy[0];
      status.textContent = `${area(state.source_h3, true)} (${state.source_type}) → ${area(state.target_h3)} · ${metricTitle}: ${label(pair, state.factor)}`;
      status.dataset.pairId = pair?.id || "noncandidate";
      status.dataset.pairValue = pair
        ? String(pair.distance_adjusted_viewability)
        : "unavailable";
      visual.replaceChildren();
      if (state.layer === "boundaries") visual.append(map(rows));
      else {
        const img = node("img", {
          src: new URL(`previews/${state.layer}.svg`, url),
          alt: "Real prepared height grid; pink marks missing inputs; 400 m display sampling",
          loading: "lazy",
        });
        visual.append(img);
      }
      if (["samples", "terrain", "canopy"].includes(state.lesson)) {
        const profile = pair && profileByPair.get(pair.id);
        visual.append(
          profile
            ? profileView(profile)
            : node(
                "p",
                {},
                "No prepared explanatory teaching profile is available for this selected pair. The stored cell result remains inspectable.",
              ),
        );
      }
      if (state.lesson === "distance") visual.append(distanceView(pair));
      const visibleActions =
        state.lesson === "inputs"
          ? ["boundaries", "ground-layer", "canopy-layer", "next", "reset"]
          : state.lesson === "canopy"
            ? ["ground-only", "ground-trees", "previous", "next", "reset"]
            : state.lesson === "explore"
              ? null
              : ["zoom-in", "zoom-out", "previous", "next", "reset"];
      actions.querySelectorAll("button").forEach((btn) => {
        btn.hidden =
          visibleActions && !visibleActions.includes(btn.dataset.action);
      });
      summary.hidden = !["combined", "inverse", "explore"].includes(
        state.lesson,
      );
      legend.textContent =
        state.lesson === "inputs"
          ? "Actual mapped areas and modeled sample dots. Change the input layer below the map. The accessible table lists stored pair values."
          : "Fixed scale: 0–1. Blue: positive; pale gray: modeled zero; outline: no candidate; hatch: unavailable, not applicable, or neutral canopy. No smoothing. Samples are modeled positions, not actual people.";
      summary.replaceChildren();
      for (const [title, f] of [
        ["Ground + distance support", "integrated"],
        ["Retained after vegetation", "retention"],
        ["Combined modeled support", "combined"],
      ])
        summary.append(node("p", {}, `${title}: ${label(pair, f)}`));
      if (state.source_type === "water")
        summary.append(
          node(
            "p",
            {},
            "Water role: opaque land, separate water samples; canopy is not applicable.",
          ),
        );
      const positive = rows.filter(
        (p) => p.distance_adjusted_viewability > 0,
      ).length;
      summary.append(
        node(
          "p",
          {},
          `${rows.length} candidate pairs, ${positive} positive. Sources included in this example; counts describe modeled areas, not actual observers.`,
        ),
      );
      technicalText.textContent = JSON.stringify(
        {
          generation: state.generation,
          scenario: state.scenario,
          role: state.source_type,
          source_h3: state.source_h3,
          target_h3: state.target_h3,
          pair: pair || "No candidate record",
          source_coverage: data["coverage.json"],
          assumptions: data.manifest.assumptions,
        },
        null,
        2,
      );
      tbody.replaceChildren(
        ...rows.map((p) => {
          const tr = node("tr");
          const th = node("th", { scope: "row" });
          const choose = node(
            "button",
            { type: "button", "data-pair": p.id },
            area(
              p[state.direction === "forward" ? "target_h3" : "source_h3"],
              state.direction === "inverse",
            ),
          );
          choose.addEventListener(
            "click",
            () => update({ source_h3: p.source_h3, target_h3: p.target_h3 }),
            options,
          );
          th.append(choose);
          tr.append(th);
          for (const f of ["bare", "canopy", "combined"])
            tr.append(node("td", {}, label(p, f)));
          return tr;
        }),
      );
      back.disabled = state.lesson === lessons[0].id;
      next.disabled = state.lesson === lessons.at(-1).id;
      if (pairFocus)
        tbody
          .querySelector(`[data-pair="${pairFocus}"]`)
          ?.focus({ preventScroll: true });
      if (focus)
        visual
          .querySelector(`[data-cell="${focus}"]`)
          ?.focus({ preventScroll: true });
    }
    for (const [index, lesson] of lessons.entries()) {
      const section = owner.querySelector(`#lesson-${lesson.id}`);
      const nav = node("div", { class: "vs-lesson-actions" });
      const choose = node(
        "button",
        { type: "button", "data-lesson": lesson.id },
        "Explore this lesson",
      );
      choose.addEventListener("click", () => chooseLesson(index), options);
      nav.append(choose);
      for (const [i, id] of lesson.pair_ids.entries()) {
        const btn = node(
          "button",
          { type: "button", "data-case": id },
          `Try real example ${i + 1}`,
        );
        btn.addEventListener(
          "click",
          () => {
            chooseLesson(index);
            const pair = byId.get(id);
            update({ source_h3: pair.source_h3, target_h3: pair.target_h3 });
          },
          options,
        );
        nav.append(btn);
      }
      section.append(nav);
    }
    const explore = node(
      "button",
      { type: "button", "data-action": "explore" },
      "Explore other areas",
    );
    explore.addEventListener(
      "click",
      () => {
        update({ lesson: "explore", layer: "boundaries" });
        card.focus();
        card.scrollIntoView({ block: "start" });
      },
      options,
    );
    root.append(explore);
    render();
    root.dataset.ready = "true";
    return { abort: () => abort.abort(), root };
  }
  async function init() {
    const owner = document.querySelector(".viewshed-examples");
    const root = owner?.querySelector("#viewshed-explorer");
    if (!root) {
      active?.abort();
      active = null;
      return;
    }
    if (root.dataset.loading || (root.dataset.ready && active?.root === root))
      return;
    if (root.dataset.ready) {
      owner
        .querySelectorAll(".vs-explorer-card,.vs-lesson-actions")
        .forEach((item) => item.remove());
      owner
        .querySelectorAll(".vs-static-fallback")
        .forEach((item) => item.classList.remove("vs-static-fallback"));
      root.replaceChildren();
      delete root.dataset.ready;
    }
    active?.abort();
    root.dataset.loading = "true";
    try {
      const url = new URL(owner.dataset.bundle, document.baseURI).href;
      const data = await load(url);
      if (root.isConnected) active = controller(root, owner, data, url);
    } catch (error) {
      root.append(
        node(
          "p",
          { role: "status" },
          "The interactive bundle could not be loaded. All seven static lessons and reveal tables remain available. " +
            error.message,
        ),
      );
    } finally {
      delete root.dataset.loading;
    }
  }
  if (typeof document$ !== "undefined") document$.subscribe(init);
  else if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
