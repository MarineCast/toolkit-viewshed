import base64
import json
import re
import shutil
import subprocess
import zlib
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from pyproj import Transformer

from viewshed_toolkit import export_raster_overlay_asset


def inputs():
    shape = (3, 4)
    dem = np.arange(12, dtype="float32").reshape(shape)
    dem[0, 0] = np.nextafter(np.float32(1), np.float32(2))
    chm = np.full(shape, 14, dtype="uint8")
    chm[1, 2] = 255
    flags = np.full(shape, 8, dtype="uint8")
    flags[1, 2] = 1 | 2 | 4 | 16
    return dict(
        dem=dem,
        dem_original_delta=np.zeros(shape, dtype="float32"),
        chm_raw=chm,
        chm_lower=np.full(shape, 14, dtype="float32"),
        building=np.full(shape, 7, dtype="float32"),
        joint=np.full(shape, 14, dtype="float32"),
        flags=flags,
    )


def test_native_values_and_display_georeference(tmp_path):
    arrays = inputs()
    gt = Affine(30, 0, 490000, 0, -30, 5333000)
    path = tmp_path / "s.js"
    result = export_raster_overlay_asset(
        "s",
        arrays,
        transform=gt,
        crs="EPSG:32610",
        output_path=path,
        provenance={"input": "qualified"},
        preview_pixels=32,
    )
    asset = json.loads(re.search(r"\]=(.*);", path.read_text())[1])
    for key, values in arrays.items():
        packed = asset["arrays"][key]
        raw = zlib.decompress(base64.b64decode(packed["zlib_base64"]))
        assert raw == values.tobytes()
        assert len(raw) == packed["native_bytes"]
    assert asset["previews"]["chm_raw"]["units"].startswith("m above ground")
    assert "elevation" in asset["previews"]["dem"]["label"]
    assert asset["previews"]["unknown"]["max"] == 1
    assert result["native_pixel_m"] == 30
    bounds = asset["previews"]["dem"]["bounds"]
    lon, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(490045, 5332955)
    assert bounds[0] < lon < bounds[2] and bounds[1] < lat < bounds[3]
    assert asset["previews"]["dem"]["image"].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "bad", ["shape", "crs", "transform", "precision", "nan", "flag", "negative", "pixel_size"]
)
def test_rejects_unqualified_alignment_and_values(tmp_path, bad):
    arrays = inputs()
    gt, crs = Affine(30, 0, 490000, 0, -30, 5333000), "EPSG:32610"
    if bad == "shape":
        arrays["building"] = arrays["building"][:2]
    elif bad == "crs":
        crs = "EPSG:4326"
    elif bad == "transform":
        gt = Affine(30, 1, 490000, 0, -30, 5333000)
    elif bad == "pixel_size":
        gt = Affine(60, 0, 490000, 0, -60, 5333000)
    elif bad == "precision":
        arrays["dem"] = arrays["dem"].astype("float64")
    elif bad == "nan":
        arrays["dem"][0, 0] = np.nan
    elif bad == "flag":
        arrays["flags"] = arrays["flags"].astype(bool)
    else:
        arrays["building"][0, 0] = -1
    with pytest.raises(ValueError):
        export_raster_overlay_asset(
            "s", arrays, transform=gt, crs=crs, output_path=tmp_path / "s.js", provenance={}
        )


