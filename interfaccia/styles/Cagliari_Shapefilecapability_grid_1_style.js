var style_Cagliari_Shapefilecapability_grid_1_cache = [
    [new ol.style.Style({
        stroke: new ol.style.Stroke({color: 'rgba(173, 173, 173, 0.55)', lineDash: null, lineCap: 'butt', lineJoin: 'miter', width: 0.988}),
        fill: new ol.style.Fill({color: 'rgba(68,1,84,0)'})
    })],
    [new ol.style.Style({
        stroke: new ol.style.Stroke({color: 'rgba(173, 173, 173, 0.55)', lineDash: null, lineCap: 'butt', lineJoin: 'miter', width: 0.988}),
        fill: new ol.style.Fill({color: 'rgba(68,1,84,0.35)'})
    })],
    [new ol.style.Style({
        stroke: new ol.style.Stroke({color: 'rgba(173, 173, 173, 0.55)', lineDash: null, lineCap: 'butt', lineJoin: 'miter', width: 0.988}),
        fill: new ol.style.Fill({color: 'rgba(59,82,139,0.35)'})
    })],
    [new ol.style.Style({
        stroke: new ol.style.Stroke({color: 'rgba(173, 173, 173, 0.55)', lineDash: null, lineCap: 'butt', lineJoin: 'miter', width: 0.988}),
        fill: new ol.style.Fill({color: 'rgba(33,144,141,0.35)'})
    })],
    [new ol.style.Style({
        stroke: new ol.style.Stroke({color: 'rgba(173, 173, 173, 0.55)', lineDash: null, lineCap: 'butt', lineJoin: 'miter', width: 0.988}),
        fill: new ol.style.Fill({color: 'rgba(93,201,99,0.35)'})
    })],
    [new ol.style.Style({
        stroke: new ol.style.Stroke({color: 'rgba(173, 173, 173, 0.55)', lineDash: null, lineCap: 'butt', lineJoin: 'miter', width: 0.988}),
        fill: new ol.style.Fill({color: 'rgba(253,231,37,0.35)'})
    })]
];

var whiteHexStyle = [new ol.style.Style({
    stroke: new ol.style.Stroke({
        color: 'rgba(173, 173, 173, 0)',
        lineDash: null,
        lineCap: 'butt',
        lineJoin: 'miter',
        width: 0.988
    }),
    fill: new ol.style.Fill({
        color: 'rgba(255,255,255,0.75)'
    }),
})];

var style_Cagliari_Shapefilecapability_grid_1 = function(feature, resolution) {
    if(selectedHexId && feature.get('hex_id') !== selectedHexId) {
        return [];
    }


    var value = Number(feature.get(selectedCapability));

    if (value === 0) {
        return style_Cagliari_Shapefilecapability_grid_1_cache[0];
    } else if (value > 0.000 && value < 0.2000) {
        return style_Cagliari_Shapefilecapability_grid_1_cache[1];
    } else if (value >= 0.2000 && value < 0.4000) {
        return style_Cagliari_Shapefilecapability_grid_1_cache[2];
    } else if (value >= 0.4000 && value < 0.6000) {
        return style_Cagliari_Shapefilecapability_grid_1_cache[3];
    } else if (value >= 0.6000 && value < 0.8000) {
        return style_Cagliari_Shapefilecapability_grid_1_cache[4];
    } else if (value >= 0.8000 && value <= 1.0000) {
        return style_Cagliari_Shapefilecapability_grid_1_cache[5];
    }
};
