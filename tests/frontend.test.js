const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

let Panel;
let nextTimer = 1;
const timers = new Set();
const timeoutCallbacks = new Map();
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
  setTimeout: (callback) => {
    const id = nextTimer++;
    timers.add(id);
    timeoutCallbacks.set(id, callback);
    return id;
  },
  clearTimeout: (id) => {
    timers.delete(id);
    timeoutCallbacks.delete(id);
  },
  confirm: () => true,
};
vm.createContext(context);
const frontendSource = fs.readFileSync("custom_components/gui_recorder/frontend/gui-recorder-panel.js", "utf8");
vm.runInContext(frontendSource, context);

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
  assert.doesNotMatch(frontendSource, /setInterval\s*\(/);

  const scheduledPollPanel = fixturePanel();
  let scheduledPollCalls = 0;
  scheduledPollPanel._pollPurgeProgress = async () => { scheduledPollCalls += 1; };
  scheduledPollPanel._startPurgeProgressPolling();
  const firstScheduledTimer = scheduledPollPanel._purgeProgressTimer;
  const firstScheduledCallback = timeoutCallbacks.get(firstScheduledTimer);
  timers.delete(firstScheduledTimer);
  timeoutCallbacks.delete(firstScheduledTimer);
  await firstScheduledCallback();
  assert.strictEqual(scheduledPollCalls, 1);
  assert.notStrictEqual(scheduledPollPanel._purgeProgressTimer, firstScheduledTimer);
  scheduledPollPanel._stopPurgeProgressPolling();

  const panel = fixturePanel();
  panel._render = () => {};
  panel._setBulkDevice("device-1", true);
  assert.strictEqual(JSON.stringify([...panel._bulkSelected].sort()), JSON.stringify(["sensor.a", "sensor.b"]));
  panel._quickFilter = "large";
  assert.strictEqual(panel._deviceVisibleEntities(panel._data.devices[0]).length, 1);
  assert.strictEqual(panel._deviceVisibleEntities(panel._data.devices[0])[0].entity_id, "sensor.a");
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(panel._deviceState(panel._data.devices[0]))),
    { mode: "mixed", all: false, some: true, label: "1/2" },
  );

  const progressPanel = fixturePanel();
  assert.strictEqual(progressPanel._setPurgeProgress({
    job_id: "job-new",
    status: "completed",
    updated_at: "2026-10-21T10:00:02+00:00",
  }), true);
  assert.strictEqual(progressPanel._setPurgeProgress({
    job_id: "job-new",
    status: "purging",
    active: true,
    updated_at: "2026-10-21T10:00:01+00:00",
  }), false);
  assert.strictEqual(progressPanel._purgeProgress.status, "completed");

  const guardedPollPanel = fixturePanel();
  guardedPollPanel._render = () => {};
  let pollCalls = 0;
  let releasePoll;
  const pendingPoll = new Promise((resolve) => { releasePoll = resolve; });
  guardedPollPanel._hass = { connection: { sendMessagePromise: async () => {
    pollCalls += 1;
    return pendingPoll;
  } } };
  const firstPoll = guardedPollPanel._pollPurgeProgress();
  const overlappingPoll = guardedPollPanel._pollPurgeProgress();
  assert.strictEqual(pollCalls, 1);
  releasePoll({
    job_id: "job-guarded",
    status: "purging",
    active: true,
    updated_at: "2026-10-21T10:00:03+00:00",
  });
  await Promise.all([firstPoll, overlappingPoll]);
  assert.strictEqual(guardedPollPanel._purgePollInFlight, false);

  const reconnectPanel = fixturePanel();
  reconnectPanel._render = () => {};
  reconnectPanel._isDataStale = () => false;
  let resolveFirstTree;
  let treeLoads = 0;
  const firstTree = new Promise((resolve) => { resolveFirstTree = resolve; });
  reconnectPanel._hass = { connection: { sendMessagePromise: async (message) => {
    if (message.type === "gui_recorder/get_tree") {
      treeLoads += 1;
      if (treeLoads === 1) return firstTree;
      return reconnectPanel._data;
    }
    if (message.type === "gui_recorder/get_purge_progress") return { active: false, history: [] };
    return {};
  } } };
  reconnectPanel._connected = true;
  reconnectPanel._loaded = true;
  const initialLoad = reconnectPanel._load();
  reconnectPanel.disconnectedCallback();
  reconnectPanel.connectedCallback();
  assert.strictEqual(reconnectPanel._reloadAfterCurrentLoad, true);
  resolveFirstTree(reconnectPanel._data);
  await initialLoad;
  await Promise.resolve();
  await Promise.resolve();
  assert.strictEqual(treeLoads, 2);

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
  assert.strictEqual(panel._purgeProgressPolling, true);
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
  let cancelFocused = false;
  let confirmFocused = false;
  let escapeHandler = null;
  const cancelButton = {
    focus: () => {
      cancelFocused = true;
      renderPanel.shadowRoot.activeElement = cancelButton;
    },
    addEventListener: () => {},
  };
  const confirmButton = {
    focus: () => {
      confirmFocused = true;
      renderPanel.shadowRoot.activeElement = confirmButton;
    },
    addEventListener: () => {},
  };
  renderPanel.shadowRoot.getElementById = (id) => {
    if (id === "purge-preview-cancel") return cancelButton;
    if (id === "purge-preview-confirm") return confirmButton;
    if (id === "purge-preview-backdrop") {
      return { addEventListener: (type, handler) => { if (type === "keydown") escapeHandler = handler; } };
    }
    return null;
  };
  renderPanel._render();
  assert.strictEqual(cancelFocused, true);
  assert.strictEqual(typeof escapeHandler, "function");
  assert.match(renderPanel.shadowRoot.innerHTML, /Maintenance history/);
  assert.match(renderPanel.shadowRoot.innerHTML, /id="quick-filter"/);
  assert.match(renderPanel.shadowRoot.innerHTML, /data-bulk-device="device-1"/);
  assert.match(renderPanel.shadowRoot.innerHTML, /MariaDB indexes/);
  assert.match(renderPanel.shadowRoot.innerHTML, /MariaDB Health/);
  assert.match(renderPanel.shadowRoot.innerHTML, /10\.11\.8-MariaDB/);
  assert.match(renderPanel.shadowRoot.innerHTML, /Confirm selected purge/);
  assert.match(renderPanel.shadowRoot.innerHTML, /Refresh statistics afterward/);
  assert.match(renderPanel.shadowRoot.innerHTML, /aria-label="Nightly auto purge"/);
  assert.match(renderPanel.shadowRoot.innerHTML, /aria-label="Purge stored history for sensor\.a"/);
  assert.match(renderPanel.shadowRoot.innerHTML, /class="table-scroll"/);
  assert.match(renderPanel.shadowRoot.innerHTML, /prefers-reduced-motion/);
  assert.doesNotMatch(renderPanel.shadowRoot.innerHTML, /sensor\.orphan/);

  renderPanel.shadowRoot.activeElement = confirmButton;
  let tabPrevented = false;
  escapeHandler({
    key: "Tab",
    shiftKey: false,
    preventDefault: () => { tabPrevented = true; },
    stopPropagation: () => {},
  });
  assert.strictEqual(tabPrevented, true);
  assert.strictEqual(renderPanel.shadowRoot.activeElement, cancelButton);
  renderPanel.shadowRoot.activeElement = cancelButton;
  escapeHandler({
    key: "Tab",
    shiftKey: true,
    preventDefault: () => {},
    stopPropagation: () => {},
  });
  assert.strictEqual(confirmFocused, true);
  assert.strictEqual(renderPanel.shadowRoot.activeElement, confirmButton);

  renderPanel._purgeProgress = {
    active: true,
    status: "stalled",
    warning: "No progress",
  };
  renderPanel._render();
  assert.match(renderPanel.shadowRoot.innerHTML, /Restart stalled job/);
  assert.match(renderPanel.shadowRoot.innerHTML, /aria-label="Purge progress"/);
  assert.doesNotMatch(
    renderPanel.shadowRoot.innerHTML,
    /id="restart-ha" disabled/,
  );

  let escapePrevented = false;
  escapeHandler({
    key: "Escape",
    preventDefault: () => { escapePrevented = true; },
    stopPropagation: () => {},
  });
  assert.strictEqual(escapePrevented, true);
  assert.strictEqual(renderPanel._purgePreview, null);

  renderPanel._collapsedSections.delete("orphans");
  renderPanel._render();
  assert.match(renderPanel.shadowRoot.innerHTML, /sensor\.orphan/);
  renderPanel._setAllSections(true);
  assert.strictEqual(renderPanel._collapsedSections.has("devices"), true);
  renderPanel._setAllSections(false);
  assert.strictEqual(renderPanel._collapsedSections.size, 0);

  const migrationPanel = fixturePanel();
  migrationPanel._migration = {
    legacy_detected: true,
    gui_include_active: false,
    legacy_summary: { include_entities_count: 1, include_domains_count: 1 },
  };
  migrationPanel._message = "Global feedback remains visible during migration.";
  migrationPanel._render();
  assert.match(migrationPanel.shadowRoot.innerHTML, /role="status" aria-live="polite"/);
  assert.match(migrationPanel.shadowRoot.innerHTML, /Global feedback remains visible during migration\./);
  assert.match(migrationPanel.shadowRoot.innerHTML, /standalone <code>include\.entities<\/code> allow-list is blocked/);
  assert.doesNotMatch(migrationPanel.shadowRoot.innerHTML, /will be lost/);
  assert.doesNotMatch(migrationPanel.shadowRoot.innerHTML, /<h2>Maintenance actions<\/h2>/);

  const focusPanel = fixturePanel();
  let focusRestored = false;
  focusPanel.shadowRoot.activeElement = { id: "bulk-mode-toggle" };
  focusPanel._render = () => {};
  const previewResult = focusPanel._confirmPurgePreview({ title: "Focus test" });
  focusPanel.shadowRoot.getElementById = (id) => (
    id === "bulk-mode-toggle" ? { focus: () => { focusRestored = true; } } : null
  );
  focusPanel._resolvePurgePreview(false);
  assert.strictEqual(await previewResult, false);
  assert.strictEqual(focusRestored, true);

  console.log("frontend tests: ok");
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
