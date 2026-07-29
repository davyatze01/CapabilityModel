# Static hatch-grid rasters for the OpenLayers export

**This supersedes `HATCH_GRID_INTEGRATION.md`.** That approach restyled the
live `capability_grid` vector layer in JavaScript and flickered on zoom no
matter how it was implemented (three attempts: live line-stroking, a tiled
`CanvasPattern`, and a per-feature pre-baked-swatch `drawImage`) — something
in OpenLayers' vector render pipeline recomputes per frame and kept breaking
the hatch. This version sidesteps that entirely: the hatch is now baked
server-side, once, into a static raster per capability, and the web viewer
just displays it as a plain georeferenced image — the same code path as any
raster basemap tile, with nothing to recompute and nothing to flicker.

## What's generated, and where it lives

`generate_experiment_shapefiles.py`'s `generate_combined_experiment_gpkg`
(called from both the main pipeline and `rebuild_qgis_output.py`) bakes the
hatch for each capability with `utils/hatch_raster.py`, at the grid's **true
ground scale** and in the **same style** as `pipeline_runner.py`'s QGIS
renderer: same `electre_bounds` / `hatch_distance_fractions` /
`hatch_line_width_fractions` ladder, same 5 ELECTRE classes, same
`has_data = 1` filter (hull-fill cells with no real measurement are left
transparent), same `qgis_grid_opacity`-equivalent transparency (white
background at 25% opacity, black lines at 50% — see `BACKGROUND_OPACITY` /
`LINE_OPACITY` in `utils/hatch_raster.py`), and the line phase anchored to a
shared canvas-wide origin so adjacent same-class hexagons form one
continuous stripe field instead of restarting per hexagon.

Written to `outputs/gpkg/<slug>/hatch/`:

```
outputs/gpkg/<slug>/hatch/
  care.png
  nutrition.png
  restorativeness.png
  manifest.json   -- plain JSON
  manifest.js      -- same content, as window.HATCH_MANIFEST (see below)
  legend.png       -- all 5 classes, labeled, one image
```

This is the **only** copy of the hatch raster, and it's the primary path for
the web interface (steps below) — the hatch gets wired into OpenLayers by
hand via `hatch_grid_static.js`, bypassing qgis2web for this one layer
entirely.

**An earlier version also embedded copies as raster tables inside the
`.gpkg` itself** (`hatch_care`/`hatch_nutrition`/`hatch_restorativeness`, via
`pipeline_runner._embed_hatch_rasters`), specifically so qgis2web could
export them to OpenLayers the same way it handles any raster layer. That
turned out not to produce anything usable in OpenLayers, and QGIS's own live
vector hatch (`grid_layer`) already renders sharper anyway — so the embedded
copies added 94% to the `.gpkg`'s file size (~30MB of a ~30.5MB file) for no
remaining benefit and have been removed. `_embed_hatch_rasters` no longer
exists; `.gpkg`s only carry the vector layers now.

`manifest.json`/`manifest.js` give each PNG's extent in EPSG:4326 (the same
CRS as every other layer in the gpkg -- these rasters are a plain image
version of `zz_capability_grid`, not a separately reprojected artifact), for
`ol.source.ImageStatic`:

```json
{
  "crs": "EPSG:4326",
  "capabilities": {
    "care":            {"path": "...", "extent": [minx, miny, maxx, maxy], "width_px": ..., "height_px": ...},
    "nutrition":        {...},
    "restorativeness":  {...}
  },
  "legend": {"path": ".../hatch/legend.png", "classes": ["Very Low (0.0–0.2)", ...], "bounds": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]}
}
```

`manifest.js` is the identical object assigned to `window.HATCH_MANIFEST`
instead of written as a bare JSON file — the interface runs over `file://`,
where `fetch()` of local JSON is blocked by the browser, so (like every
other data file the interface loads) it has to arrive as a `<script>` tag
global rather than be fetched. `hatch_grid_static.js` reads
`window.HATCH_MANIFEST` when present and only falls back to `fetch()` for
http(s) deployments that skip the script tag.

`hatch/legend.png` is one combined image, all 5 ELECTRE classes stacked
vertically (Very High at top to Very Low at bottom) with their labels baked
in (`render_hatch_legend_image` in `utils/hatch_raster.py`), rendered in
the exact same style as the map rasters (same spacing/width ladder, 45° angle, semi-transparent white
background with 50%-opacity black lines; each swatch spans one nominal
100 m grid cell). A single ready-to-use `<img>` — no JS assembly needed —
in place of the qgis2web-generated legend for the old vector hatch layer.
Text uses DejaVuSans (bundled with matplotlib, already a project
dependency) since PIL's default bitmap font has no glyph for the en dash in
the labels.

