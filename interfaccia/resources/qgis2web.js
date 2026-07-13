var selectedHexId = null;
var selectedHexFeature = null;
var selectedPoiIds = null;
var selectedPoiPowersById = null;
var hoveredPoiId = null;
var hexPoiRecordsByHexId = {};
var hexPoiLoadPromisesByHexId = {};
var hexPoiScriptNodesByHexId = {};
var hexPoiLoadErrorsByHexId = {};
var poiFeatureById = null;
var isPoiFilterHooked = false; // Traccia se abbiamo già agganciato lo stile

var poiAddressCache = {};
var hoveredTooltipPoiId = null;

function reverseGeocodePoi(feature) {
    var poiId = String(feature.get('id'));

    if (poiAddressCache[poiId]) {
        return Promise.resolve(poiAddressCache[poiId]);
    }

    var lat = feature.get('lat');
    var lon = feature.get('lon');

    var url = 'https://nominatim.openstreetmap.org/reverse?format=jsonv2&addressdetails=1&lat='
        + encodeURIComponent(lat)
        + '&lon='
        + encodeURIComponent(lon);

    return fetch(url)
        .then(function(response) {
            if (!response.ok) {
                throw new Error('Reverse geocoding non disponibile.');
            }

            return response.json();
        })
        .then(function(data) {
            var address = data.address || {};

            var info = {
                via: address.road || address.pedestrian || address.footway || '',
                citta: address.city || address.town || address.village || address.municipality || '',
                cap: address.postcode || ''
            };

            poiAddressCache[poiId] = info;
            return info;
        });
}

function createPoiAddressTooltipHtml(info) {
    return createPoiAddressHtml(info, 'tooltip-address-row');
}

function createPoiAddressHtml(info, rowId) {
    return '<tr id="' + escapeHtml(rowId || 'poi-address-row') + '"><td colspan="2">' +
        '<strong>Address</strong><br />' + escapeHtml(info.via ? info.via + ', ' : '') +
        escapeHtml(info.citta || '') +
        escapeHtml(info.cap ? ' ('+info.cap+') ' : '') +
        '</td></tr>';
}

function createPoiAddressErrorHtml(rowId) {
    return '<tr id="' + escapeHtml(rowId || 'poi-address-row') + '">' +
        '<td colspan="2"><strong>Address</strong><br />Not available</td></tr>';
}

function createPoiAddressLoadingHtml(poiId) {
    return '<tr id="tooltip-address-row-' + escapeHtml(poiId) + '">' +
        '<td colspan="2"><strong>Address</strong><br />Loading...</td></tr>';
}

// I power dei POI non sono piu' casuali: arrivano dai file
// resources/hex_pois/<hex_id>.js. Ogni file chiama __onHexPois(hexId, records)
// e contiene solo i POI dell'esagono selezionato, quindi il caricamento resta
// leggero anche con molti esagoni.
window.__onHexPois = function(hexId, records) {
    hexPoiRecordsByHexId[hexId] = Array.isArray(records) ? records : [];
};

window.__onHexPoisManifest = function(manifest) {
    window.hexPoisManifest = manifest;
};

var map = new ol.Map({
    target: 'map',
    renderer: 'canvas',
    layers: layersList,
    view: new ol.View({
         maxZoom: 18, minZoom: 12.5
    })
});

// Il layer POI non deve mai partire con l'intero dataset in mappa:
// viene popolato solo dopo la selezione di un esagono.
setPoiLayerFeatures([]);

//initial view - epsg:3857 coordinates if not "Match project CRS"
var initialExtent = [1005903.508842, 4745772.051660, 1027302.249761, 4763729.781966];
function refreshInitialView() {
    map.updateSize();
    map.getView().fit(initialExtent, {
        size: map.getSize(),
        nearest: true
    });
    map.renderSync();
}
refreshInitialView();
requestAnimationFrame(refreshInitialView);
window.addEventListener('load', refreshInitialView);

// 1. Configurazione della ScaleBar con lo stile identico all'esempio OpenLayers
var scaleBarControl = new ol.control.ScaleLine({
    units: 'metric',       // Mantiene il sistema metrico (metri/km)
    bar: false,             // Forza la modalità "ScaleBar" a blocchi alternati
    //steps: 4,              // Numero di segmenti della barra
   // text: true,            // Mostra il testo sopra la barra
    minWidth: 140          // Larghezza minima per una corretta visualizzazione
});

// 2. Aggiunta del controllo alla mappa esportata da QGIS
map.addControl(scaleBarControl);


// Aggiunta della visualizzazione mappa tramite toggle
var toggle = document.getElementById('toggle-results');
var didascalia = document.getElementById('didascalia-mappa');

function updateDidascaliaVisibility() {
    if (!didascalia || !toggle) {
        return;
    }

    didascalia.style.display = !selectedHexId && toggle.checked ? 'flex' : 'none';
}

toggle.addEventListener('change', function () {
    alpha = 0.35

    var opacita = toggle.checked ? alpha : 0
    
    updateDidascaliaVisibility();

    style_Cagliari_Shapefilecapability_grid_1_cache.forEach(function (styleArray, index){
        if (index === 0){
            return;
        }

        var fill = styleArray[0].getFill();
        var color = fill.getColor();

        fill.setColor(
            color.replace(
                /rgba\(([^,]+),([^,]+),([^,]+),[^)]+\)/,
                'rgba($1,$2,$3,' + opacita + ')'
            )
        );
    });

    lyr_Cagliari_Shapefilecapability_grid_1.changed();
});

var selectedCapability = "grid_mean_care";
lyr_Cagliari_Shapefilecapability_grid_1.changed();

//full zooms only
map.getView().setProperties({constrainResolution: true});

//change cursor
function pointerOnFeature(evt) {
    if (evt.dragging) {
        return;
    }

    var nextHoveredPoiId = null;
    if (typeof lyr_pois_used2pois_used_1 !== 'undefined' && lyr_pois_used2pois_used_1) {
        var hoveredPoiFeature = map.forEachFeatureAtPixel(evt.pixel, function(feature, layer) {
            if (layer === lyr_pois_used2pois_used_1) {
                return feature;
            }
        }, {
            hitTolerance: 4
        });

        if (hoveredPoiFeature) {
            nextHoveredPoiId = String(hoveredPoiFeature.get('id'));
        }
    }

    if (hoveredPoiId !== nextHoveredPoiId) {
        hoveredPoiId = nextHoveredPoiId;
        if (typeof lyr_pois_used2pois_used_1 !== 'undefined' && lyr_pois_used2pois_used_1) {
            lyr_pois_used2pois_used_1.changed();
        }
    }

    var hasFeature = map.hasFeatureAtPixel(evt.pixel, {
        layerFilter: function(layer) {
            return layer && (layer.get("interactive"));
        },
        hitTolerance: 4
    });
    map.getViewport().style.cursor = hasFeature ? "pointer" : "";
}
map.on('pointermove', pointerOnFeature);
function styleCursorMove() {
    map.on('pointerdrag', function() {
        map.getViewport().style.cursor = "move";
    });
    map.on('pointerup', function() {
        map.getViewport().style.cursor = "default";
    });
}
styleCursorMove();

