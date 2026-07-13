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
var features_Cagliari_Shapefilecapability_grid_1 = format_Cagliari_Shapefilecapability_grid_1.readFeatures(json_Cagliari_Shapefilecapability_grid_1, 
            {dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857'});
var jsonSource_Cagliari_Shapefilecapability_grid_1 = new ol.source.Vector({
    attributions: ' ',
});
jsonSource_Cagliari_Shapefilecapability_grid_1.addFeatures(features_Cagliari_Shapefilecapability_grid_1);
var lyr_Cagliari_Shapefilecapability_grid_1 = new ol.layer.Vector({
                declutter: false,
                source:jsonSource_Cagliari_Shapefilecapability_grid_1, 
                style: style_Cagliari_Shapefilecapability_grid_1,
                popuplayertitle: 'Cagliari_Shapefile — capability_grid',
                interactive: true,
    title: 'Cagliari_Shapefile — capability_grid<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_0.png" /> 0.1687 - 0.3070<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_1.png" /> 0.3070 - 0.4453<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_2.png" /> 0.4453 - 0.5836<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_3.png" /> 0.5836 - 0.7218<br />\
    <img src="styles/legend/Cagliari_Shapefilecapability_grid_1_4.png" /> 0.7218 - 0.8601<br />' });

lyr_CartoDBPositron_0.setVisible(true);lyr_Cagliari_Shapefilecapability_grid_1.setVisible(true);
var layersList = [lyr_CartoDBPositron_0,lyr_Cagliari_Shapefilecapability_grid_1];
lyr_Cagliaricapability_grid_1.set('fieldAliases', {'fid': 'fid', 'hex_id': 'hex_id', 'node_id': 'node_id', 'grid_mean_restorativeness': 'grid_mean_restorativeness', 'grid_mean_nutrition': 'grid_mean_nutrition', 'grid_mean_care': 'grid_mean_care', 'has_data': 'has_data', 'cell_size_m': 'cell_size_m', 'hex_radius_m': 'hex_radius_m', 'hex_width_m': 'hex_width_m', 'hex_height_m': 'hex_height_m', });
lyr_Cagliaricapability_grid_1.set('fieldImages', {'fid': '', 'hex_id': '', 'node_id': '', 'grid_mean_restorativeness': '', 'grid_mean_nutrition': '', 'grid_mean_care': '', 'has_data': '', 'cell_size_m': '', 'hex_radius_m': '', 'hex_width_m': '', 'hex_height_m': '', });
lyr_Cagliaricapability_grid_1.set('fieldLabels', {'fid': 'header label - visible with data', 'hex_id': 'header label - visible with data', 'node_id': 'header label - visible with data', 'grid_mean_restorativeness': 'header label - visible with data', 'grid_mean_nutrition': 'header label - visible with data', 'grid_mean_care': 'header label - visible with data', 'has_data': 'header label - visible with data', 'cell_size_m': 'header label - visible with data', 'hex_radius_m': 'header label - visible with data', 'hex_width_m': 'header label - visible with data', 'hex_height_m': 'header label - visible with data', });
lyr_Cagliari_Shapefilecapability_grid_1.on('precompose', function(evt) {
    evt.context.globalCompositeOperation = 'normal';
});