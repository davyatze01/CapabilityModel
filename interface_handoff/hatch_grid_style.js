// 45-degree diagonal hatch fill for the capability grid layer, rendered by
// stretching a pre-baked raster swatch over each hexagon every frame.
//
// Why this exists: pipeline_runner.py styles the grid layer in QGIS with
// QgsLinePatternFillSymbolLayer (a true diagonal-hatch fill, 5 density
// classes, RenderMetersInMapUnits so line COUNT per hexagon stays constant
// across zoom -- only the on-screen thickness/spacing grow as you zoom in).
// qgis2web has no equivalent for that symbol type, so the exported
// OpenLayers style silently drops or degrades it.
//
// Two earlier attempts here both flickered on zoom:
//  1. Live line-stroking sized in ground meters (mirroring QGIS exactly):
//     stroke width tracks live resolution, so at some zoom it's thin enough
//     to hit canvas's sub-pixel antialiasing threshold and disappear.
//  2. A tiled CanvasPattern fill at a fixed screen size: stable, but the
//     pattern repeats in screen space, so a hexagon shows MORE repeats as
//     it grows on screen while zooming in -- i.e. line count is no longer
//     constant per hexagon, which defeats the point (the hatch density is
//     the legend).
//
// This version keeps the per-hexagon-constant-count property AND avoids
// live stroking: each class's hatch is drawn ONCE into an offscreen square
// canvas at a fixed reference resolution (SWATCH_PX), then every frame is
// just `ctx.drawImage(swatch, ...)` stretched to the feature's current pixel
// bounding box, clipped to the hexagon. The swatch image scales together
// with the hexagon as you zoom (same one image, mapped once), so the
// apparent line count per hexagon stays constant, and image scaling has no
// sub-pixel disappearance failure mode the way live stroke rendering does
// (it just blurs at extreme scales, same as QGIS's own "cells read as one
// gray tone at full extent" design intent).
//
// Usage (see HATCH_GRID_INTEGRATION.md):
//
//   var style_Cagliari_Shapefilecapability_grid_1 = window.makeHatchGridStyle(
//       function (feature) { return feature.get(selectedCapability); },
//       function (feature) { return !selectedHexId || feature.get('hex_id') === selectedHexId; }
//   );
//
// getValue(feature)     -> the capability value in [0, 1] to classify.
// getHexFilter(feature) -> optional; return false to skip a feature entirely
//                          (used for the "highlight one hex" mode).