////small screen definition
    var hasTouchScreen = map.getViewport().classList.contains('ol-touch');
    var isSmallScreen = window.innerWidth < 650;

////controls container

    //top left container
    var topLeftContainer = new ol.control.Control({
        element: (() => {
            var topLeftContainer = document.createElement('div');
            topLeftContainer.id = 'top-left-container';
            return topLeftContainer;
        })(),
    });
    map.addControl(topLeftContainer)

    //bottom left container
    var bottomLeftContainer = new ol.control.Control({
        element: (() => {
            var bottomLeftContainer = document.createElement('div');
            bottomLeftContainer.id = 'bottom-left-container';
            return bottomLeftContainer;
        })(),
    });
    map.addControl(bottomLeftContainer)
  
    //top right container
    var topRightContainer = new ol.control.Control({
        element: (() => {
            var topRightContainer = document.createElement('div');
            topRightContainer.id = 'top-right-container';
            return topRightContainer;
        })(),
    });
    map.addControl(topRightContainer)

    //bottom right container
    var bottomRightContainer = new ol.control.Control({
        element: (() => {
            var bottomRightContainer = document.createElement('div');
            bottomRightContainer.id = 'bottom-right-container';
            return bottomRightContainer;
        })(),
    });
    map.addControl(bottomRightContainer)

//popup
var container = document.getElementById('popup');
var content = document.getElementById('popup-content');
var closer = document.getElementById('popup-closer');
var resetLayerViewButton = document.getElementById('reset-layer-view');
var resultsToggleGroup = document.getElementById('results-toggle-group');
var serviceMenuGroup = document.getElementById('service-menu-group');
var poiTypeColorToggleGroup = document.getElementById('poi-type-color-toggle-group');
var poiTypeColorToggle = document.getElementById('toggle-poi-type-colors');
var poiTypeLegend = document.getElementById('poi-type-legend');
var poiTypeLegendRows = document.getElementById('poi-type-legend-rows');
var capabilityLegendRows = document.querySelectorAll('#capability-legend .legend-row');
var sketch;

function stopMediaInPopup() {
    var mediaElements = container.querySelectorAll('audio, video');
    mediaElements.forEach(function(media) {
        media.pause();
        media.currentTime = 0;
    });
}

function updateLayerModeControls() {
    var secondLayerActive = !!selectedHexId;

    if (resultsToggleGroup) {
        resultsToggleGroup.style.display = secondLayerActive ? 'none' : 'block';
    }

    if (resetLayerViewButton) {
        resetLayerViewButton.style.display = secondLayerActive ? 'block' : 'none';
    }

    if (serviceMenuGroup) {
        serviceMenuGroup.style.display = secondLayerActive ? 'block' : 'none';
    }

    if (poiTypeColorToggleGroup) {
        poiTypeColorToggleGroup.style.display = secondLayerActive && selectedService ? 'block' : 'none';
    }

    capabilityLegendRows.forEach(function(row) {
        row.style.display = secondLayerActive ? 'none' : 'flex';
    });

    updatePoiTypeLegend();
    updateDidascaliaVisibility();
}

function resetSelectedHexState() {
    selectedHexId = null;
    selectedHexFeature = null;
    selectedPoiIds = null;
    selectedPoiPowersById = null;
    hoveredPoiId = null;

    updateLayerModeControls();

    if (typeof lyr_Cagliari_Shapefilecapability_grid_1 !== 'undefined') {
        lyr_Cagliari_Shapefilecapability_grid_1.changed();
    }

    if (typeof lyr_pois_used2pois_used_1 !== 'undefined') {
        lyr_pois_used2pois_used_1.setVisible(false);
        setPoiLayerFeatures([]);
        lyr_pois_used2pois_used_1.changed();
    }

    popupContent = '';
    popupCoord = null;
    container.style.display = 'none';
    closer.blur();
    stopMediaInPopup();
}

closer.onclick = function() {
    container.style.display = 'none';
    closer.blur();
    stopMediaInPopup();
    return false;
};

if (resetLayerViewButton) {
    resetLayerViewButton.addEventListener('click', function() {
        resetSelectedHexState();
    });
}

updateLayerModeControls();

var overlayPopup = new ol.Overlay({
    element: container,
	autoPan: true
});
map.addOverlay(overlayPopup)
    
    
var NO_POPUP = 0
var ALL_FIELDS = 1

/**
 * Returns either NO_POPUP, ALL_FIELDS or the name of a single field to use for
 * a given layer
 * @param layerList {Array} List of ol.Layer instances
 * @param layer {ol.Layer} Layer to find field info about
 */
function getPopupFields(layerList, layer) {
    // Determine the index that the layer will have in the popupLayers Array,
    // if the layersList contains more items than popupLayers then we need to
    // adjust the index to take into account the base maps group
    var idx = layersList.indexOf(layer) - (layersList.length - popupLayers.length);
    return popupLayers[idx];
}

//highligth collection
var collection = new ol.Collection();
var featureOverlay = new ol.layer.Vector({
    map: map,
    source: new ol.source.Vector({
        features: collection,
        useSpatialIndex: false // optional, might improve performance
    }),
    style: [new ol.style.Style({
        stroke: new ol.style.Stroke({
            color: '#f00',
            width: 1
        }),
        fill: new ol.style.Fill({
            color: 'rgba(255,0,0,0.1)'
        }),
    })],
    updateWhileAnimating: true, // optional, for instant visual feedback
    updateWhileInteracting: true // optional, for instant visual feedback
});


var servicesByCapability = {};
var selectedService = null;
var hoveredService = null;
var isHoveringServiceButton = false;
var colorPoisByType = true;
var serviceLabels = createLabelDictionary({});
var poiTypeLabels = createLabelDictionary({});

