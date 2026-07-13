var wms_layers = [];


var lyr_CartoDBPositron_0 = new ol.layer.Tile({
    'title': 'CartoDB Positron',
    'opacity': 1.000000,


    source: new ol.source.XYZ({
        attributions: ' ',
        url: 'https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png'
    })
});
var format_Cagliari_Shapefilecapability_grid_1 = new ol.format.GeoJSON();
var format_pois_used2pois_used_1 = new ol.format.GeoJSON();
var features_Cagliari_Shapefilecapability_grid_1 = format_Cagliari_Shapefilecapability_grid_1.readFeatures(json_Cagliari_Shapefilecapability_grid_1,
    { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' });
var features_pois_used2pois_used_1 = format_pois_used2pois_used_1.readFeatures(json_pois_used2pois_used_1,
    { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' });
var jsonSource_Cagliari_Shapefilecapability_grid_1 = new ol.source.Vector({
    attributions: ' ',
});
var jsonSource_pois_used2pois_used_1 = new ol.source.Vector({
    attributions: ' ',
});
jsonSource_Cagliari_Shapefilecapability_grid_1.addFeatures(features_Cagliari_Shapefilecapability_grid_1);
jsonSource_pois_used2pois_used_1.addFeatures(features_pois_used2pois_used_1);
var lyr_Cagliari_Shapefilecapability_grid_1 = new ol.layer.Vector({
    declutter: false,
    source: jsonSource_Cagliari_Shapefilecapability_grid_1,
    style: style_Cagliari_Shapefilecapability_grid_1,
    popuplayertitle: 'Cagliari_Shapefile — capability_grid',
    interactive: true,
    title: 'Cagliari_Shapefile — capability_grid<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_0.png" /> 0.1687 - 0.3070<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_1.png" /> 0.3070 - 0.4453<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_2.png" /> 0.4453 - 0.5836<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_3.png" /> 0.5836 - 0.7218<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_4.png" /> 0.7218 - 0.8601<br />' });


var lyr_pois_used2pois_used_1 = new ol.layer.Vector({
    declutter: false,
    source: jsonSource_pois_used2pois_used_1,
    style: style_pois_used2pois_used_1,
    popuplayertitle: 'pois_used 2 — pois_used',
    interactive: true,
    title: '<img src="styles/legend/pois_used2pois_used_1.png" /> pois_used 2 — pois_used'
});
lyr_CartoDBPositron_0.setVisible(true); lyr_Cagliari_Shapefilecapability_grid_1.setVisible(true); lyr_pois_used2pois_used_1.setVisible(false);
var layersList = [lyr_CartoDBPositron_0, lyr_Cagliari_Shapefilecapability_grid_1, lyr_pois_used2pois_used_1];
lyr_Cagliari_Shapefilecapability_grid_1.set('fieldAliases', { 'fid': 'fid', 'hex_id': 'hex_id', 'node_id': 'node_id', 'grid_mean_restorativeness': 'grid_mean_restorativeness', 'grid_mean_nutrition': 'grid_mean_nutrition', 'grid_mean_care': 'grid_mean_care', 'has_data': 'has_data', 'cell_size_m': 'cell_size_m', 'hex_radius_m': 'hex_radius_m', 'hex_width_m': 'hex_width_m', 'hex_height_m': 'hex_height_m', });
lyr_Cagliari_Shapefilecapability_grid_1.set('fieldImages', { 'fid': '', 'hex_id': '', 'node_id': '', 'grid_mean_restorativeness': '', 'grid_mean_nutrition': '', 'grid_mean_care': '', 'has_data': '', 'cell_size_m': '', 'hex_radius_m': '', 'hex_width_m': '', 'hex_height_m': '', });
lyr_Cagliari_Shapefilecapability_grid_1.set('fieldLabels', { 'fid': 'header label - visible with data', 'hex_id': 'header label - visible with data', 'node_id': 'header label - visible with data', 'grid_mean_restorativeness': 'header label - visible with data', 'grid_mean_nutrition': 'header label - visible with data', 'grid_mean_care': 'header label - visible with data', 'has_data': 'header label - visible with data', 'cell_size_m': 'header label - visible with data', 'hex_radius_m': 'header label - visible with data', 'hex_width_m': 'header label - visible with data', 'hex_height_m': 'header label - visible with data', });
lyr_Cagliari_Shapefilecapability_grid_1.on('precompose', function (evt) {
    evt.context.globalCompositeOperation = 'normal';
});
lyr_pois_used2pois_used_1.set('fieldAliases', { 'fid': 'fid', 'id': 'id', 'source_key': 'source_key', 'lon': 'lon', 'lat': 'lat', 'angular_coords': 'angular_coords', 'poi_types': 'poi_types', 'svc_map': 'svc_map', });
lyr_pois_used2pois_used_1.set('fieldImages', { 'fid': '', 'id': '', 'source_key': '', 'lon': '', 'lat': '', 'angular_coords': '', 'poi_types': '', 'svc_map': '', });
lyr_pois_used2pois_used_1.set('fieldLabels', { 'fid': 'hidden field', 'id': 'hidden field', 'source_key': 'hidden field', 'lon': 'hidden field', 'lat': 'hidden field', 'angular_coords': 'hidden field', 'poi_types': 'hidden field', 'svc_map': 'hidden field','power': 'hidden field', 'capabilityPower': 'hidden field', 'servicePowers': 'hidden field' });
lyr_pois_used2pois_used_1.on('precompose', function (evt) {
    evt.context.globalCompositeOperation = 'normal';
});
