/**
 * hex_shard_loader.js — drop-in loader for the sharded hexagon-POI store
 * (schema hexagon_poi_powers_v2, produced by hex_shard_writer.py).
 *
 * Public API (identical name, signature and resolved shape to the old
 * per-hexagon loader in qgis2web.js):
 *
 *     loadHexPoiRecords(hexId) -> Promise<[{i, sp:{service:val}, cp:{cap:val}}, ...]>
 *
 * How it works:
 *   - hexId -> shard filename is a pure function (no lookup table), so
 *     retrieval stays O(1): one <script> injection per shard, then dict
 *     lookups. Script injection (JSONP) keeps everything working over
 *     file:// where fetch()/XHR are blocked.
 *   - Shard payloads are zlib-deflated + base64; they are inflated with the
 *     browser-native DecompressionStream API (no external library).
 *   - Memory is bounded: shards are cached in compact form in a small LRU,
 *     and decoded per-hexagon records have their own small LRU — unlike the
 *     old unbounded hexPoiRecordsByHexId cache, browsing many hexagons
 *     cannot grow memory indefinitely.
 *
 * Configuration (optional, set BEFORE this script is loaded):
 *     window.HEX_POIS_BASE = "resources/hex_pois/";   // shard directory URL
 */
(function () {
  "use strict";

  var BASE = (typeof window !== "undefined" && window.HEX_POIS_BASE) || "resources/hex_pois/";
  var SCHEMA = "hexagon_poi_powers_v2";
  var MAX_CACHED_SHARDS = 3;  // compact (undecoded) shard payloads
  var MAX_DECODED_HEXES = 16; // fully decoded per-hexagon record arrays

  var manifest = null;
  var manifestPromise = null;
  var shardCache = new Map();          // shard name -> compact hexmap {hexId: [records]}
  var shardPromises = new Map();       // shard name -> in-flight Promise
  var decodedHexCache = new Map();     // hexId -> decoded records
  var pendingShardPayloads = {};       // shard name -> base64 payload (set by JSONP callback)

  // ---------------------------------------------------------------- JSONP

  // Chain rather than clobber, in case the page already registered a
  // manifest handler of its own.
  var prevManifestCb = typeof window.__onHexPoisManifest === "function" ? window.__onHexPoisManifest : null;
  window.__onHexPoisManifest = function (m) {
    manifest = m;
    if (prevManifestCb) { try { prevManifestCb(m); } catch (e) { /* ignore */ } }
  };

  window.__onHexShardZ = function (shardName, b64) {
    pendingShardPayloads[shardName] = b64;
  };

  // -------------------------------------------------------------- helpers

  function injectScript(url) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = url;
      s.async = true;
      s.onload = function () { s.remove(); resolve(); };
      s.onerror = function () { s.remove(); reject(new Error("Failed to load " + url)); };
      document.head.appendChild(s);
    });
  }

  function ensureManifest() {
    if (manifest) { return Promise.resolve(manifest); }
    if (!manifestPromise) {
      manifestPromise = injectScript(BASE + "index.js").then(function () {
        if (!manifest) { throw new Error("hex_pois manifest (index.js) did not register"); }
        if (manifest.schema !== SCHEMA) {
          throw new Error("Unsupported hex_pois schema " + manifest.schema + " (expected " + SCHEMA + ")");
        }
        return manifest;
      });
      manifestPromise.catch(function () { manifestPromise = null; });
    }
    return manifestPromise;
  }

  function pad4(n) { return String(n).padStart(4, "0"); }

  // Mirrors hex_shard_writer.shard_name_for_hex — keep in sync.
  function shardNameForHex(hexId, block) {
    var m = /^H(\d+)_(\d+)$/.exec(hexId);
    if (m) {
      var a = Math.floor(parseInt(m[1], 10) / block);
      var b = Math.floor(parseInt(m[2], 10) / block);
      return "s" + pad4(a) + "_" + pad4(b);
    }
    var h = 0;
    for (var i = 0; i < hexId.length; i++) { h = (h * 31 + hexId.charCodeAt(i)) % 4096; }
    return "sx" + pad4(h);
  }

  function base64ToBytes(b64) {
    var bin = atob(b64);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) { bytes[i] = bin.charCodeAt(i); }
    return bytes;
  }

  function inflate(bytes) {
    if (typeof DecompressionStream === "undefined") {
      return Promise.reject(new Error(
        "This browser lacks DecompressionStream; use the pako fallback described in INTEGRATION.md"));
    }
    // "deflate" = zlib-wrapped deflate, matching Python's zlib.compress().
    var stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate"));
    return new Response(stream).text();
  }

  function lruTouch(map, key) {
    var val = map.get(key);
    map.delete(key);
    map.set(key, val);
    return val;
  }

  function lruTrim(map, maxSize) {
    while (map.size > maxSize) { map.delete(map.keys().next().value); }
  }

  // Compact record -> {i, sp:{name:val}, cp:{name:val}} (pure decoding; the
  // powers were fully precomputed by the pipeline).
  function decodeRecords(records, mf) {
    var out = [];
    for (var k = 0; k < records.length; k++) {
      var rec = records[k];
      if (typeof rec === "number") { out.push({ i: rec }); continue; }
      var item = { i: rec[0] };
      var spFlat = rec[1] || [];
      var cpFlat = rec[2] || [];
      var j;
      if (spFlat.length) {
        var sp = {};
        for (j = 0; j < spFlat.length; j += 2) { sp[mf.services[spFlat[j]]] = spFlat[j + 1] / mf.scale; }
        item.sp = sp;
      }
      if (cpFlat.length) {
        var cp = {};
        for (j = 0; j < cpFlat.length; j += 2) { cp[mf.capabilities[cpFlat[j]]] = cpFlat[j + 1] / mf.scale; }
        item.cp = cp;
      }
      out.push(item);
    }
    return out;
  }

  function loadShard(shardName) {
    if (shardCache.has(shardName)) { return Promise.resolve(lruTouch(shardCache, shardName)); }
    if (shardPromises.has(shardName)) { return shardPromises.get(shardName); }
    var p = injectScript(BASE + shardName + ".js")
      .then(function () {
        var b64 = pendingShardPayloads[shardName];
        delete pendingShardPayloads[shardName];
        if (b64 == null) { throw new Error("Shard " + shardName + " loaded but registered no payload"); }
        return inflate(base64ToBytes(b64));
      })
      .then(function (text) {
        var hexmap = JSON.parse(text);
        shardCache.set(shardName, hexmap);
        lruTrim(shardCache, MAX_CACHED_SHARDS);
        return hexmap;
      });
    var tracked = p.then(
      function (v) { shardPromises.delete(shardName); return v; },
      function (e) { shardPromises.delete(shardName); throw e; }
    );
    shardPromises.set(shardName, tracked);
    return tracked;
  }

  // ----------------------------------------------------------- public API

  window.loadHexPoiRecords = function (hexId) {
    if (decodedHexCache.has(hexId)) { return Promise.resolve(lruTouch(decodedHexCache, hexId)); }
    return ensureManifest().then(function (mf) {
      var shardName = shardNameForHex(hexId, mf.shard.block);
      return loadShard(shardName).then(function (hexmap) {
        var records = hexmap[hexId] ? decodeRecords(hexmap[hexId], mf) : [];
        decodedHexCache.set(hexId, records);
        lruTrim(decodedHexCache, MAX_DECODED_HEXES);
        return records;
      });
    });
  };

  // Exposed for the manifest consumer that previously read hex_ids/count.
  window.loadHexPoisManifest = ensureManifest;
})();