function normalizeLabelKey(value) {
    var normalized = String(value == null ? '' : value).trim().toLowerCase();

    // Permette al dizionario di riconoscere anche varianti come
    // "food-access", "Food access" e "food_access".
    if (typeof normalized.normalize === 'function') {
        normalized = normalized.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    }

    return normalized.replace(/[^a-z0-9]+/g, '');
}

function createLabelDictionary(data, sourceName) {
    var dictionary = {
        exact: {},
        normalized: {}
    };

    if (!data || Array.isArray(data) || typeof data !== 'object') {
        if (sourceName) {
            console.warn(sourceName + ': il JSON deve contenere un oggetto chiave/etichetta.');
        }
        return dictionary;
    }

    Object.keys(data).forEach(function(key) {
        var value = data[key];
        var label = typeof value === 'string' ? value.trim() : '';

        if (!key.trim() || !label) {
            if (sourceName) {
                console.warn(sourceName + ': voce ignorata per la chiave "' + key + '".');
            }
            return;
        }

        dictionary.exact[key] = label;

        var normalizedKey = normalizeLabelKey(key);
        if (normalizedKey && !dictionary.normalized[normalizedKey]) {
            dictionary.normalized[normalizedKey] = label;
        }
    });

    return dictionary;
}

function loadLabelDictionary(url) {
    return fetch(url)
        .then(function(response) {
            if (!response.ok) {
                throw new Error('HTTP ' + response.status);
            }
            return response.json();
        })
        .then(function(data) {
            return createLabelDictionary(data, url);
        })
        .catch(function(error) {
            // Un dizionario assente o non valido non blocca la mappa: le chiavi
            // vengono trasformate in etichette leggibili come fallback.
            console.warn('Etichette non caricate da ' + url + ':', error);
            return createLabelDictionary({});
        });
}

function loadInterfaceLabels() {
    return Promise.all([
        loadLabelDictionary('resources/service_labels.json'),
        loadLabelDictionary('resources/poi_type_labels.json')
    ]).then(function(dictionaries) {
        serviceLabels = dictionaries[0];
        poiTypeLabels = dictionaries[1];
    });
}

