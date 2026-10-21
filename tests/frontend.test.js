const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

let Panel;
let nextTimer = 1;
const timers = new Set();
const shadowRoot = () => ({
  innerHTML: "",
  activeElement: null,
  getElementById: () => null,
  querySelectorAll: () => [],
});
const context = {
  HTMLElement: class {
    attachShadow() { this.shadowRoot = shadowRoot(); }
    dispatchEvent() {}
  },
  customElements: { define: (_name, klass) => { Panel = klass; } },
  CustomEvent: class {},
  Intl,
  Date,
  Map,
  Set,
  Number,
  Boolean,
  String,
  Array,
  console,
  setInterval: () => { const id = nextTimer++; timers.add(id); return id; },
  clearInterval: (id) => timers.delete(id),
  confirm: () => true,
};
vm.createContext(context);
vm.runInContext(
  fs.readFileSync("custom_components/gui_recorder/frontend/gui-recorder-panel.js", "utf8"),
  context,
);

function fixturePanel() {
  const panel = new Panel();
  panel._migration = {};
  panel._data = {
    ...panel._data,
    devices: [{
      device_id: "device-1",
      name: "Kitchen sensor",
      manufacturer: "Demo",
      model: "One",
      record_count: 120010,
      entities: [
        { entity_id: "sensor.a", name: "A", platform: "demo", domain: "sensor", record_count: 120000, recorded: true },
        { entity_id: "sensor.b", name: "B", platform: "demo", domain: "sensor", record_count: 10, recorded: false },
      ],
    }],
    orphans: [{ entity_id: "sensor.orphan", name: "Orphan", platform: "demo", domain: "sensor", record_count: 5, recorded: true }],
    obsolete: [{ entity_id: "sensor.old", name: "Old", platform: "sensor", domain: "sensor", record_count: 7, recorded: false }],
    matched_exclusions: ["sensor.b"],
    stats: {
      entity_counts: { "sensor.a": 120000, "sensor.b": 10 },
      total_rows: 120022,
      backend: "mariadb",
      db_size_bytes: 1000,
      db_product: "MariaDB",
      db_server_version: "10.11.8-MariaDB",
      db_character_set: "utf8mb4",
      db_collation: "utf8mb4_bin",
      db_default_engine: "InnoDB",
      db_non_innodb_tables: 0,
      db_health_status: "ok",
      db_health_issues: [],
    },
  };
  return panel;
}

async function run() {
  const panel = fixturePanel();
  panel._render = () => {};
  panel._setBulkDevice("device-1", true);
  assert.strictEqual(JSON.stringify([...panel._bulkSelected].sort()), JSON.stringify(["sensor.a", "sensor.b"]));
  panel._quickFilter = "large";
  assert.strictEqual(panel._deviceVisibleEntities(panel._data.devices[0]).length, 1);
  assert.strictEqual(panel._deviceVisibleEntities(panel._data.devices[0])[0].entity_id, "sensor.a");

  const paged = panel._pageItems(Array.from({ length: 205 }, (_, index) => index), "orphans");
  assert.strictEqual(paged.items.length, 100);
  assert.strictEqual(paged.pageCount, 3);

  const calls = [];
  panel._quickFilter = "all";
  panel._confirmPurgePreview = async () => true;
  panel._hass = { connection: { sendMessagePromise: async (message) => {
    calls.push(message);
    return { job: { job_id: "job-1", active: true, status: "queued", kind: "selected", label: "2 selected entities" } };
  } } };
  panel._bulkSelected = new Set(["sensor.a", "sensor.b"]);
  panel._pollPurgeProgress = async () => {};
  await panel._purgeSelected();
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].type, "gui_recorder/purge_selected");
  assert.strictEqual(panel._purgeProgress.job_id, "job-1");
  assert.strictEqual(panel._purgeProgressTimer != null, true);
  panel._stopPurgeProgressPolling();

  let refreshed = false;
  panel._refreshTreeData = async () => { refreshed = true; };
  await panel._handlePurgeFinished({
    job_id: "job-1",
    status: "completed",
    kind: "selected",
    label: "2 selected entities",
    determinate: true,
    deleted_rows: 120010,
    stats_refreshed: false,
    started_at: "2026-10-21T10:00:00+00:00",
    finished_at: "2026-10-21T10:00:10+00:00",
  });
  assert.strictEqual(refreshed, false);
  assert.strictEqual(panel._refreshRecommended, true);
  assert.match(panel._dataMessage, /Update data/);

  const renderPanel = fixturePanel();
  renderPanel._bulkMode = true;
  renderPanel._expanded.add("device-1");
  renderPanel._purgeHistory = [{
    job_id: "old-job",
    label: "sensor.old",
    status: "completed",
    entity_count: 1,
    deleted_rows: 7,
    started_at: "2026-10-21T10:00:00+00:00",
    finished_at: "2026-10-21T10:00:07+00:00",
  }];
  renderPanel._purgePreview = {
    title: "Confirm selected purge",
    entities: 2,
    records: 120010,
    repack: false,
  };
  renderPanel._render();
  assert.match(renderPanel.shadowRoot.innerHTML, /Maintenance history/);
  assert.match(renderPanel.shadowRoot.innerHTML, /id="quick-filter"/);
  assert.match(renderPanel.shadowRoot.innerHTML, /data-bulk-device="device-1"/);
  assert.match(renderPanel.shadowRoot.innerHTML, /MariaDB indexes/);
  assert.match(renderPanel.shadowRoot.innerHTML, /MariaDB Health/);
  assert.match(renderPanel.shadowRoot.innerHTML, /10\.11\.8-MariaDB/);
  assert.match(renderPanel.shadowRoot.innerHTML, /Confirm selected purge/);
  assert.match(renderPanel.shadowRoot.innerHTML, /Refresh statistics afterward/);
  assert.doesNotMatch(renderPanel.shadowRoot.innerHTML, /sensor\.orphan/);

  renderPanel._collapsedSections.delete("orphans");
  renderPanel._render();
  assert.match(renderPanel.shadowRoot.innerHTML, /sensor\.orphan/);
  renderPanel._setAllSections(true);
  assert.strictEqual(renderPanel._collapsedSections.has("devices"), true);
  renderPanel._setAllSections(false);
  assert.strictEqual(renderPanel._collapsedSections.size, 0);

  console.log("frontend tests: ok");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