## The JS

`hatch_grid_static.js` (in this folder) exposes two functions:

```js
window.makeHatchGridImageLayers(manifestUrl, pngBaseUrl) -> Promise<{care, nutrition, restorativeness}>
window.setHatchCapability(hatchLayers, capability)
```

`makeHatchGridImageLayers` reads `window.HATCH_MANIFEST` (set by
`manifest.js`) if present, else falls back to `fetch(manifestUrl)`, and
returns one `ol.layer.Image` per capability (all created `visible: false`).
`setHatchCapability` shows the one matching `capability` and hides the other
two — call it whenever `selectedCapability` changes elsewhere in the
interface, in place of whatever previously re-styled the grid layer.

## Steps

1. Copy the whole `hatch/` folder (3 PNGs, `manifest.json`, `manifest.js`,
   `legend/`) from `outputs/gpkg/<slug>/` into the web export, e.g.
   `resources/hatch/`. Re-copy it after every pipeline/rebuild run — these
   are regenerated outputs, not hand-edited files.

2. Add `manifest.js` and `hatch_grid_static.js` to `index.html`, in that
   order, **before** whatever script calls `makeHatchGridImageLayers`
   (matches the pattern every other data file in the interface already
   uses — see e.g. `hex_shard_loader.js`'s manifest handling):

   ```html
   <script src="resources/hatch/manifest.js"></script>
   <script src="resources/hatch_grid_static.js"></script>
   ```

3. Where the map and `layersList` are set up (`layers/layers.js` or
   wherever `map.addLayer` is called for the other layers), add:

   ```js
   window.makeHatchGridImageLayers('resources/hatch/manifest.json', 'resources/hatch/')
       .then(function (hatchLayers) {
           Object.keys(hatchLayers).forEach(function (cap) { map.addLayer(hatchLayers[cap]); });
           window.setHatchCapability(hatchLayers, selectedCapability);
           // keep a reference so the capability switch below can reach it:
           window.__hatchLayers = hatchLayers;
       });
   ```

   (The `manifestUrl` argument is only used as a `fetch()` fallback if
   `manifest.js` wasn't loaded — with the script tag from step 2 in place,
   it's effectively unused, but keep it for anyone running the page over
   http(s) without the script tag.)

4. Wherever `selectedCapability` currently gets reassigned (the same place
   that used to trigger a `style_Cagliari_Shapefilecapability_grid_1`
   restyle), add:

   ```js
   window.setHatchCapability(window.__hatchLayers, selectedCapability);
   ```

5. The `capability_grid` **vector** layer/style from the qgis2web export
   (`layers/Cagliari_Shapefilecapability_grid_1.js` /
   `styles/Cagliari_Shapefilecapability_grid_1_style.js`) is no longer
   needed for the hatch — these 3 PNGs replace it. You can drop those two
   `<script>` tags and files, or leave them in place unused; either is
   fine, they just won't be doing anything.

6. This only replaces the hatch. The separate hexagon outline (thin grey
   border, `qgis_grid_outline_width`/`qgis_grid_outline_color`) and the
   colored service layers are unrelated and unaffected — leave those as
   qgis2web exports them.

## Trying it standalone

`hatch_grid_static_test.html` in this folder loads the real,
pipeline-generated files from `../outputs/gpkg/Cagliari/hatch/` (run
`python3 rebuild_qgis_output.py` first if that folder doesn't exist yet) and
lets you switch between the 3 capabilities with a button. It works over
`file://` — just double-click it, no server needed, same as the rest of the
interface. Confirm each one lines up correctly over Cagliari and that
panning/zooming is completely stable — this is a plain image layer, so
there is nothing left to flicker or re-render on zoom, and the on-screen
line count per hexagon is frozen into the image (baked at
`qgis_grid_hatch_target_px` resolution), so it can't change as you zoom
in/out, unlike a live vector redraw.

## Notes / limitations

- `qgis_grid_hatch_target_px` in `config.py` (default `4096`) controls the
  max PNG dimension. Bump it for a sharper image at deep zoom (bigger file,
  slower bake); the bake itself is fast (well under a second per capability
  for ~1,500 hexagons at the default size).
- Because it's a raster now, zooming in far beyond the PNG's native
  resolution will show it getting soft/blurry rather than staying crisp —
  expected for a static image, unlike a live vector redraw. `target_max_px`
  is the knob for that trade-off.
- The 3 PNGs are regenerated on every pipeline/rebuild run alongside the
  `.gpkg`; treat them as build output, not something to hand-edit.
