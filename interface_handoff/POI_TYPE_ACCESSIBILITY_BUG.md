# Fixing the per-POI-type bars in the hexagon object inspector

## The symptom

Open a hexagon in the interface after selecting a capability **and** a service.
The inspector shows a "Tipi" (types) section with one bar per POI type,
meant to show each type's contribution to the score — i.e. the accessibility
of that POI type in the hexagon. But the bar lengths don't match the per-type
accessibility: they look like the POI *power*, or something else entirely.

## The cause

The bars are built by `createObjectInspectorTypeContributionsHtml()` in
`resources/qgis2web.js`. What it renders is **not** the per-type accessibility —
it's a normalized share derived from a double-counted sum. Two separate defects
stack up:

### 1. The per-type value is summed once for every POI of that type

```js
selectedPoiIds.forEach(function(poiId) {
    ...
    poiTypes.forEach(function(poiType) {
        var contribution = getPoiTypeAccessibility(selectedHexId, poiType);
        if (contribution == null) {
            contribution = getServiceContributionForPoi(poiId, 1);
        }
        contributionsByType[poiType] = (contributionsByType[poiType] || 0) + contribution; // per POI
        totalContribution += contribution;
    });
});
```

`getPoiTypeAccessibility(hex, type)` already returns `A^i_k(x)` — the **RRA
aggregation over all POIs of that type in the hexagon**. It is a per-(hexagon,
type) constant. This is the value written by the pipeline into
`resources/poi_type_accessibility.json`
(`poi_exports.py::_write_poi_type_accessibility`), and its docstring is explicit:
it's "how accessible is this hexagon to POIs of this type overall", not any
individual POI's contribution.

But the outer loop iterates over POIs and adds that constant **once per POI**.
So a type reachable via N POIs gets `N × A^i_k(x)`. That's the inflated,
"looks like power" number.

### 2. The bar width is a share, not the accessibility

```js
var widthPercent = Math.max(0, Math.min(1, contributionsByType[poiType] / totalContribution)) * 100;
```

Even without defect 1, dividing by `totalContribution` turns each bar into a
*proportion among the types present*, not the accessibility value in [0, 1].
A single type at accessibility 0.2 would render as a full 100% bar.

## The fix

Collect each reachable type **once**, read its RRA accessibility once, and use
that raw value (clamped to [0, 1]) directly as the bar width. Replace the body
of `createObjectInspectorTypeContributionsHtml()` with:

```js
function createObjectInspectorTypeContributionsHtml() {
    if (!selectedHexId || !selectedService || !selectedPoiIds) {
        return '';
    }

    var poiById = getPoiFeatureById();

    // Accessibility per POI type is the RRA aggregation over ALL POIs of that
    // type in the hexagon: one value per (hexagon, type), NOT per POI. Read it
    // once per type — summing per POI multiplies it by the POI count.
    var accessibilityByType = {};
    selectedPoiIds.forEach(function(poiId) {
        var feature = poiById[String(poiId)];
        if (!feature || !poiHasService(feature, selectedService)) {
            return;
        }
        getPoiTypesForService(feature, selectedService).forEach(function(poiType) {
            if (poiType in accessibilityByType) {
                return;  // already recorded for this type
            }
            var value = getPoiTypeAccessibility(selectedHexId, poiType);
            if (value == null) {
                // No precomputed per-type accessibility: fall back to this POI's
                // own service power as the best available proxy for the type.
                value = getServiceContributionForPoi(poiId, 0);
            }
            accessibilityByType[poiType] = value;
        });
    });

    var typeKeys = Object.keys(accessibilityByType);
    if (!typeKeys.length) {
        return '';
    }

    var rowsHtml = typeKeys.sort(function(firstType, secondType) {
        return accessibilityByType[secondType] - accessibilityByType[firstType];
    }).map(function(poiType) {
        // The bar is the accessibility value itself (0..1), not a share of a total.
        var widthPercent = Math.max(0, Math.min(1, accessibilityByType[poiType])) * 100;
        var typeColor = getPoiColorByType(poiType);

        return '<div class="object-inspector-type-row">' +
            '<span class="object-inspector-type-name">' + escapeHtml(getPoiTypeLabel(poiType)) + '</span>' +
            '<span class="object-inspector-type-bar" aria-hidden="true">' +
            '<span class="object-inspector-type-fill" style="width:' + widthPercent.toFixed(1) +
            '%; background-color:' + escapeHtml(typeColor) + '"></span>' +
            '</span>' +
            '</div>';
    }).join('');

    // Title + toggle markup below is unchanged from the original function.
    return '<div class="object-inspector-type-title">Tipi</div>' +
        '<div class="object-inspector-types">' + rowsHtml + '</div>' +
        '<div class="object-inspector-type-toggle">' +
        '<span>Appartenenza al tipo</span>' +
        '<label class="switch">' +
        '<input id="object-inspector-type-toggle" type="checkbox"' + (colorPoisByType ? ' checked' : '') + '>' +
        '<span class="slider round"></span>' +
        '</label>' +
        '</div>';
}
```

### What changed

- **Each type is recorded once** (`if (poiType in accessibilityByType) return;`),
  so the RRA value is no longer multiplied by the number of POIs of that type.
- **The bar width is the accessibility value itself** (`accessibilityByType[poiType]`
  clamped to [0, 1]), not `contribution / totalContribution`. The
  `totalContribution` accumulator is gone.
- The `null` fallback now defaults to `0` instead of `1`, so a type with no
  precomputed accessibility and no service power reads as empty rather than a
  full bar.

No other functions, data files, or markup need to change — `getPoiTypeAccessibility`,
`getPoiTypesForService`, and `poi_type_accessibility.json` are already correct;
only the consumer was wrong.