function humanizeLabelKey(value) {
    var text = String(value == null ? '' : value)
        .replace(/[_-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();

    return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
}

function getDictionaryLabel(dictionary, key) {
    var rawKey = String(key == null ? '' : key);
    return dictionary.exact[rawKey] ||
        dictionary.normalized[normalizeLabelKey(rawKey)] ||
        humanizeLabelKey(rawKey);
}

function getServiceLabel(service) {
    return getDictionaryLabel(serviceLabels, service);
}

function getPoiTypeLabel(poiType) {
    return getDictionaryLabel(poiTypeLabels, poiType);
}

var capabilityCsvKeyByField = {
    grid_mean_care: 'care',
    grid_mean_nutrition: 'nutrition',
    grid_mean_restorativeness: 'restorativeness'
};

function loadCapabilityServicesCsv() {
    return fetch('resources/capability.csv')
        .then(function(response) {
            if (!response.ok) {
                throw new Error('Impossibile caricare capability_new.csv');
            }
            return response.text();
        })
        .then(function(csvText) {
            servicesByCapability = parseCapabilityServicesCsv(csvText);
        });
}

function parseCapabilityServicesCsv(csvText) {
    var rows = csvText.trim().split(/\r?\n/);
    var result = {};

    rows.slice(1).forEach(function(row) {
        var firstCommaIndex = row.indexOf(',');
        var capability = row.slice(0, firstCommaIndex).trim();
        var servicesText = row.slice(firstCommaIndex + 1).trim();

        servicesText = servicesText
            .replace(/^"/, '')
            .replace(/"$/, '')
            .replace(/^\[/, '')
            .replace(/\]$/, '');

        var services = servicesText
            .split(',')
            .map(function(service) {
                return service.trim().replace(/^'/, '').replace(/'$/, '');
            })
            .filter(Boolean);

        result[capability] = services;
    });

    return result;
}

function updateServiceButtons() {
    var buttonsContainer = document.getElementById('service-buttons');
    if (!buttonsContainer) return;

    var csvCapabilityKey = capabilityCsvKeyByField[selectedCapability];
    var services = servicesByCapability[csvCapabilityKey] || [];

    buttonsContainer.innerHTML = '';

    selectedService = null;
    colorPoisByType = false;
    if (poiTypeColorToggle) {
        poiTypeColorToggle.checked = false;
    }

    function addServiceButton(service, label) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'service-button';
        button.dataset.service = service || '';
        button.textContent = label || getServiceLabel(service);

        if (service === selectedService) {
            button.classList.add('active');
        }

        button.addEventListener('click', function() {
            selectedService = service;

            buttonsContainer.querySelectorAll('.service-button').forEach(function(btn) {
                btn.classList.toggle('active', btn.dataset.service === (selectedService || ''));
            });

            if (!selectedService) {
                colorPoisByType = false;
                if (poiTypeColorToggle) {
                    poiTypeColorToggle.checked = false;
                }
            }

            updateVisiblePoiPowers();
            updateLayerModeControls();

            if (typeof lyr_pois_used2pois_used_1 !== 'undefined') {
                lyr_pois_used2pois_used_1.changed();
            }
        });

        button.addEventListener('mouseenter', function() {
            if (selectedService) {
                return;
            }


            hoveredService = service;
            isHoveringServiceButton = true;

            if (typeof lyr_pois_used2pois_used_1 !== 'undefined') {
                lyr_pois_used2pois_used_1.changed();
            }
        });

        button.addEventListener('mouseleave', function() {
            hoveredService = null;
            isHoveringServiceButton = false;

            if (typeof lyr_pois_used2pois_used_1 !== 'undefined') {
                lyr_pois_used2pois_used_1.changed();
            }
        });

        buttonsContainer.appendChild(button);
    }

    addServiceButton(null, 'All');

    services.forEach(function(service) {
        addServiceButton(service);
    });

    if (typeof lyr_pois_used2pois_used_1 !== 'undefined') {
        lyr_pois_used2pois_used_1.changed();
    }
}

document.getElementById('capability-select').addEventListener('change', function () {
    selectedCapability = this.value;
    lyr_Cagliari_Shapefilecapability_grid_1.changed();

    updateServiceButtons();
    updateVisiblePoiPowers();

    if (typeof lyr_pois_used2pois_used_1 !== 'undefined') {
        lyr_pois_used2pois_used_1.changed();
    }
});

Promise.all([
    loadCapabilityServicesCsv().catch(function(error) {
        console.error('Errore caricamento servizi capability:', error);
    }),
    loadInterfaceLabels()
]).then(function() {
    // I controlli vengono costruiti soltanto dopo aver letto sia i dati sia le
    // etichette, quindi basta sostituire i JSON per aggiornare l'interfaccia.
    updateServiceButtons();
    updatePoiTypeLegend();
});

if (poiTypeColorToggle) {
    colorPoisByType = poiTypeColorToggle.checked;
    poiTypeColorToggle.addEventListener('change', function() {
        colorPoisByType = this.checked;
        updatePoiTypeLegend();

        if (typeof lyr_pois_used2pois_used_1 !== 'undefined') {
            lyr_pois_used2pois_used_1.changed();
        }
    });
}

function poiHasSelectedService(feature) {
    if (!selectedService) {
        return true;
    }

    return poiHasService(feature, selectedService);
}

function poiHasServicePower(feature, service) {
    if (!service) {
        return false;
    }

    var servicePowers = feature.get('servicePowers') || {};
    return Number.isFinite(Number(servicePowers[service]));
}

function poiHasService(feature, service) {
    if (!service) {
        return false;
    }

    var svcMap = feature.get('svc_map') || {};

    return Object.keys(svcMap).some(function(poiType) {
        return (svcMap[poiType] || []).includes(service);
    });
}

function getPoiTypeForColor(feature) {
    var poiTypes = feature.get('poi_types') || [];
    var svcMap = feature.get('svc_map') || {};

    if (!Array.isArray(poiTypes)) {
        return String(poiTypes || '');
    }

    if (selectedService) {
        for (var i = 0; i < poiTypes.length; i++) {
            var type = poiTypes[i];
            if ((svcMap[type] || []).includes(selectedService)) {
                return type;
            }
        }
    }

    return poiTypes[0] || '';
}

function updatePoiTypeLegend() {
    if (!poiTypeLegend || !poiTypeLegendRows) {
        return;
    }

    poiTypeLegendRows.innerHTML = '';

    if (!selectedHexId || !selectedService || !colorPoisByType || !selectedPoiIds) {
        poiTypeLegend.style.display = 'none';
        return;
    }

    var poiById = getPoiFeatureById();
    var visibleTypes = new Set();

    selectedPoiIds.forEach(function(poiId) {
        var feature = poiById[String(poiId)];

        if (!feature || !poiHasService(feature, selectedService) ||
            !poiHasServicePower(feature, selectedService)) {
            return;
        }

        var poiType = getPoiTypeForColor(feature);
        if (poiType) {
            visibleTypes.add(poiType);
        }
    });

    Array.from(visibleTypes).sort(function(firstType, secondType) {
        return getPoiTypeLabel(firstType).localeCompare(getPoiTypeLabel(secondType));
    }).forEach(function(poiType) {
        var row = document.createElement('div');
        row.className = 'poi-type-legend-row';

        var swatch = document.createElement('span');
        swatch.className = 'poi-type-legend-color';
        swatch.style.backgroundColor = getPoiColorByType(poiType);

        var label = document.createElement('span');
        label.textContent = getPoiTypeLabel(poiType);

        row.appendChild(swatch);
        row.appendChild(label);
        poiTypeLegendRows.appendChild(row);
    });

    poiTypeLegend.style.display = visibleTypes.size ? 'block' : 'none';
}

function getSelectedCapabilityKey() {
    return capabilityCsvKeyByField[selectedCapability] || '';
}

function getCapabilityPowerForRecord(record) {
    var capabilityKey = getSelectedCapabilityKey();
    var capabilityPowers = record && record.cp ? record.cp : {};
    var value = Number(capabilityPowers[capabilityKey]);

    return Number.isFinite(value) ? value : 0;
}

function getServicePowerForRecord(record, service) {
    var servicePowers = record && record.sp ? record.sp : {};
    var value = Number(servicePowers[service]);

    return Number.isFinite(value) ? value : null;
}

function updatePoiPowerForCurrentMode(feature) {
    if (!selectedPoiPowersById) {
        feature.set('capabilityPower', 0, true);
        feature.set('servicePowers', {}, true);
        feature.set('power', 0, true);
        return;
    }

    var poiId = String(feature.get('id'));
    var record = selectedPoiPowersById[poiId];
    var capabilityPower = record ? getCapabilityPowerForRecord(record) : 0;
    var servicePowers = record && record.sp ? record.sp : {};
    var servicePower = selectedService ? getServicePowerForRecord(record, selectedService) : null;

    // "power" resta il campo unico letto dallo stile:
    // - con "Tutti" usa cp[capability] (capability power);
    // - con un servizio usa sp[servizio] (service power).
    feature.set('capabilityPower', capabilityPower, true);
    feature.set('servicePowers', servicePowers, true);
    feature.set('power', selectedService ? (servicePower == null ? 0 : servicePower) : capabilityPower, true);
}

function updateVisiblePoiPowers() {
    if (!selectedPoiIds) {
        return;
    }

    var poiById = getPoiFeatureById();

    selectedPoiIds.forEach(function(poiId) {
        if (poiById[poiId]) {
            updatePoiPowerForCurrentMode(poiById[poiId]);
        }
    });
}

var doHighlight = false;
var doHover = false;

function createPopupField(currentFeature, currentFeatureKeys, layer) {
    var popupText = '';
    for (var i = 0; i < currentFeatureKeys.length; i++) {
        if (currentFeatureKeys[i] != 'geometry' && currentFeatureKeys[i] != 'layerObject' && currentFeatureKeys[i] != 'idO') {
            var popupField = '';
            if (layer.get('fieldLabels')[currentFeatureKeys[i]] == "hidden field") {
                continue;
            } else if (layer.get('fieldLabels')[currentFeatureKeys[i]] == "inline label - visible with data") {
                if (currentFeature.get(currentFeatureKeys[i]) == null) {
                    continue;
                }
            }
            if (layer.get('fieldLabels')[currentFeatureKeys[i]] == "inline label - always visible" ||
                layer.get('fieldLabels')[currentFeatureKeys[i]] == "inline label - visible with data") {
                popupField += '<th>' + layer.get('fieldAliases')[currentFeatureKeys[i]] + '</th><td>';
            } else {
                popupField += '<td colspan="2">';
            }
            if (layer.get('fieldLabels')[currentFeatureKeys[i]] == "header label - visible with data") {
                if (currentFeature.get(currentFeatureKeys[i]) == null) {
                    continue;
                }
            }
            if (layer.get('fieldLabels')[currentFeatureKeys[i]] == "header label - always visible" ||
                layer.get('fieldLabels')[currentFeatureKeys[i]] == "header label - visible with data") {
                popupField += '<strong>' + layer.get('fieldAliases')[currentFeatureKeys[i]] + '</strong><br />';
            }
            if (layer.get('fieldImages')[currentFeatureKeys[i]] != "ExternalResource") {
				popupField += (currentFeature.get(currentFeatureKeys[i]) != null ? autolinker.link(currentFeature.get(currentFeatureKeys[i]).toLocaleString()) + '</td>' : '');
			} else {
				var fieldValue = currentFeature.get(currentFeatureKeys[i]);
				if (/\.(gif|jpg|jpeg|tif|tiff|png|avif|webp|svg)$/i.test(fieldValue)) {
					popupField += (fieldValue != null ? '<img src="images/' + fieldValue.replace(/[\\\/:]/g, '_').trim() + '" /></td>' : '');
				} else if (/\.(mp4|webm|ogg|avi|mov|flv)$/i.test(fieldValue)) {
					popupField += (fieldValue != null ? '<video controls><source src="images/' + fieldValue.replace(/[\\\/:]/g, '_').trim() + '" type="video/mp4">Il tuo browser non supporta il tag video.</video></td>' : '');
				} else if (/\.(mp3|wav|ogg|aac|flac)$/i.test(fieldValue)) {
                    popupField += (fieldValue != null ? '<audio controls><source src="images/' + fieldValue.replace(/[\\\/:]/g, '_').trim() + '" type="audio/mpeg">Il tuo browser non supporta il tag audio.</audio></td>' : '');
                } else {
					popupField += (fieldValue != null ? autolinker.link(fieldValue.toLocaleString()) + '</td>' : '');
				}
			}
            popupText += '<tr>' + popupField + '</tr>';
        }
    }
    return popupText;
}

var highlight;
var autolinker = new Autolinker({truncate: {length: 30, location: 'smart'}});

function onPointerMove(evt) {
    if (!doHover && !doHighlight) {
        return;
    }
    var pixel = map.getEventPixel(evt.originalEvent);
    var coord = evt.coordinate;
    var currentFeature;
    var currentLayer;
    var currentFeatureKeys;
    var clusteredFeatures;
    var clusterLength;
    var popupText = '<ul>';

    // Collect all features and their layers at the pixel
    var featuresAndLayers = [];
    map.forEachFeatureAtPixel(pixel, function(feature, layer) {
        if (layer && feature instanceof ol.Feature && (layer.get("interactive") || layer.get("interactive") === undefined)) {
            featuresAndLayers.push({ feature, layer });
        }
    });

    // Iterate over the features and layers in reverse order
    for (var i = featuresAndLayers.length - 1; i >= 0; i--) {
        var feature = featuresAndLayers[i].feature;
        var layer = featuresAndLayers[i].layer;
        var doPopup = false;
        for (k in layer.get('fieldImages')) {
            if (layer.get('fieldImages')[k] != "Hidden") {
                doPopup = true;
            }
        }
        currentFeature = feature;
        currentLayer = layer;
        clusteredFeatures = feature.get("features");
        if (clusteredFeatures) {
            clusterLength = clusteredFeatures.length;
        }
        if (typeof clusteredFeatures !== "undefined") {
            if (doPopup) {
                for(var n=0; n<clusteredFeatures.length; n++) {
                    currentFeature = clusteredFeatures[n];
                    currentFeatureKeys = currentFeature.getKeys();
                    popupText += '<li><table>'
                    popupText += '<a>' + '<b>' + layer.get('popuplayertitle') + '</b>' + '</a>';
                    popupText += createPopupField(currentFeature, currentFeatureKeys, layer);
                    popupText += '</table></li>';    
                }
            }
        } else {
            currentFeatureKeys = currentFeature.getKeys();
            if (doPopup) {
                popupText += '<li><table>';
                popupText += '<a>' + '<b>' + layer.get('popuplayertitle') + '</b>' + '</a>';

                if (layer === lyr_pois_used2pois_used_1) {
                    var poiId = String(currentFeature.get('id'));
                    hoveredTooltipPoiId = poiId;

                    popupText += createPopupField(currentFeature, currentFeatureKeys, layer);

                    if (poiAddressCache[poiId]) {
                        popupText += createPoiAddressTooltipHtml(poiAddressCache[poiId]);
                    } else {
                        popupText += createPoiAddressLoadingHtml(poiId);

                        reverseGeocodePoi(currentFeature).then(function(info) {
                            if (hoveredTooltipPoiId !== poiId) {
                                return;
                            }

                            var row = document.getElementById('tooltip-address-row-' + poiId);
                            if (row) {
                                row.outerHTML = createPoiAddressTooltipHtml(info);
                            }
                        }).catch(function(error) {
                            console.error('Errore reverse geocoding POI:', error);
                            var row = document.getElementById('tooltip-address-row-' + poiId);
                            if (row) {
                                row.outerHTML = createPoiAddressErrorHtml('tooltip-address-row-' + poiId);
                            }
                        });
                    }
                } else {
                    popupText += createPopupField(currentFeature, currentFeatureKeys, layer);
                }

                popupText += '</table></li>';
            }
        }
    }

    if (popupText == '<ul>') {
        popupText = '';
    } else {
        popupText += '</ul>';
    }
    
	if (doHighlight) {
        if (currentFeature !== highlight) {
            if (highlight) {
                featureOverlay.getSource().removeFeature(highlight);
            }
            if (currentFeature) {
                var featureStyle
                if (typeof clusteredFeatures == "undefined") {
					var style = currentLayer.getStyle();
					var styleFunction = typeof style === 'function' ? style : function() { return style; };
					featureStyle = styleFunction(currentFeature)[0];
				} else {
					featureStyle = currentLayer.getStyle().toString();
				}

                if (currentFeature.getGeometry().getType() == 'Point' || currentFeature.getGeometry().getType() == 'MultiPoint') {
                    var radius
					if (typeof clusteredFeatures == "undefined") {
						radius = featureStyle.getImage().getRadius();
					} else {
						radius = parseFloat(featureStyle.split('radius')[1].split(' ')[1]) + clusterLength;
					}

                    highlightStyle = new ol.style.Style({
                        image: new ol.style.Circle({
                            fill: new ol.style.Fill({
                                color: "rgba(255, 255, 0, 1.00)"
                            }),
                            radius: radius
                        })
                    })
                } else if (currentFeature.getGeometry().getType() == 'LineString' || currentFeature.getGeometry().getType() == 'MultiLineString') {

                    var featureWidth = featureStyle.getStroke().getWidth();

                    highlightStyle = new ol.style.Style({
                        stroke: new ol.style.Stroke({
                            color: 'rgba(255, 255, 0, 1.00)',
                            lineDash: null,
                            width: featureWidth
                        })
                    });

                } else {
                    highlightStyle = new ol.style.Style({
                        fill: new ol.style.Fill({
                            color: 'rgba(255, 255, 0, 1.00)'
                        })
                    })
                }
                featureOverlay.getSource().addFeature(currentFeature);
                featureOverlay.setStyle(highlightStyle);
            }
            highlight = currentFeature;
        }
    }

    if (doHover) {
        if (popupText) {
			content.innerHTML = popupText;
            container.style.display = 'block';
            overlayPopup.setPosition(coord);
        } else {
            container.style.display = 'none';
            closer.blur();
        }
    }
};

map.on('pointermove', onPointerMove);

var popupContent = '';
var popupCoord = null;
var featuresPopupActive = false;

function loadHexPoiRecords(hexId) {
    if (hexPoiRecordsByHexId[hexId]) {
        return Promise.resolve(hexPoiRecordsByHexId[hexId]);
    }

    if (hexPoiLoadPromisesByHexId[hexId]) {
        return hexPoiLoadPromisesByHexId[hexId];
    }

    hexPoiLoadPromisesByHexId[hexId] = new Promise(function(resolve, reject) {
        var script = document.createElement('script');
        script.src = 'resources/hex_pois/' + encodeURIComponent(hexId) + '.js';
        script.async = true;

        script.onload = function() {
            resolve(hexPoiRecordsByHexId[hexId] || []);
        };

        script.onerror = function() {
            var error = new Error('Impossibile caricare resources/hex_pois/' + hexId + '.js');
            hexPoiLoadErrorsByHexId[hexId] = error;
            reject(error);
        };

        hexPoiScriptNodesByHexId[hexId] = script;
        document.head.appendChild(script);
    });

    return hexPoiLoadPromisesByHexId[hexId];
}

// Intercettazione sicura eseguita SOLO a richiesta avviata e se il layer esiste
function setupDynamicPoiFiltering() {
    if (isPoiFilterHooked || typeof lyr_pois_used2pois_used_1 === 'undefined' || !lyr_pois_used2pois_used_1) return;

    var originalStyleFunction = lyr_pois_used2pois_used_1.getStyle();

    lyr_pois_used2pois_used_1.setStyle(function(feature, resolution) {
        if (!selectedPoiIds) {
            return [];
        }
        
        var poiId = String(feature.get('id'));
        var matchesSelectedService = !selectedService
            || (poiHasService(feature, selectedService) && poiHasServicePower(feature, selectedService));
        var matchesHoveredService = !selectedService && isHoveringServiceButton && (!hoveredService
            || (poiHasService(feature, hoveredService) && poiHasServicePower(feature, hoveredService)));
        var matchesVisibleService = matchesSelectedService || matchesHoveredService;

        if (selectedPoiIds.has(poiId) && matchesVisibleService) {
            if (typeof originalStyleFunction === 'function') {
                return originalStyleFunction(feature, resolution);
            }
            return originalStyleFunction;
        } else {
            return [];
        }
    });

    isPoiFilterHooked = true;
}

function getPoiFeatureById() {
    if (!poiFeatureById) {
        poiFeatureById = {};
        if (typeof features_pois_used2pois_used_1 !== 'undefined' && features_pois_used2pois_used_1) {
            features_pois_used2pois_used_1.forEach(function(feature) {
                poiFeatureById[String(feature.get('id'))] = feature;
            });
        } else if (typeof jsonSource_pois_used2pois_used_1 !== 'undefined' && jsonSource_pois_used2pois_used_1) {
            jsonSource_pois_used2pois_used_1.getFeatures().forEach(function(feature) {
                poiFeatureById[String(feature.get('id'))] = feature;
            });
        }
    }
    return poiFeatureById;
}

function setPoiLayerFeatures(poiFeatures) {
    if (typeof jsonSource_pois_used2pois_used_1 === 'undefined' || !jsonSource_pois_used2pois_used_1) {
        return;
    }

    jsonSource_pois_used2pois_used_1.clear();
    if (poiFeatures.length > 0) {
        jsonSource_pois_used2pois_used_1.addFeatures(poiFeatures);
    }

    if (typeof lyr_pois_used2pois_used_1 !== 'undefined' && lyr_pois_used2pois_used_1) {
        lyr_pois_used2pois_used_1.setSource(jsonSource_pois_used2pois_used_1);
    }
}

function escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function createLinkedPoiHtml(hexId, poiFeatures, totalCount) {
    var html = '<ul><li><table>';
    html += '<a><b>Esagono selezionato</b></a>';
    html += '<tr><td colspan="2"><strong>hex_id</strong><br />' + escapeHtml(hexId) + '</td></tr>';
    html += '<tr><td colspan="2"><strong>POI collegati</strong><br />' + totalCount + '</td></tr>';

    if (poiFeatures.length === 0) {
        html += '<tr><td colspan="2">Nessun POI collegato trovato.</td></tr>';
    } else {
        var maxVisiblePois = 100;
        poiFeatures.slice(0, maxVisiblePois).forEach(function(poiFeature) {
            html += '<tr><td colspan="2">';
            html += '<strong>POI ' + escapeHtml(poiFeature.get('id')) + '</strong><br />';
            var summaryPoiTypes = poiFeature.get('poi_types') || [];
            if (!Array.isArray(summaryPoiTypes)) {
                summaryPoiTypes = [summaryPoiTypes];
            }
            html += escapeHtml(summaryPoiTypes.map(getPoiTypeLabel).join(', ')) + '<br />';
            html += escapeHtml(poiFeature.get('angular_coords'));
            html += '</td></tr>';
        });

        if (poiFeatures.length > maxVisiblePois) {
            html += '<tr><td colspan="2">Mostrati i primi ' + maxVisiblePois + ' POI.</td></tr>';
        }
    }

    html += '</table></li></ul>';
    return html;
}

function showPoisForSelectedHex(hexId, hexFeature) {
    if (hexPoiLoadErrorsByHexId[hexId]) {
        console.error('Errore nel caricamento dei POI dell esagono.', hexPoiLoadErrorsByHexId[hexId]);
        return;
    }

    // Attiva l'aggancio dello stile dinamico in totale sicurezza ora che i dati e i layer ci sono
    setupDynamicPoiFiltering();

    selectedPoiIds = new Set();
    selectedPoiPowersById = {};

    if (typeof lyr_pois_used2pois_used_1 !== 'undefined' && lyr_pois_used2pois_used_1) {
        lyr_pois_used2pois_used_1.setVisible(false);
        setPoiLayerFeatures([]);
        lyr_pois_used2pois_used_1.changed();
    }

    loadHexPoiRecords(hexId)
        .then(function(linkedPois) {
            if (selectedHexId !== hexId) {
                return;
            }

            var poiById = getPoiFeatureById();

            selectedPoiIds = new Set(linkedPois.map(function(item) {
                return String(item.i);
            }));

            linkedPois.forEach(function(item) {
                selectedPoiPowersById[String(item.i)] = item;
            });

            var poiFeatures = linkedPois
                .map(function(item) {
                    return poiById[String(item.i)];
                })
                .filter(function(feature) {
                    return !!feature;
                });

            updateVisiblePoiPowers();
            updatePoiTypeLegend();

            if (typeof lyr_pois_used2pois_used_1 !== 'undefined' && lyr_pois_used2pois_used_1) {
                setPoiLayerFeatures(poiFeatures);
                lyr_pois_used2pois_used_1.setVisible(poiFeatures.length > 0);
                lyr_pois_used2pois_used_1.changed();
            }
        })
        .catch(function(error) {
            console.error('Errore nel caricamento dei POI dell esagono:', error);
            selectedPoiIds = new Set();
            selectedPoiPowersById = {};

            if (typeof lyr_pois_used2pois_used_1 !== 'undefined' && lyr_pois_used2pois_used_1) {
                lyr_pois_used2pois_used_1.setVisible(false);
                setPoiLayerFeatures([]);
                lyr_pois_used2pois_used_1.changed();
            }
        });
}

function updatePopup() {
    if (popupContent) {
        content.innerHTML = popupContent;
        container.style.display = 'block';
		overlayPopup.setPosition(popupCoord);
    } else {
        container.style.display = 'none';
        closer.blur();
        stopMediaInPopup();
    }
} 

function createPoiTypeServiceTable(feature) {
    var poiTypes = feature.get('poi_types') || [];
    var svcMap = feature.get('svc_map') || {};
    var html = '';

    html += '<table class="poi-type-service-table">';
    html += '<tr><th>Type</th><th>Service</th></tr>';

    poiTypes.forEach(function(type) {
        var services = svcMap[type] || [''];
        services.forEach(function(service) {
            if (selectedService && service !== selectedService) {
                return;
            }

            html += '<tr class="poi-type-service-row">';
            html += '<td class="poi-type-cell">' + escapeHtml(getPoiTypeLabel(type)) + '</td>';
            html += '<td class="poi-service-cell">' + escapeHtml(getServiceLabel(service)) + '</td>';
            html += '</tr>';
        });
    });

    html += '</table>';
    return html;
}

function onSingleClickFeatures(evt) {
    if (doHover || sketch) {
        return;
    }
    if (!featuresPopupActive) {
        featuresPopupActive = true;
    }
    var pixel = map.getEventPixel(evt.originalEvent);
    var coord = evt.coordinate;
    var currentFeature;
    var currentFeatureKeys;
    var clusteredFeatures;
    var clickedPoiFeature = null;
    var clickedHexFeature = null;
    var popupText = '<ul>';

    if (typeof lyr_pois_used2pois_used_1 !== 'undefined' && lyr_pois_used2pois_used_1) {
        clickedPoiFeature = map.forEachFeatureAtPixel(evt.pixel, function(feature, layer) {
            if (layer === lyr_pois_used2pois_used_1) {
                return feature;
            }
        }, {
            hitTolerance: 4
        });
    }
    
    map.forEachFeatureAtPixel(pixel, function(feature, layer) {
        if (!clickedPoiFeature && typeof lyr_Cagliari_Shapefilecapability_grid_1 !== 'undefined' && layer === lyr_Cagliari_Shapefilecapability_grid_1) {
            clickedHexFeature = feature;
            selectedHexFeature = feature;
            selectedHexId = feature.get('hex_id');
            selectedPoiIds = null;
            updateLayerModeControls();
            lyr_Cagliari_Shapefilecapability_grid_1.changed();
            if (typeof lyr_pois_used2pois_used_1 !== 'undefined') {
                lyr_pois_used2pois_used_1.changed();
            }
        }

        if (layer && feature instanceof ol.Feature && (layer.get("interactive") || layer.get("interactive") === undefined)) {
            if (clickedPoiFeature && layer !== lyr_pois_used2pois_used_1) {
                return;
            }

            if (typeof lyr_Cagliari_Shapefilecapability_grid_1 !== 'undefined' && layer === lyr_Cagliari_Shapefilecapability_grid_1) {
                return;
            }

            var doPopup = false;
            for (var k in layer.get('fieldImages')) {
                if (layer.get('fieldImages')[k] !== "Hidden") {
                    doPopup = true;
                }
            }
            currentFeature = feature;
            clusteredFeatures = feature.get("features");
            if (typeof clusteredFeatures !== "undefined") {
                if (doPopup) {
                    for(var n = 0; n < clusteredFeatures.length; n++) {
                        currentFeature = clusteredFeatures[n];
                        currentFeatureKeys = currentFeature.getKeys();
                        popupText += '<li><table>';
                        popupText += '<a><b>' + layer.get('popuplayertitle') + '</b></a>';
                        popupText += createPopupField(currentFeature, currentFeatureKeys, layer);
                        popupText += '</table></li>';    
                    }
                }
            } else {
                currentFeatureKeys = currentFeature.getKeys();
                if (doPopup) {
                    popupText += '<li><table>';

                    if (layer === lyr_pois_used2pois_used_1) {
                        popupText += '<tr><td colspan="2"><strong>Coordinates</strong><br />'+currentFeature.get('angular_coords')+'</td></tr>';
                        popupText += createPopupField(currentFeature, currentFeatureKeys, layer);
                        popupText += '<tr><td colspan="2">' + createPoiTypeServiceTable(currentFeature) + '</td></tr>';
                        popupText += '<tr id="poi-address-row"><td colspan="2"><strong>Address</strong><br />Loading...</td></tr>';

                        reverseGeocodePoi(currentFeature).then(function(info) {
                            var row = document.getElementById('poi-address-row');
                            if (row) {
                                row.outerHTML = createPoiAddressHtml(info, 'poi-address-row');
                            }
                        }).catch(function(error) {
                            console.error('Errore reverse geocoding POI:', error);
                            var row = document.getElementById('poi-address-row');
                            if (row) {
                                row.outerHTML = createPoiAddressErrorHtml('poi-address-row');
                            }
                        });
                    } else {
                        popupText += createPopupField(currentFeature, currentFeatureKeys, layer);
                    }

                    popupText += '</table></li>';
                }
            }
        }
    });
    if (popupText === '<ul>') {
        popupText = '';
    } else {
        popupText += '</ul>';
    }
	
	popupContent = popupText;
    popupCoord = coord;
    updatePopup();

    if (clickedHexFeature) {
        showPoisForSelectedHex(selectedHexId, clickedHexFeature);
    }
}

function onSingleClickWMS(evt) {
    if (doHover || sketch) {
        return;
    }
    if (!featuresPopupActive) {
        popupContent = '';
    }
    var coord = evt.coordinate;
    var viewProjection = map.getView().getProjection();
    var viewResolution = map.getView().getResolution();

    if (typeof wms_layers !== 'undefined') {
        for (var i = 0; i < wms_layers.length; i++) {
            if (wms_layers[i][1] && wms_layers[i][0].getVisible()) {
                var url = wms_layers[i][0].getSource().getFeatureInfoUrl(
                    evt.coordinate, viewResolution, viewProjection, {
                        'INFO_FORMAT': 'text/html',
                    });
                if (url) {
                    const wmsTitle = wms_layers[i][0].get('popuplayertitle');
                    var ldsRoller = '<div class="roller-switcher" style="height: 25px; width: 25px;"></div>';

                    popupCoord = coord;
                    popupContent += ldsRoller;
                    updatePopup();

                    var timeoutPromise = new Promise((resolve, reject) => {
                        setTimeout(() => {
                            reject(new Error('Timeout exceeded'));
                        }, 5000);
                    });

                    function tryFetch(urls) {
                        if (urls.length === 0) {
                            return Promise.reject(new Error('All fetch attempts failed'));
                        }
                        return fetch(urls[0])
                            .then((response) => {
                                if (response.ok) {
                                    return response.text();
                                } else {
                                    throw new Error('Fetch failed');
                                }
                            })
                            .catch(() => tryFetch(urls.slice(1)));
                    }

                    const urlsToTry = [
                        url,
                        encodeURIComponent(url),
                        'https://api.allorigins.win/raw?url=' + encodeURIComponent(url)
                    ];

                    Promise.race([tryFetch(urlsToTry), timeoutPromise])
                        .then((html) => {
                            if (html.indexOf('<table') !== -1) {
                                popupContent += '<a><b>' + wmsTitle + '</b></a>';
                                popupContent += html + '<p></p>';
                                updatePopup();
                            }
                        })
                        .finally(() => {
                            setTimeout(() => {
                                var loaderIcon = document.querySelector('.roller-switcher');
                                if (loaderIcon) loaderIcon.remove();
                            }, 500);
                        });
                }
            }
        }
    }
}

map.on('singleclick', onSingleClickFeatures);
map.on('singleclick', onSingleClickWMS);

var topLeftContainerDiv = document.getElementById('top-left-container')
var bottomLeftContainerDiv = document.getElementById('bottom-left-container')
var topRightContainerDiv = document.getElementById('top-right-container')
var bottomRightContainerDiv = document.getElementById('bottom-right-container')

var bottomAttribution = new ol.control.Attribution({
  collapsible: false,
  collapsed: false,
  className: 'bottom-attribution'
});
map.addControl(bottomAttribution);

map.once('rendercomplete', function() {
  var bottomAttributionUl = bottomAttribution.element.querySelector('ul');
  if (bottomAttributionUl) {
    var layerAttrs = Array.from(bottomAttributionUl.querySelectorAll('li'))
      .map(function(li) { return li.innerHTML.trim(); }).filter(Boolean);
    var attribHtml = `
    <a href="https://github.com/qgis2web/qgis2web">qgis2web</a> &middot;
    <a href="https://openlayers.org/">OpenLayers</a> &middot;
    <a href="https://qgis.org/">QGIS</a>`;
    if (layerAttrs.length > 0) { attribHtml += ' &nbsp;|&nbsp; ' + layerAttrs.join(', '); }
    bottomAttributionUl.innerHTML = '<li>' + attribHtml + '</li>';
  }
});

var preDoHover = doHover;
var preDoHighlight = doHighlight;
var isPopupAllActive = false;
document.addEventListener('DOMContentLoaded', function() {
	if (doHover || doHighlight) {
		var controlElements = document.getElementsByClassName('ol-control');
		for (var i = 0; i < controlElements.length; i++) {
			controlElements[i].addEventListener('mouseover', function() { 
				doHover = false;
				doHighlight = false;
			});
			controlElements[i].addEventListener('mouseout', function() {
				doHover = preDoHover;
				if (isPopupAllActive) { return }
				doHighlight = preDoHighlight;
			});
		}
	}
});

var zoomControl = document.getElementsByClassName('ol-zoom')[0];
if (zoomControl) {
    topLeftContainerDiv.appendChild(zoomControl);
}
if (typeof geolocateControl !== 'undefined') {
    topLeftContainerDiv.appendChild(geolocateControl);
}
if (typeof measureControl !== 'undefined') {
    topLeftContainerDiv.appendChild(measureControl);
}
var searchbar = document.getElementsByClassName('photon-geocoder-autocomplete ol-unselectable ol-control')[0];
if (searchbar) {
    topLeftContainerDiv.appendChild(searchbar);
}
var searchLayerControl = document.getElementsByClassName('search-layer')[0];
if (searchLayerControl) {
    topLeftContainerDiv.appendChild(searchLayerControl);
}
var scaleLineControl = document.getElementsByClassName('ol-scale-line')[0];
if (scaleLineControl) {
    scaleLineControl.className += ' ol-control';
    bottomRightContainerDiv.appendChild(scaleLineControl);
}
var attributionControl = document.getElementsByClassName('bottom-attribution')[0];
if (attributionControl) {
    bottomRightContainerDiv.appendChild(attributionControl);
}
