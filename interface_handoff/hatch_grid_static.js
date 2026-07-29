// PRIMARY PATH for the hatch in the web export. qgis2web's own raster export
// of the hatch_care/hatch_nutrition/hatch_restorativeness layers (embedded
// in the .gpkg, wired into the auto-generated QGIS project) turned out not
// to produce anything usable in OpenLayers either -- so this file, wiring
// the pre-baked PNGs in directly as plain image layers, is the way the
// hatch gets into the web interface. See HATCH_GRID_STATIC_INTEGRATION.md.
//
// Loads the 3 pre-baked, static capability-grid hatch PNGs (care, nutrition,
// restorativeness) as plain OpenLayers image overlays, and switches between
// them.
//
// This replaces hatch_grid_style.js / HATCH_GRID_INTEGRATION.md's live
// per-feature vector styling entirely. That approach flickered on zoom no
// matter how it was implemented (live line-stroking, tiled CanvasPattern,
// even a per-feature drawImage of a pre-baked swatch): OpenLayers' vector
// rendering pipeline recomputes something every frame, and something in that
// path kept breaking. The grid is now baked server-side, once, into one
// static PNG per capability at the grid's true ground scale (see
// utils/hatch_raster.py + generate_experiment_shapefiles.py), matching
// pipeline_runner.py's QGIS hatch design exactly (same electre_bounds /
// hatch_distance_fractions / hatch_line_width_fractions ladder). OpenLayers
// then just displays it as an ordinary georeferenced image -- the same code
// path as any raster basemap tile, with none of the failure modes above.
//
// Usage (see HATCH_GRID_STATIC_INTEGRATION.md):
//
//   var hatchLayers = window.makeHatchGridImageLayers(
//       "outputs/gpkg/Cagliari/hatch/manifest.json"
//   ).then(function (layers) {
//       // layers = { care: ol.layer.Image, nutrition: ..., restorativeness: ... }
//       Object.keys(layers).forEach(function (cap) { map.addLayer(layers[cap]); });
//       window.setHatchCapability(layers, selectedCapability);
//   });
//
//   // whenever selectedCapability changes elsewhere in the interface:
//   window.setHatchCapability(layers, selectedCapability);

(function () {
    // manifest.json (written by utils.hatch_raster.render_capability_hatch_png
    // via generate_experiment_shapefiles.py) looks like:
    //   {
    //     "crs": "EPSG:4326",
    //     "capabilities": {
    //       "care":            {"path": ".../hatch/care.png",            "extent": [minx,miny,maxx,maxy], ...},
    //       "nutrition":       {"path": ".../hatch/nutrition.png",       "extent": [...], ...},
    //       "restorativeness": {"path": ".../hatch/restorativeness.png", "extent": [...], ...}
    //     }
    //   }
    // The rasters are in the same CRS as every other layer in the gpkg
    // (EPSG:4326) -- a plain image version of zz_capability_grid, not a
    // separately reprojected artifact. `projection` is read from the
    // manifest rather than hardcoded, so ol.source.ImageStatic reprojects it
    // on the fly to match the map view, the same way OpenLayers already
    // handles every 4326 vector layer next to a 3857 basemap.
    //
    // Loading the manifest: the interface runs over file://, where fetch()
    // of local JSON is blocked by the browser -- so the primary path is the
    // script-tag global set by hatch/manifest.js (include it in index.html
    // BEFORE this call; same pattern as every other data file in the
    // interface). fetch(manifestUrl) remains as a fallback for http(s)
    // deployments that skip the script tag.
    window.makeHatchGridImageLayers = function (manifestUrl, pngBaseUrl) {
        var manifestPromise;
        if (window.HATCH_MANIFEST) {
            manifestPromise = Promise.resolve(window.HATCH_MANIFEST);
        } else {
            manifestPromise = fetch(manifestUrl).then(function (resp) {
                if (!resp.ok) throw new Error('Failed to load ' + manifestUrl + ': ' + resp.status);
                return resp.json();
            });
        }
        return manifestPromise
            .then(function (manifest) {
                var layers = {};
                Object.keys(manifest.capabilities).forEach(function (capability) {
                    var entry = manifest.capabilities[capability];
                    var fileName = entry.path.split('/').pop();
                    var url = (pngBaseUrl || '') + fileName;

                    layers[capability] = new ol.layer.Image({
                        source: new ol.source.ImageStatic({
                            url: url,
                            imageExtent: entry.extent,
                            projection: manifest.crs || 'EPSG:4326'
                        }),
                        visible: false
                    });
                });
                return layers;
            });
    };

    // Shows only the layer for `capability`, hides the other two.
    window.setHatchCapability = function (hatchLayers, capability) {
        Object.keys(hatchLayers).forEach(function (cap) {
            hatchLayers[cap].setVisible(cap === capability);
        });
    };
})();
