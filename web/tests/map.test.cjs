const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");

function createMap(stations = []) {
  const tiles = [];
  const markers = [];
  const map = {
    setView(center, zoom) {
      this.center = Array.from(center);
      this.zoom = zoom;
      return this;
    },
    removeLayer() {},
  };
  const context = vm.createContext({
    window: {},
    document: { addEventListener() {} },
    L: {
      map(id, options) {
        map.id = id;
        map.options = options;
        return map;
      },
      tileLayer(url, options) {
        const tile = { url, options, addTo(target) { this.map = target; } };
        tiles.push(tile);
        return tile;
      },
      circleMarker(coordinates, options) {
        const marker = {
          coordinates: Array.from(coordinates),
          options,
          addTo(target) { this.map = target; return this; },
          bindPopup(html) { this.popup = html; },
          on(event, handler) { this[event] = handler; },
        };
        markers.push(marker);
        return marker;
      },
    },
  });
  vm.runInContext(source, context);
  context.stations = stations;
  vm.runInContext('STATE.data = { stations, mois_courant_nom: "octobre" }; initMap();', context);
  return { context, map, tiles, markers };
}

test("map uses one standard HTTPS OpenStreetMap layer with visible attribution", () => {
  const { map, tiles } = createMap();
  assert.equal(tiles.length, 1);
  assert.equal(tiles[0].url, "https://tile.openstreetmap.org/{z}/{x}/{y}.png");
  assert.match(tiles[0].options.attribution, /https:\/\/www\.openstreetmap\.org\/copyright/);
  assert.match(tiles[0].options.attribution, /OpenStreetMap contributors/);
  assert.equal(tiles[0].map, map);
  assert.equal(map.options.attributionControl, true);
  assert.equal(tiles[0].options.detectRetina, undefined);
});

test("basemap change preserves Quebec framing and interactive station markers", () => {
  const { context, map, markers } = createMap([{
    code: "030348",
    plan_deau: "Runnels",
    lat: 45.8,
    lon: -73,
    categorie_observe: "eleve",
    categorie_etiage: "critique",
    pression_observe_pct: 34,
    pression_etiage_pct: 92.3,
  }]);
  assert.equal(map.id, "map");
  assert.deepEqual(map.center, [47.5, -72.5]);
  assert.equal(map.zoom, 6);
  assert.equal(map.options.minZoom, 5);
  assert.equal(map.options.maxZoom, 12);
  assert.equal(markers.length, 1);
  assert.deepEqual(markers[0].coordinates, [45.8, -73]);
  assert.equal(markers[0].options.fillColor, "#d97a4a");
  assert.equal(typeof markers[0].click, "function");
  assert.match(markers[0].popup, /Runnels/);
  assert.match(markers[0].popup, /34\.0/);
  assert.match(markers[0].popup, /92\.3/);
  vm.runInContext('STATE.mode = "etiage"; refreshMarkers();', context);
  assert.equal(markers[1].options.fillColor, "#a32424");
});