def test_offline_native_inspection_projection_and_nodata(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for native raster inspector")
    arrays = inputs()
    gt = Affine(30, 0, 490000, 0, -30, 5333000)
    path = tmp_path / "s.js"
    export_raster_overlay_asset(
        "s", arrays, transform=gt, crs=32610, output_path=path, provenance={}, preview_pixels=32
    )
    lon, lat = Transformer.from_crs(32610, 4326, always_xy=True).transform(490075, 5332955)
    helper = (
        Path(__file__).resolve().parents[2] / "src/viewshed_toolkit/resources/raster_overlay.js"
    )
    runner = tmp_path / "check.cjs"
    runner.write_text("""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const els={},ctx=vm.createContext({document:{getElementById:id=>els[id]||(els[id]={textContent:''})},
 D:{raster_overlays:{sites:{s:{path:'s.js'}}}},fmt:x=>String(x),atob,Blob,Response,DecompressionStream});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),ctx);
vm.runInContext(fs.readFileSync(process.argv[3],'utf8'),ctx);
(async()=>{await vm.runInContext("loadRaster('s').then(a=>activeRaster=a)",ctx);
 const pixel=vm.runInContext(`inspectRaster(${process.argv[4]},${process.argv[5]})`,ctx);
 assert.equal(pixel.row,1);assert.equal(pixel.col,2);assert(Math.abs(pixel.x-490075)<.002);assert(Math.abs(pixel.y-5332955)<.002);assert.equal(pixel.chm_raw,255);
 assert.equal(pixel.building,7);assert.equal(pixel.joint,14);assert.equal(pixel.flags,23);
 assert(els['raster-pixel'].textContent.includes('NO DATA (stored255)'));
 assert(els['raster-pixel'].textContent.includes('UNKNOWN upper support'));
 assert(els['raster-pixel'].textContent.includes('m elevation'));
 assert(els['raster-pixel'].textContent.includes('m AGL'));
 const outside=vm.runInContext("inspectRaster(-130,45)",ctx);assert.equal(outside,null);
 assert(els['raster-pixel'].textContent.includes('Outside'));
 for(const xy of [[490000,5333000],[459360,5364420],[520000,5300000]]){
  const got=vm.runInContext(`toUTM(...fromUTM(${xy[0]},${xy[1]}))`,ctx);
  assert(Math.abs(got[0]-xy[0])<.002);assert(Math.abs(got[1]-xy[1])<.002);
 }
 assert(!/fetch\\(|XMLHttpRequest/.test(fs.readFileSync(process.argv[2],'utf8')));
})().catch(e=>{console.error(e);process.exit(1)});
""")
    subprocess.run(
        [node, str(runner), str(helper), str(path), str(lon), str(lat)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_raster_selection_reversed_completion_and_off(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for raster selection races")
    helper = (
        Path(__file__).resolve().parents[2] / "src/viewshed_toolkit/resources/raster_overlay.js"
    )
    runner = tmp_path / "race.cjs"
    runner.write_text("""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const els={},waits={},ctx=vm.createContext({document:{getElementById:id=>els[id]||(els[id]={textContent:''}),createElement:()=>({}),body:{appendChild(s){waits[s.src]=s.onload}}},
 D:{raster_overlays:{sites:{A:{path:'A'},B:{path:'B'}}}},
 render(){ctx.rendered=vm.runInContext('activeRaster?.source??null',ctx)}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),ctx);
vm.runInContext(`rasterLayer.value='dem';rasterSite.value='A';`,ctx);
function finish(source){ctx.VIEWSHED_RASTERS[source]={source,provenance:{site:source},crs:'EPSG:32610',contract:'native_30m_inspection_display_preview_v1',arrays:{}};waits[source]();}
(async()=>{
 const first=vm.runInContext('changeRaster()',ctx);
 vm.runInContext("rasterSite.value='B'",ctx);const second=vm.runInContext('changeRaster()',ctx);
 finish('B');await second;
 finish('A');await first;
 assert.equal(ctx.rendered,'B');assert.equal(vm.runInContext('activeRaster.source',ctx),'B');
 assert(els['raster-provenance'].textContent.includes('B'));
 vm.runInContext("delete VIEWSHED_RASTERS.A;rasterSite.value='A'",ctx);const pending=vm.runInContext('changeRaster()',ctx);
 assert.equal(vm.runInContext('activeRaster',ctx),null);
 vm.runInContext("rasterLayer.value='none'",ctx);await vm.runInContext('changeRaster()',ctx);
 finish('A');await pending;
 assert.equal(ctx.rendered,null);assert.equal(vm.runInContext('activeRaster',ctx),null);
 assert(els['raster-legend'].textContent.includes('off'));
})().catch(e=>{console.error(e);process.exit(1)});
""")
    subprocess.run([node, str(runner), str(helper)], check=True, capture_output=True, text=True)