(function () {
    // ELECTRE class boundaries -- must match `electre_bounds` in
    // pipeline_runner.py. 5 classes: Very Low/Low/Medium/High/Very High.
    var ELECTRE_BOUNDS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0];

    // Same numbers as hatch_distance_fractions / hatch_line_width_fractions
    // in pipeline_runner.py -- fractions of the swatch size.
    var DISTANCE_FRACTIONS = [1 / 1.5, 1 / 2.5, 1 / 3.5, 1 / 4.5, 1 / 6];
    var WIDTH_FRACTIONS = [1 / 45, 1 / 20, 1 / 12, 1 / 9, 1 / 8];

    // Resolution (px) of the pre-baked square swatch, before it gets
    // stretched to fit each hexagon's actual on-screen size. High enough
    // that upscaling a couple of zoom levels still looks reasonably crisp.
    var SWATCH_PX = 256;
    var MIN_WIDTH_PX = 1.1;
    // 50%-opacity black, matching LINE_OPACITY in utils/hatch_raster.py (the
    // baked legend.png + map rasters). Solid black here made the map hatch read
    // much darker than the legend swatches it's supposed to match.
    var HATCH_COLOR = 'rgba(0, 0, 0, 0.5)';

    function classify(rawValue) {
        // Check null/undefined BEFORE numeric coercion: Number(null) is 0, not NaN,
        // so a caller that pre-coerces (e.g. classify(getValue(feature))) would
        // silently misclassify missing data as "Very Low" instead of "no data" here.
        if (rawValue === null || rawValue === undefined) return -1;
        var value = Number(rawValue);
        if (isNaN(value)) return -1;
        for (var i = 0; i < ELECTRE_BOUNDS.length - 1; i++) {
            var lo = ELECTRE_BOUNDS[i];
            var hi = ELECTRE_BOUNDS[i + 1];
            var isLast = i === ELECTRE_BOUNDS.length - 2;
            if (value >= lo && (value < hi || (isLast && value <= hi))) return i;
        }
        return -1;
    }

    function makeSwatch(cls) {
        var distancePx = SWATCH_PX * DISTANCE_FRACTIONS[cls];
        var widthPx = Math.max(SWATCH_PX * WIDTH_FRACTIONS[cls], MIN_WIDTH_PX);

        var canvas = document.createElement('canvas');
        canvas.width = SWATCH_PX;
        canvas.height = SWATCH_PX;
        var ctx = canvas.getContext('2d');
        ctx.strokeStyle = HATCH_COLOR;
        ctx.lineWidth = widthPx;
        ctx.lineCap = 'butt';

        // Sweep 45-degree lines across the swatch, spaced distancePx apart
        // (perpendicular gap), starting/ending past the edges so the swatch
        // tile itself has full-bleed stripes right to its corners.
        var step = distancePx * Math.SQRT2;
        ctx.beginPath();
        for (var offset = -SWATCH_PX; offset <= SWATCH_PX * 2; offset += step) {
            ctx.moveTo(offset, 0);
            ctx.lineTo(offset - SWATCH_PX, SWATCH_PX);
        }
        ctx.stroke();
        return canvas;
    }

    var swatchCache = [];
    function getSwatch(cls) {
        if (!swatchCache[cls]) swatchCache[cls] = makeSwatch(cls);
        return swatchCache[cls];
    }

    function boundingBoxOf(polygons) {
        var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
        polygons.forEach(function (rings) {
            rings.forEach(function (ring) {
                for (var i = 0; i < ring.length; i++) {
                    var x = ring[i][0], y = ring[i][1];
                    if (x < minX) minX = x;
                    if (x > maxX) maxX = x;
                    if (y < minY) minY = y;
                    if (y > maxY) maxY = y;
                }
            });
        });
        return [minX, minY, maxX, maxY];
    }

    function clipToPolygons(ctx, polygons) {
        ctx.beginPath();
        polygons.forEach(function (rings) {
            rings.forEach(function (ring) {
                for (var i = 0; i < ring.length; i++) {
                    if (i === 0) ctx.moveTo(ring[i][0], ring[i][1]);
                    else ctx.lineTo(ring[i][0], ring[i][1]);
                }
                ctx.closePath();
            });
        });
        ctx.clip();
    }

    window.makeHatchGridStyle = function (getValue, getHexFilter) {
        return new ol.style.Style({
            renderer: function (pixelCoordinates, state) {
                var feature = state.feature;
                if (getHexFilter && !getHexFilter(feature)) return;

                var cls = classify(getValue(feature));
                if (cls < 0) return;

                var geometryType = state.geometry.getType();
                var polygons = geometryType === 'MultiPolygon' ? pixelCoordinates : [pixelCoordinates];

                var bbox = boundingBoxOf(polygons);
                if (!isFinite(bbox[0])) return;

                var ctx = state.context;
                ctx.save();
                clipToPolygons(ctx, polygons);
                ctx.drawImage(getSwatch(cls), bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1]);
                ctx.restore();
            }
        });
    };

    // Reference size (in screen pixels) the DISTANCE_FRACTIONS/WIDTH_FRACTIONS
    // ladder is measured against for the fixed-screen-size variant below. Set to
    // 96 to match render_hatch_legend_image's swatch_px (utils/hatch_raster.py):
    // the legend swatch's line spacing is 96 * DISTANCE_FRACTIONS, so using the
    // same 96 here makes the map's on-screen hatch spacing/width identical to the
    // legend swatches. It does NOT depend on hexagon size or zoom, unlike
    // SWATCH_PX above (which maps to a per-hexagon image), so the pattern stays
    // fixed on screen at every zoom level. Tune to taste (larger = coarser).
    var FIXED_SCREEN_REF_PX = 96;

    // Alternative to makeHatchGridStyle: keeps the hatch pattern's on-screen
    // line spacing/width CONSTANT across zoom levels (same visual density
    // whether zoomed in or out), instead of QGIS's default of constant line
    // COUNT per hexagon (spacing growing as you zoom in -- see the big header
    // comment above and makeHatchGridStyle's own docstring). This is
    // approach #2 from that header comment, done via direct line-stroking in
    // the renderer's already-screen-pixel coordinate space instead of a tiled
    // CanvasPattern -- no swatch image, no stretching, so there's no
    // resolution-dependent blur/moire either.
    //
    // Pick this one if you want the map to "look the same" at every zoom;
    // pick makeHatchGridStyle if you want the QGIS-native behavior of a
    // constant line count per hexagon (i.e. per-hexagon density carries
    // meaning regardless of how zoomed in you are).
    window.makeHatchGridStyleFixedScreen = function (getValue, getHexFilter) {
        return new ol.style.Style({
            renderer: function (pixelCoordinates, state) {
                var feature = state.feature;
                if (getHexFilter && !getHexFilter(feature)) return;

                var cls = classify(getValue(feature));
                if (cls < 0) return;

                var geometryType = state.geometry.getType();
                var polygons = geometryType === 'MultiPolygon' ? pixelCoordinates : [pixelCoordinates];

                var bbox = boundingBoxOf(polygons);
                if (!isFinite(bbox[0])) return;

                var distancePx = FIXED_SCREEN_REF_PX * DISTANCE_FRACTIONS[cls];
                var widthPx = Math.max(FIXED_SCREEN_REF_PX * WIDTH_FRACTIONS[cls], MIN_WIDTH_PX);
                var step = distancePx * Math.SQRT2;

                var ctx = state.context;
                ctx.save();
                clipToPolygons(ctx, polygons);
                ctx.strokeStyle = HATCH_COLOR;
                ctx.lineWidth = widthPx;
                ctx.lineCap = 'butt';
                ctx.beginPath();
                // Each 45-degree line is the locus x + y = c. Anchoring c to a GLOBAL
                // canvas-pixel grid (multiples of `step` from the canvas origin) rather
                // than to this hexagon's own bbox corner is what makes every hexagon of
                // the same class share ONE continuous hatch field: neighbouring cells
                // line up seamlessly instead of each starting the pattern at its own
                // phase (which showed up as broken/dashed diagonals and uneven-looking
                // density across hexagon seams). Spacing/width stay in fixed screen
                // pixels, so the pattern still looks identical at every zoom level.
                var cMin = bbox[0] + bbox[1];
                var cMax = bbox[2] + bbox[3];
                var c = Math.floor(cMin / step) * step;
                for (; c <= cMax; c += step) {
                    ctx.moveTo(c - bbox[1], bbox[1]);
                    ctx.lineTo(c - bbox[3], bbox[3]);
                }
                ctx.stroke();
                ctx.restore();
            }
        });
    };

    // Class labels, in the same Very Low -> Very High order as the fraction
    // ladders above and as render_hatch_legend_image in utils/hatch_raster.py.
    var ELECTRE_LABELS = [
        'Very Low (0.0–0.2)',
        'Low (0.2–0.4)',
        'Medium (0.4–0.6)',
        'High (0.6–0.8)',
        'Very High (0.8–1.0)'
    ];

    // ===== Millimetre (screen-constant) hatch ============================
    // Spacing + width given in real millimetres, per capability level. Unlike
    // the fraction-of-cell ladders above (which scale with the map), mm is a
    // screen/print unit: the pattern looks the SAME at every zoom, and more
    // stripes fall inside a hexagon as it grows on screen -- exactly QGIS's
    // "millimetres" symbol unit (as opposed to RenderMetersInMapUnits).
    // Very Low -> Very High, matching the class order everywhere else here.
    var HATCH_SPACING_MM = [9.5, 7.5, 5.5, 3.5, 1.5];   // 2 mm step between levels
    // Progressive line width from 0.1 mm (Very Low) to 0.5 mm (Very High) in
    // 0.1 mm steps, so the level reads from BOTH tightening spacing and bolder
    // strokes. Per class, Very Low -> Very High.
    var HATCH_WIDTH_MM = [0.1, 0.2, 0.3, 0.4, 0.5];
    var PX_PER_MM = 96 / 25.4;               // CSS reference (96 CSS px = 1 in)

    // Build a seamless 45-degree line-hatch CanvasPattern for one class at the
    // given device-pixel ratio. Using a CanvasPattern as an ol.style.Fill color
    // is the OpenLayers-native, flicker-free way to get a screen-constant hatch
    // (a plain fill, cached and tiled by the renderer -- NOT a per-feature
    // custom renderer, which is what the earlier vector attempts used and why
    // they flickered; see the header comment). The tile repeats in screen
    // space, so the hatch stays fixed on screen as you zoom -- the definition
    // of a millimetre unit.
    var _mmPatternCache = {};
    function getMMPattern(cls, pixelRatio) {
        var key = cls + '@' + pixelRatio;
        if (_mmPatternCache[key]) return _mmPatternCache[key];
        // Perpendicular spacing / stroke width in DEVICE px (OL fills in device
        // pixels; multiplying by pixelRatio keeps true mm on HiDPI screens).
        var spacingPx = HATCH_SPACING_MM[cls] * PX_PER_MM * pixelRatio;
        var widthPx = Math.max(HATCH_WIDTH_MM[cls] * PX_PER_MM * pixelRatio, 1);
        // Anti-diagonal lines are the locus x + y = c. Spacing them by t in c
        // gives a perpendicular gap of t / sqrt(2); we want that to equal
        // spacingPx, so t = spacingPx * sqrt(2). A square tile of side t is then
        // exactly one period in both x and y, so 'repeat' tiles seamlessly.
        var t = Math.max(2, Math.round(spacingPx * Math.SQRT2));
        var tile = document.createElement('canvas');
        tile.width = t;
        tile.height = t;
        var tctx = tile.getContext('2d');
        tctx.strokeStyle = HATCH_COLOR;
        tctx.lineWidth = widthPx;
        tctx.lineCap = 'butt';
        tctx.beginPath();
        // c = 0 and c = 2t stroke the two corners (so the line straddling a tile
        // edge is completed by the neighbouring tile); c = t is the main
        // diagonal. Together they tile into continuous 45-degree stripes.
        for (var c = 0; c <= 2 * t; c += t) {
            tctx.moveTo(c, 0);
            tctx.lineTo(c - t, t);
        }
        tctx.stroke();
        var pattern = tctx.createPattern(tile, 'repeat');
        _mmPatternCache[key] = pattern;
        return pattern;
    }

    // Hexagon outline, matching pipeline_runner.py's "Capability grid outline"
    // layer (qgis_grid_outline_color 90,90,90,170 -> ~0.667 alpha,
    // qgis_grid_outline_width 0.35 mm). ol.style.Stroke width is in CSS px
    // (OpenLayers applies the device-pixel ratio itself), so no *pr here.
    var OUTLINE_COLOR = 'rgba(90, 90, 90, 0.667)';
    var OUTLINE_WIDTH_MM = 0.35;

    // Returns an OpenLayers STYLE FUNCTION (feature -> Style) that fills each
    // hexagon with its class's millimetre hatch AND strokes its border. Same
    // getValue/getHexFilter contract as makeHatchGridStyle. Because the fill is
    // a screen-space CanvasPattern, the hatch is identical at every zoom and a
    // static legend drawn at the same mm scale always matches it.
    window.makeHatchGridStyleMM = function (getValue, getHexFilter) {
        var pr = window.devicePixelRatio || 1;
        var styleCache = [];
        return function (feature) {
            if (getHexFilter && !getHexFilter(feature)) return null;
            var cls = classify(getValue(feature));
            if (cls < 0) return null;
            if (!styleCache[cls]) {
                styleCache[cls] = new ol.style.Style({
                    fill: new ol.style.Fill({ color: getMMPattern(cls, pr) }),
                    stroke: new ol.style.Stroke({
                        color: OUTLINE_COLOR,
                        width: OUTLINE_WIDTH_MM * PX_PER_MM
                    })
                });
            }
            return styleCache[cls];
        };
    };

    // Exposed so a dynamic (zoom-tracking) legend can draw swatches that match
    // the map exactly, instead of relying on a fixed baked legend.png.
    window.HATCH_GRID_CONSTANTS = {
        DISTANCE_FRACTIONS: DISTANCE_FRACTIONS,
        WIDTH_FRACTIONS: WIDTH_FRACTIONS,
        ELECTRE_BOUNDS: ELECTRE_BOUNDS,
        ELECTRE_LABELS: ELECTRE_LABELS,
        HATCH_COLOR: HATCH_COLOR,
        MIN_WIDTH_PX: MIN_WIDTH_PX,
        HATCH_SPACING_MM: HATCH_SPACING_MM,
        HATCH_WIDTH_MM: HATCH_WIDTH_MM,
        PX_PER_MM: PX_PER_MM,
        OUTLINE_COLOR: OUTLINE_COLOR,
        OUTLINE_WIDTH_MM: OUTLINE_WIDTH_MM
    };

    // Flat-top hexagon path (first vertex east, matching the demo's hex build
    // and context.py's tiling), inscribed in a `size`-px square centred at
    // (size/2, size/2), leaving `margin` px for the outline stroke.
    function _hexPath(ctx, size, margin) {
        var cx = size / 2, cy = size / 2, r = size / 2 - margin;
        ctx.beginPath();
        for (var i = 0; i < 6; i++) {
            var a = Math.PI / 180 * (60 * i);
            var x = cx + r * Math.cos(a), y = cy + r * Math.sin(a);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.closePath();
    }

    // Draw one class's hatch clipped to a hexagon that fills `canvas`, plus the
    // hexagon outline -- a 1:1 preview of a real map cell. spacingPx/widthPx/
    // outlineWidthPx are DEVICE pixels (so the caller can size the canvas to the
    // current on-screen cell size and get a faithful preview whose line COUNT
    // grows as you zoom in, while the hatch TEXTURE still matches the map).
    window.drawHatchLegendHexCell = function (canvas, spacingPx, widthPx, outlineWidthPx) {
        var S = canvas.width; // assumed square, device px
        var ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, S, S);
        var margin = Math.max(2, outlineWidthPx);
        ctx.save();
        _hexPath(ctx, S, margin);
        ctx.clip();
        ctx.strokeStyle = HATCH_COLOR;
        ctx.lineWidth = Math.max(widthPx, 1);
        ctx.lineCap = 'butt';
        var step = spacingPx * Math.SQRT2;
        ctx.beginPath();
        for (var offset = -S; offset <= 2 * S; offset += step) {
            ctx.moveTo(offset, 0);
            ctx.lineTo(offset - S, S);
        }
        ctx.stroke();
        ctx.restore();
        // Outline on top (path retraced since clip() consumed the previous one).
        _hexPath(ctx, S, margin);
        ctx.strokeStyle = OUTLINE_COLOR;
        ctx.lineWidth = Math.max(outlineWidthPx, 1);
        ctx.stroke();
    };

    // Draw one class's 45-degree hatch into `canvas`, with the perpendicular
    // line spacing (distancePx) and stroke width (widthPx) given directly in
    // screen pixels -- the SAME line-drawing math the map renderers use, so a
    // legend swatch drawn with the map's CURRENT on-screen spacing/width looks
    // identical to the hatch on the map at that zoom. `makeHatchGridStyle`
    // stretches a swatch to each hexagon's on-screen bbox, so the map's current
    // spacing for a class is (hexagon bbox width in px) * DISTANCE_FRACTIONS[cls]
    // and its width is that same px * WIDTH_FRACTIONS[cls]; feed those here to
    // keep the legend in lockstep with the map as the user zooms.
    window.drawHatchLegendSwatch = function (canvas, distancePx, widthPx) {
        var box = canvas.width; // assumed square
        var ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, box, box);
        ctx.strokeStyle = HATCH_COLOR;
        ctx.lineWidth = Math.max(widthPx, MIN_WIDTH_PX);
        ctx.lineCap = 'butt';
        var step = distancePx * Math.SQRT2;
        // A line with top-edge x-intercept `offset`, running to (offset - box, box),
        // passes through the box centre when offset === box. Anchor the sweep on
        // that centre line and fan out both ways, so even when the spacing is
        // larger than the box (very zoomed in, sparse classes) the swatch still
        // shows a representative line instead of occasionally rendering blank.
        var span = Math.ceil(box / step) + 1;
        ctx.beginPath();
        for (var k = -span; k <= span; k++) {
            var offset = box + k * step;
            ctx.moveTo(offset, 0);
            ctx.lineTo(offset - box, box);
        }
        ctx.stroke();
    };
})();
