var size = 0;
var placement = 'point';

// Maps a POI power value in [0, 1] to a continuous fill-color scale.
// qgis2web.js keeps feature.get('power') synchronized with the current mode:
// capability power from cp[capability] when "Tutti" is selected, service power
// from sp[service] when a single service is selected.
var poiPowerColorStops = [
    [247, 233, 168],
    [230, 188, 112],
    [205, 132, 86],
    [164, 75, 72],
    [110, 24, 52]
];

function getPoiColorByPower(powerValue) {
    powerValue = Number(powerValue);
    if (!Number.isFinite(powerValue)) {
        powerValue = 0;
    }

    var normalizedValue = Math.max(0, Math.min(1, powerValue));
    var scaledValue = normalizedValue * (poiPowerColorStops.length - 1);
    var lowerIndex = Math.floor(scaledValue);
    var upperIndex = Math.min(lowerIndex + 1, poiPowerColorStops.length - 1);
    var fraction = scaledValue - lowerIndex;
    var lowerColor = poiPowerColorStops[lowerIndex];
    var upperColor = poiPowerColorStops[upperIndex];
    var color = lowerColor.map(function(channel, index) {
        return Math.round(channel + (upperColor[index] - channel) * fraction);
    });

    return 'rgba(' + color.join(',') + ',1.0)';
}

var poiTypeColorPalette = [
    'rgba(31,119,180,1.0)',
    'rgba(44,160,44,1.0)',
    'rgba(214,39,40,1.0)',
    'rgba(148,103,189,1.0)',
    'rgba(140,86,75,1.0)',
    'rgba(227,119,194,1.0)',
    'rgba(127,127,127,1.0)',
    'rgba(188,189,34,1.0)',
    'rgba(23,190,207,1.0)',
    'rgba(255,127,14,1.0)'
];

function getPoiColorByType(typeValue) {
    var typeText = String(typeValue || 'unknown');
    var hash = 0;

    for (var i = 0; i < typeText.length; i++) {
        hash = ((hash << 5) - hash) + typeText.charCodeAt(i);
        hash |= 0;
    }

    return poiTypeColorPalette[Math.abs(hash) % poiTypeColorPalette.length];
}

var style_pois_used2pois_used_1 = function(feature, resolution){
    if (!selectedPoiIds || !selectedPoiIds.has(String(feature.get('id')))) {
        return [];
    }

    var poiPower = feature.get('power');
    var isHoveredPoi = hoveredPoiId === String(feature.get('id'));
    var poiFillColor = getPoiColorByPower(poiPower);
    var poiRadius = isHoveredPoi ? 6.5 : 5.5;
    var poiStrokeColor = isHoveredPoi ? 'rgba(80, 20, 35, 1.0)' : 'rgba(35,35,35,1.0)';

    var context = {
        feature: feature,
        variables: {}
    };

    if (selectedService && colorPoisByType) {
        poiFillColor = getPoiColorByType(getPoiTypeForColor(feature));
        poiRadius = 7;
    }

    if (hoveredService) {
    if (poiHasService(feature, hoveredService)) {
        // POI evidenziati
        poiFillColor = 'rgba(0,128,0,1.0)';
        poiRadius = 7;
    } else {
        // POI restanti
        var dimmedColor = ol.color.asArray(poiFillColor).slice();
        dimmedColor[3] = 0.5;

        poiFillColor = dimmedColor;
        poiStrokeColor = 'rgba(35,35,35,0.2)';
    }
}
    
    var labelText = ""; 
    var value = feature.get("");
    var labelFont = "10px, sans-serif";
    var labelFill = "#000000";
    var bufferColor = "";
    var bufferWidth = 0;
    var textAlign = "left";
    var offsetX = 0;
    var offsetY = 0;
    var placement = 'point';
    if ("" !== null) {
        labelText = String("");
    }
    var style = [ new ol.style.Style({
        image: new ol.style.Circle({radius: poiRadius + size,
            displacement: [0, 0], stroke: new ol.style.Stroke({color: poiStrokeColor, lineDash: null, lineCap: 'butt', lineJoin: 'miter', width: 0.6}), fill: new ol.style.Fill({color: poiFillColor})}),
        text: createTextStyle(feature, resolution, labelText, labelFont,
                              labelFill, placement, bufferColor,
                              bufferWidth)
    })];

    return style;
};
