# Integrating the sharded hex-POI store into the interface

The per-hexagon files in `resources/hex_pois/` changed format: instead of one
`H####_####.js` file per hexagon (~886 MB total), hexagons are now grouped
into **shard** files (`s####_####.js`), each holding a 4×4 block of hexagons
with compact-encoded, zlib-compressed records, plus the usual `index.js`
manifest. Total size is roughly 10–20× smaller. Loading is still plain
`<script>` injection, so everything keeps working over `file://`.

`hex_shard_loader.js` (in this folder) is a drop-in replacement for the old
loading code: it exposes the **same** function

```js
loadHexPoiRecords(hexId) -> Promise<[{i, sp:{service:val}, cp:{cap:val}}, ...]>
```

with the same resolved record shape, so nothing downstream of the promise
needs to change. It also bounds memory with two small LRU caches (the old
`hexPoiRecordsByHexId` cache grew without limit — that alone could crash the
tab after browsing many hexagons).

## Steps

1. **Replace the data**: delete the old `resources/hex_pois/` contents and
   unzip the new `hex_pois.zip` there (shard `s*.js` files + `index.js`).

2. **Add the loader** to `index.html`, before `resources/qgis2web.js`:

   ```html
   <script src="resources/hex_shard_loader.js"></script>
   ```

   (Copy `hex_shard_loader.js` into `resources/`. If the shard directory is
   not `resources/hex_pois/`, set `window.HEX_POIS_BASE = ".../";` in a small
   inline script *before* this tag.)

3. **Remove the old loader code from `resources/qgis2web.js`** — two blocks:

   - The `__onHexPois` JSONP callback registration (currently lines ~75–82,
     starting at the comment `// I power dei POI non sono piu' casuali...`):

     ```js
     window.__onHexPois = function(hexId, records) {
         hexPoiRecordsByHexId[hexId] = Array.isArray(records) ? records : [];
     };
     ```

     The old shard files no longer call `__onHexPois`, so this is dead code.
     Keep `window.__onHexPoisManifest` if you still use `window.hexPoisManifest`
     elsewhere — the new loader chains to a pre-existing handler instead of
     replacing it (but note the manifest is now loaded lazily, on the first
     `loadHexPoiRecords` call, not necessarily at startup).

   - The whole `function loadHexPoiRecords(hexId) { ... }` definition
     (currently lines ~1055–1084). The loader script provides
     `window.loadHexPoiRecords` with the same behavior.

   The now-unused top-of-file variables can also be removed:
   `hexPoiRecordsByHexId`, `hexPoiLoadPromisesByHexId`,
   `hexPoiLoadErrorsByHexId`, `hexPoiScriptNodesByHexId` (line ~6).

4. Nothing else changes: the click handler keeps calling
   `loadHexPoiRecords(hexId).then(...)` exactly as before.

## Manifest

`resources/hex_pois/index.js` still calls `__onHexPoisManifest({...})`. New
fields next to the existing `slug` / `count` / `hex_ids`:

| field | meaning |
|---|---|
| `schema` | `"hexagon_poi_powers_v2"` |
| `shard.block` | hexes per shard side (shard = `s{col//block}_{row//block}`, zero-padded to 4) |
| `encoding` | `"deflate-base64"` — shard payloads are zlib-deflate, base64-wrapped |
| `scale` | `10000` — stored power ints are `round(value * scale)` |
| `services`, `capabilities` | ordered key lists; compact records store indices into these |

You normally don't need any of this — the loader handles it — but it's there
if you want to read shards yourself. Raw record format inside a shard:
`poi_id` (number, id-only) or `[poi_id, [svcIdx, q, ...], [capIdx, q, ...]]`
with `value = q / scale`.

## Browser support

Decompression uses the native `DecompressionStream` API (Chrome/Edge 80+,
Firefox 113+, Safari 16.4+). No external library is needed. If you must
support older browsers, load [pako](https://github.com/nodeca/pako)
(`pako_inflate.min.js`) and replace the `inflate(bytes)` function in
`hex_shard_loader.js` with:

```js
function inflate(bytes) {
    return Promise.resolve(pako.inflate(bytes, { to: "string" }));
}
```

## Trying it standalone

`test.html` in this folder loads the loader against an export directory and
prints decoded records for a few hexagons — open it over `file://` to sanity
check a new export without touching the real interface.
