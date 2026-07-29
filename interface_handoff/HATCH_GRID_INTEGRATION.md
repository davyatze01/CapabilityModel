# Fixing the capability-grid hatch fill in the OpenLayers export

## The problem

In QGIS, `capability_grid` is styled with `QgsLinePatternFillSymbolLayer`: a
true diagonal-hatch fill, 5 density classes (Very Low → Very High), sized in
`RenderMetersInMapUnits` so each hexagon always shows the same number of
stripes regardless of zoom (they just get thicker/thinner on screen as you
zoom). This renders correctly in QGIS.

qgis2web has no equivalent for that symbol type. When the project is
exported, the hatch is dropped or degraded — in the current export almost
every cell shows only its bare outline, with no fill at all, and only a
stray region shows anything hatch-like. This is an export-plugin limitation,
not a data or config problem; the underlying `.gpkg` and the QGIS project
are correct.

## The fix

`hatch_grid_style.js` (in this folder) reimplements the hatch by stretching a
**pre-baked raster swatch** over each hexagon, bypassing qgis2web's style
translation for this one layer.

Two earlier attempts here both flickered on zoom:

1. Drawing the lines live every frame, sized in ground meters to mirror
   QGIS's `RenderMetersInMapUnits` (constant line count per hexagon
   regardless of zoom): stroke width tracked the live resolution, so at some
   zoom level it swept through a sub-pixel value and disappeared — the same
   canvas-antialiasing flicker that affected the hexagon border stroke
   before `qgis_grid_outline_width` was widened to a fixed screen size.
2. A tiled `CanvasPattern` fill at a fixed screen size: stable, but the
   pattern repeats in *screen* space, so a hexagon shows more repeats as it
   grows bigger on screen while zooming in — line count is no longer
   constant per hexagon, which defeats the point (the density *is* the
   legend).

This version keeps both properties at once: each class's hatch is drawn
**once** into an offscreen square canvas (`SWATCH_PX`, a fixed reference
resolution), and every frame the renderer just clips to the hexagon and
calls `ctx.drawImage(swatch, ...)` stretched to that hexagon's *current* pixel
bounding box. Because it's one image mapped once per hexagon (not tiled), it
scales together with the hexagon as you zoom, so the line count stays
constant — matching QGIS. And because it's image scaling, not live line
stroking, there's no sub-pixel-width failure mode: a scaled-down image just
blurs (which is in fact the original design's intent — "at full extent a
cell reads as one gray tone equal to the ink coverage" per the comment in
`pipeline_runner.py`), it never disappears.

It exposes one function:

```js
window.makeHatchGridStyle(getValue, getHexFilter) -> ol.style.Style
```

- `getValue(feature)` — returns the capability value (0–1) to classify into
  one of the 5 ELECTRE bands. Pass whatever currently selects the active
  capability field.
- `getHexFilter(feature)` — optional; return `false` to skip a feature
  entirely. Pass through the existing "highlight one hex" logic if present.

The 5 classes' relative spacing/width ladder and the ELECTRE bounds mirror
`pipeline_runner.py` (`hatch_distance_fractions`, `hatch_line_width_fractions`,
`electre_bounds`), scaled here against a fixed `SWATCH_PX` reference instead
of ground meters. If the ladder is ever retuned in the Python pipeline,
mirror the new *ratios* into `DISTANCE_FRACTIONS` / `WIDTH_FRACTIONS` at the
top of `hatch_grid_style.js`; `SWATCH_PX` / `MIN_WIDTH_PX` are purely local
design constants (bump `SWATCH_PX` for a crisper swatch, e.g. if hexagons
render very large on screen).

## Steps

1. Copy `hatch_grid_style.js` into `resources/`.

2. Add it to `index.html`, before `layers/Cagliari_Shapefilecapability_grid_1.js`:

   ```html
   <script src="resources/hatch_grid_style.js"></script>
   ```

3. In `styles/Cagliari_Shapefilecapability_grid_1_style.js`, replace the
   `style_Cagliari_Shapefilecapability_grid_1` function (the one built from
   `style_Cagliari_Shapefilecapability_grid_1_cache`) with:

   ```js
   var style_Cagliari_Shapefilecapability_grid_1 = window.makeHatchGridStyle(
       function (feature) { return feature.get(selectedCapability); },
       function (feature) { return !selectedHexId || feature.get('hex_id') === selectedHexId; }
   );
   ```

   `makeHatchGridStyle` keeps line COUNT constant per hexagon (QGIS's default
   `RenderMetersInMapUnits` behavior) — on-screen spacing/thickness grow as
   you zoom in. If instead you want the pattern to look visually IDENTICAL
   at every zoom level (constant screen-pixel spacing, more repeats per
   hexagon as it grows on screen), use `window.makeHatchGridStyleFixedScreen`
   instead — same call signature, drop-in swap. Compare
   `hatch_grid_test.html` vs `hatch_grid_fixed_screen_test.html` to see the
   difference directly.

   Keep `selectedCapability` / `selectedHexId` and whatever sets them
   elsewhere in the interface unchanged — this is a drop-in replacement for
   the style function only, everything that reads/writes those globals stays
   the same.

4. The old `style_Cagliari_Shapefilecapability_grid_1_cache` array and
   `whiteHexStyle` become unused if nothing else references them and can be
   removed.

5. This is layer-specific: it only replaces the grid layer's style. The
   separate hexagon outline (thin grey border, drawn by
   `qgis_grid_outline_width`/`qgis_grid_outline_color`) is a different QGIS
   layer/style and is unaffected — leave it as qgis2web exports it.

## Trying it standalone

`hatch_grid_test.html` in this folder draws six synthetic hexagons — one per
ELECTRE class plus one no-data cell — using `hatch_grid_style.js` against a
plain OpenLayers map (loaded from `../interfaccia/resources/ol.js`, so no
internet or build step needed). Open it over `file://` to sanity-check the
hatch visually before wiring it into the real interface: zoom in/out and
confirm (a) each hexagon keeps the same apparent line count at every zoom
level (no flicker/disappearing, no extra stripes appearing as you zoom in),
and (b) class boldness/frequency reads as a clear ladder from Very Low to
Very High.

## Notes / limitations

- The 5 swatches are built once (lazily, on first use) and cached — styling
  ~2,000 grid cells costs one classification + one `drawImage` call per
  feature per frame, not a redraw of individual lines.
- `cell_size_m` on the feature is not used by the style (the swatch is
  stretched to the feature's actual pixel bounding box each frame, whatever
  that is) — safe to ignore if you see it in `fieldAliases`.
- At extreme zoom-out, a downscaled swatch can look closer to a flat gray
  tone than distinct stripes — this matches the original design intent (see
  the comment on `hatch_distance_fractions` in `pipeline_runner.py`), not a
  bug.
