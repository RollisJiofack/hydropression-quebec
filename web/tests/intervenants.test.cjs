const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../app.js"), "utf8");

function render(intervenants) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { style: {}, textContent: "", innerHTML: "", hidden: false });
    return elements.get(id);
  };
  const context = vm.createContext({ window: {}, HydroStationFlags: require("../station-flags.js"),
    document: { addEventListener() {}, getElementById: element, querySelector: element } });
  vm.runInContext(source, context);
  context.station = { code: "040409", plan_deau: "Test", pression_observe_pct: 0,
    pression_etiage_pct: 28, n_sites_amont: 4, n_sites_inactifs_mois: 2, intervenants };
  vm.runInContext('STATE.data = {mois_courant_nom:"octobre"}; openDetail(station);', context);
  return element("tech-intervenants").innerHTML;
}
test("complete JSON list keeps the old active-only portal table byte-identical", () => {
  const active = [{ num_site: "A", nom_intervenant: "Active A", debit_mois_courant_m3s: 2 },
    { num_site: "B", nom_intervenant: "Active B", debit_mois_courant_m3s: 1 }];
  const inactive = [{ num_site: "C", nom_intervenant: "Inactive C", debit_mois_courant_m3s: 0 },
    { num_site: "D", nom_intervenant: "Inactive D", debit_mois_courant_m3s: 0 }];
  assert.equal(render([...active, ...inactive]), render(active));
  assert.doesNotMatch(render([...active, ...inactive]), /Inactive [CD]/);
  assert.match(render([...active, ...inactive]), /Active A/);
});
test("all-inactive sites do not create visible zero-flow portal rows", () => {
  const inactive = [{ num_site: "C", nom_intervenant: "Inactive C", debit_mois_courant_m3s: 0 }];
  assert.equal(render(inactive), render([]));
  assert.match(render(inactive), /Aucun préleveur avec déclaration/);
});
