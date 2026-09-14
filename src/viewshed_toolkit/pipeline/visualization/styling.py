"""Shared palettes, legends, display scaling, and Folium controls."""

from __future__ import annotations

from typing import Any

import folium
import numpy as np
import pandas as pd
from branca.element import Element, MacroElement, Template
from folium.plugins import MeasureControl
from matplotlib import colormaps
from matplotlib.colors import LinearSegmentedColormap, to_hex

from .data import ViewshedMapConfig

VISIBILITY_HEAT_COLORS = (
    "#32c7f3",
    "#61d9d5",
    "#9ee59b",
    "#dce85a",
    "#ffe13b",
    "#f9a431",
    "#ed5b27",
    "#d52222",
    "#941936",
)


class _ClickToCopyCoordinates(MacroElement):
    """Leaflet interaction that opens copyable latitude/longitude on map click."""

    _template = Template("""
        {% macro script(this, kwargs) %}
        {{ this._parent.get_name() }}.on('click', function(event) {
            const coordinates = event.latlng.lat.toFixed(6) + ', ' +
                event.latlng.lng.toFixed(6);
            const wrapper = L.DomUtil.create('div');
            wrapper.style.minWidth = '230px';

            const label = L.DomUtil.create('div', '', wrapper);
            label.textContent = 'Latitude, longitude';
            label.style.marginBottom = '5px';
            label.style.fontWeight = '600';

            const input = L.DomUtil.create('input', '', wrapper);
            input.type = 'text';
            input.readOnly = true;
            input.value = coordinates;
            input.setAttribute('aria-label', 'Clicked latitude and longitude');
            input.style.boxSizing = 'border-box';
            input.style.width = '100%';
            input.style.marginBottom = '6px';

            const button = L.DomUtil.create('button', '', wrapper);
            button.type = 'button';
            button.textContent = 'Copy coordinates';

            const fallbackCopy = function() {
                input.focus();
                input.select();
                const copied = document.execCommand('copy');
                button.textContent = copied ? 'Copied' : 'Select and copy';
            };
            button.addEventListener('click', function() {
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(coordinates).then(function() {
                        button.textContent = 'Copied';
                    }).catch(fallbackCopy);
                } else {
                    fallbackCopy();
                }
            });
            L.DomEvent.disableClickPropagation(wrapper);

            L.popup({maxWidth: 280})
                .setLatLng(event.latlng)
                .setContent(wrapper)
                .openOn({{ this._parent.get_name() }});
        });
        {% endmacro %}
        """)

    def __init__(self) -> None:
        super().__init__()
        self._name = "ClickToCopyCoordinates"


def add_click_to_copy_coordinates(map_: folium.Map) -> None:
    """Make a Folium map click open a latitude/longitude copy control."""

    _ClickToCopyCoordinates().add_to(map_)


def _add_map_controls(map_: folium.Map) -> None:
    add_click_to_copy_coordinates(map_)
    MeasureControl(primary_length_unit="kilometers").add_to(map_)
    folium.LayerControl(collapsed=False).add_to(map_)


def _resolve_colormap(name: str) -> Any:
    normalized = str(name).strip().lower().replace("-", "_")
    if normalized == "visibility_heat":
        return LinearSegmentedColormap.from_list(
            "visibility_heat",
            VISIBILITY_HEAT_COLORS,
            N=256,
        )
    return colormaps.get_cmap(name)


def _map_colors(config: ViewshedMapConfig, count: int = 11) -> list[str]:
    cmap = _resolve_colormap(config.colormap_name).resampled(count)
    return [to_hex(cmap(index / max(1, count - 1)), keep_alpha=False) for index in range(count)]


def _display_max(values: pd.Series | np.ndarray, quantile: float) -> float:
    array = np.asarray(pd.to_numeric(pd.Series(np.ravel(values)), errors="coerce"), dtype=float)
    valid = array[np.isfinite(array) & (array > 0)]
    if valid.size == 0:
        return 1.0
    clipped_quantile = min(1.0, max(0.0, float(quantile)))
    value = float(np.quantile(valid, clipped_quantile))
    return value if value > 0 else float(valid.max())


def _generalized_visibility_legend(colors: list[str], class_count: int) -> str:
    swatches = "".join(
        f'<span style="display:block;flex:1;height:14px;background:{color};"></span>'
        for color in colors
    )
    return f"""
    <div style="position:fixed;bottom:32px;left:48px;z-index:9999;
                background:rgba(255,255,255,0.94);padding:9px 11px;
                border:1px solid #666;border-radius:4px;min-width:270px;
                font:13px/1.25 Arial,sans-serif;color:#111;">
      <div style="font-weight:600;margin-bottom:5px;">Generalized visibility ({class_count} classes)</div>
      <div style="display:flex;width:100%;">{swatches}</div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;">
        <span>Poor visibility</span><span>High visibility</span>
      </div>
    </div>
    """


def _map_note(
    map_: folium.Map,
    *,
    title: str,
    config: ViewshedMapConfig,
) -> None:
    colors = ", ".join(_map_colors(config, count=9))
    gradient = "linear-gradient(90deg, " + colors + ")"
    map_.get_root().html.add_child(Element(f"""
            <div style="position:fixed;left:14px;bottom:28px;z-index:9999;
                        width:310px;background:rgba(255,255,255,.94);padding:10px 12px;
                        border:1px solid #777;border-radius:5px;font:13px/1.35 sans-serif;">
              <div style="font-weight:700;margin-bottom:5px;">{title}</div>
              <div style="height:10px;background:{gradient};margin-bottom:3px;"></div>
              <div style="display:flex;justify-content:space-between;"><span>Low</span><span>High</span></div>
              <div style="margin-top:5px;color:#444;">Each factor is independently scaled
              from zero to its configured display quantile. Hover an original H3 layer
              for its unscaled grid value.</div>
            </div>
            """))
