/* Browse a bounded excerpt of precomputed regional results. */
(() => {
  "use strict";
  const svgNS = "http://www.w3.org/2000/svg";
  const colors = ["#dce4ea", "#bbdbee", "#72b3da", "#3c86bc", "#205794", "#102f5a"];

  function svgElement(name, attributes) {
    const node = document.createElementNS(svgNS, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  }

  function score(value) {
    if (value === null) return "Not applicable";
    if (value === 0) return "0";
    return value < 0.000001 ? value.toExponential(2) : value.toFixed(6);
  }

  function mount(root, data) {
    const map = root.querySelector("svg");
    const sources = root.querySelector("[data-sources]");
    const picker = root.querySelector("select");
    const summary = root.querySelector("[data-summary]");
    const rows = root.querySelector("tbody");
    const targetButtons = new Map();

    for (const [index, [cell, target]] of Object.entries(data.targets).entries()) {
      const option = document.createElement("option");
      const label = `Target ${index + 1} · ${cell}`;
      option.value = cell;
      option.textContent = label;
      picker.append(option);

      const button = svgElement("path", {
        d: target.outline,
        "aria-label": label,
        tabindex: "0",
        role: "button",
        class: "regional-target",
      });
      button.addEventListener("click", () => select(cell));
      button.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        select(cell);
      });
      map.append(button);
      targetButtons.set(cell, button);
    }

    function select(cell) {
      picker.value = cell;
      const role = root.querySelector("input:checked").value;
      const result = data.targets[cell].roles[role];
      sources.replaceChildren(...result.paths.map((path, index) =>
        svgElement("path", { d: path, fill: colors[index], stroke: "#fff", "stroke-width": ".7" })
      ));
      for (const [target, button] of targetButtons) {
        button.setAttribute("aria-pressed", String(cell === target));
      }
      const label = role === "land" ? "Land" : "Water";
      summary.textContent = `${label} observers · ${cell} · ${result.count} recorded pairs · ` +
        `${result.positive} positive · ${result.count - result.positive} zero · ` +
        `mean combined support ${score(result.mean)}`;

      rows.replaceChildren(...result.strongest.map(([source, ...values]) => {
        const row = document.createElement("tr");
        const heading = document.createElement("th");
        heading.scope = "row";
        heading.textContent = source;
        row.append(heading);
        for (const value of values) row.insertCell().textContent = score(value);
        return row;
      }));
    }

    picker.addEventListener("change", () => select(picker.value));
    for (const input of root.querySelectorAll("input")) {
      input.addEventListener("change", () => select(picker.value));
    }
    select(picker.value);
    root.hidden = false;
  }

  async function init() {
    const root = document.getElementById("regional-explorer");
    if (!root || root.dataset.initialized) return;
    root.dataset.initialized = "true";
    const link = document.getElementById("regional-example-data");
    try {
      const response = await fetch(link.href);
      if (!response.ok) throw new Error("Cannot load regional example");
      const data = await response.json();
      if (data.schema !== "regional_viewshed_example_v1") throw new Error("Unknown example data");
      if (root.isConnected) mount(root, data);
    } catch {
      if (root.isConnected) document.getElementById("regional-explorer-status").hidden = false;
    }
  }

  if (typeof document$ !== "undefined") document$.subscribe(init);
  else if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
