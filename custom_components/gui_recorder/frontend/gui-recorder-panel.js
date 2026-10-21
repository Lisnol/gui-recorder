class GuiRecorderPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = {
      devices: [],
      orphans: [],
      obsolete: [],
      matched_exclusions: [],
      unmatched_exclusions: [],
      pending_restart: false,
      purge_keep_days: 10,
      auto_purge: true,
      auto_repack: true,
      commit_interval: 5,
      auto_update_data: true,
      notify_on_purge_finish: true,
      stats: { entity_counts: {}, generated_at: null, backend: null, database: "home-assistant_v2.db", db_size_bytes: null, total_rows: 0, error: null },
    };
    this._filter = "";
    this._filterDebounceTimer = null;
    this._message = "";
    this._refreshRecommended = false;
    this._expanded = new Set();
    this._pending = new Set();
    this._analyzing = false;
    this._purgingAll = false;
    this._purgingExcluded = false;
    this._bulkMode = false;
    this._bulkSelected = new Set();
    this._bulkPurging = false;
    this._purgeProgress = null;
    this._purgeProgressTimer = null;
    this._purgeProgressSeenActive = false;
    this._purgeProgressStartDeadline = 0;
    this._purgeHistory = [];
    this._lastHandledPurgeJobId = null;
    this._purgePreview = null;
    this._purgePreviewResolver = null;
    this._quickFilter = "all";
    this._sortMode = "records_desc";
    this._pageSize = 100;
    this._pages = { devices: 1, orphans: 1, obsolete: 1 };
    this._savingGlobal = false;
    this._keepDaysValue = 10;
    this._autoPurgeValue = true;
    this._autoRepackValue = true;
    this._commitIntervalValue = 5;
    this._autoUpdateDataValue = true;
    this._togglingAutoUpdate = false;
    this._repackAfterManualPurgeValue = false;
    this._togglingRepack = false;
    this._notifyOnPurgeFinishValue = true;
    this._togglingNotifications = false;
    this._dataMessage = "";
    this._migration = {};
    this._migrationBusy = false;
    this._manualExclusionsValue = "";
    this._manualExclusionsError = "";
    this._savingManualExclusions = false;
    this._collapsibleSections = ["maintenance-history", "devices", "orphans", "unmatched", "obsolete"];
    this._collapsedSections = new Set(["maintenance-history", "orphans", "unmatched", "obsolete"]);
    this._restoreCollapsedSections();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._load();
    }
    // Do NOT re-render here: HA invokes this setter on every entity state
    // change in the whole system. The render is a pure function of our own
    // state (_data from WS, _filter, _migration, etc.) and never reads from
    // hass.states, so rebuilding the shadow DOM many times per second would
    // be wasted work AND destroys focus/selection on any input (textarea,
    // filter). _render() is invoked explicitly after our own state changes.
  }

  set narrow(narrow) {
    if (this._narrow === narrow) return;
    this._narrow = narrow;
    this._render();
  }

  set route(route) {
    this._route = route;
  }

  set panel(panel) {
    this._panel = panel;
  }

  disconnectedCallback() {
    this._stopPurgeProgressPolling();
  }

  async _load() {
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/get_tree" });
      this._data = result;
      this._migration = result?.migration || {};
      this._keepDaysValue = Number(result?.purge_keep_days || 10);
      this._autoPurgeValue = Boolean(result?.auto_purge ?? true);
      this._autoRepackValue = Boolean(result?.auto_repack ?? true);
      if (!this._autoPurgeValue) this._autoRepackValue = false;
      this._commitIntervalValue = Number(result?.commit_interval ?? 5);
      this._autoUpdateDataValue = Boolean(result?.auto_update_data ?? true);
      this._repackAfterManualPurgeValue = Boolean(result?.repack_after_manual_purge ?? false);
      this._notifyOnPurgeFinishValue = Boolean(result?.notify_on_purge_finish ?? true);
      this._manualExclusionsValue = result?.manual_exclusions_yaml || "";
      this._pruneBulkSelection();
      try {
        this._migration = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/get_migration_status" });
      } catch (_err) {}
      try {
        this._purgeProgress = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/get_purge_progress" });
        this._purgeHistory = this._purgeProgress?.history || [];
        if (this._purgeProgress?.active) {
          this._collapsedSections.delete("maintenance-history");
          this._startPurgeProgressPolling();
        } else if (
          ["completed", "error", "interrupted"].includes(this._purgeProgress?.status)
          && this._purgeProgress?.job_id
          && this._lastHandledPurgeJobId !== this._purgeProgress.job_id
        ) {
          this._lastHandledPurgeJobId = this._purgeProgress.job_id;
          if (["error", "interrupted"].includes(this._purgeProgress.status)) {
            this._collapsedSections.delete("maintenance-history");
          }
          await this._handlePurgeFinished(this._purgeProgress);
        }
      } catch (_err) {}
      this._render();
      if (this._isDataStale() && !this._analyzing && !this._isPurgeBusy()) {
        this._analyzeDb();
      }
    } catch (err) {
      this._message = `Error loading data: ${err?.message || err}`;
      this._render();
    }
  }

  async _refreshTreeData() {
    const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/get_tree" });
    this._data = result;
    this._migration = result?.migration || this._migration || {};
    this._keepDaysValue = Number(result?.purge_keep_days || this._keepDaysValue || 10);
    this._autoPurgeValue = Boolean(result?.auto_purge ?? this._autoPurgeValue ?? true);
    this._autoRepackValue = Boolean(result?.auto_repack ?? this._autoRepackValue ?? true);
    if (!this._autoPurgeValue) this._autoRepackValue = false;
    this._commitIntervalValue = Number(result?.commit_interval ?? this._commitIntervalValue ?? 5);
    this._autoUpdateDataValue = Boolean(result?.auto_update_data ?? this._autoUpdateDataValue ?? true);
    this._repackAfterManualPurgeValue = Boolean(result?.repack_after_manual_purge ?? this._repackAfterManualPurgeValue ?? false);
    this._notifyOnPurgeFinishValue = Boolean(result?.notify_on_purge_finish ?? this._notifyOnPurgeFinishValue ?? true);
    this._manualExclusionsValue = result?.manual_exclusions_yaml ?? this._manualExclusionsValue ?? "";
    this._pruneBulkSelection();
  }

  _formatNumber(value) {
    return new Intl.NumberFormat("en-US").format(Number(value || 0));
  }

  _restoreCollapsedSections() {
    try {
      if (typeof localStorage === "undefined") return;
      const stored = JSON.parse(localStorage.getItem("gui-recorder:collapsed-sections:v1") || "null");
      if (Array.isArray(stored)) {
        this._collapsedSections = new Set(stored.filter((section) => this._collapsibleSections.includes(section)));
      }
    } catch (_err) { /* Keep safe defaults when browser storage is unavailable. */ }
  }

  _saveCollapsedSections() {
    try {
      if (typeof localStorage !== "undefined") {
        localStorage.setItem("gui-recorder:collapsed-sections:v1", JSON.stringify([...this._collapsedSections]));
      }
    } catch (_err) { /* Collapsing must still work without persistent storage. */ }
  }

  _toggleSection(section) {
    if (this._collapsedSections.has(section)) this._collapsedSections.delete(section);
    else this._collapsedSections.add(section);
    this._saveCollapsedSections();
    this._render();
  }

  _setAllSections(collapsed) {
    this._collapsedSections = collapsed ? new Set(this._collapsibleSections) : new Set();
    if (this._purgeProgress?.active) this._collapsedSections.delete("maintenance-history");
    this._saveCollapsedSections();
    this._render();
  }

  _sectionToggleMarkup(section, title, summary) {
    const collapsed = this._collapsedSections.has(section);
    return `<button class="section-toggle" data-collapse-section="${this._escapeHtml(section)}" aria-expanded="${collapsed ? "false" : "true"}">
      <span class="section-caret ${collapsed ? "" : "expanded"}">›</span>
      <span class="section-title">${this._escapeHtml(title)}</span>
      ${summary ? `<span class="section-summary">${this._escapeHtml(summary)}</span>` : ""}
    </button>`;
  }

  _formatBytes(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = bytes;
    let unitIndex = 0;
    while (size >= 1024 && unitIndex < units.length - 1) {
      size /= 1024;
      unitIndex += 1;
    }
    const decimals = unitIndex === 0 ? 0 : size >= 10 ? 1 : 2;
    return `${size.toFixed(decimals)} ${units[unitIndex]}`;
  }

  _formatPercent(part, total) {
    if (!total) return "0%";
    return `${((part / total) * 100).toFixed(1)}%`;
  }

  _formatDate(value) {
    if (!value) return "—";
    try {
      return new Date(value).toLocaleString("en-US");
    } catch (_err) {
      return value;
    }
  }

  _formatDuration(startedAt, finishedAt = null) {
    if (!startedAt) return "—";
    const start = new Date(startedAt).getTime();
    const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
    if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
    const seconds = Math.max(0, Math.round((end - start) / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    if (hours) return `${hours}h ${minutes}m ${remainder}s`;
    if (minutes) return `${minutes}m ${remainder}s`;
    return `${remainder}s`;
  }

  _isDataStale() {
    const generatedAt = this._data?.stats?.generated_at;
    if (!generatedAt) return true;
    try {
      const ts = new Date(generatedAt).getTime();
      if (Number.isNaN(ts)) return true;
      return (Date.now() - ts) > 24 * 60 * 60 * 1000;
    } catch (_err) {
      return true;
    }
  }

  _shouldFlagUpdate() {
    if (this._analyzing) return false;
    return this._isDataStale() || this._refreshRecommended;
  }

  _isPurgeBusy() {
    if (this._analyzing || this._purgingAll || this._purgingExcluded || this._bulkPurging || this._data?.repack_in_progress || this._purgeProgress?.active) return true;
    for (const key of this._pending) {
      if (String(key).startsWith("purge:")) return true;
      if (String(key).startsWith("purge-device:")) return true;
    }
    return false;
  }

  _purgeProgressMessage(progress = this._purgeProgress) {
    if (!progress) return "";
    const label = progress.label || "selected history";
    const total = Number(progress.total_rows || 0);
    const remaining = Number(progress.remaining_rows || 0);
    const deleted = Number(progress.deleted_rows || 0);
    if (progress.status === "queued") return `Queued ${label}…`;
    if (progress.status === "counting") return `Counting stored history for ${label}…`;
    if (progress.status === "verifying") return `All tracked records for ${label} are gone. Verifying Home Assistant's final queued batch…`;
    if (progress.status === "repacking") return `Purge batches completed for ${label}. Repacking the database…`;
    if (progress.status === "analyzing") return `Purge batches completed for ${label}. Updating database analysis…`;
    if (progress.status === "finalizing") return `Saving the maintenance result for ${label}…`;
    if (progress.status === "error") return `Purge monitoring stopped: ${progress.error || "unknown error"}`;
    if (progress.status === "interrupted") return `Purge monitoring was interrupted: ${progress.error || "Home Assistant restarted"}`;
    if ((progress.active || progress.status === "purging") && progress.determinate === false) {
      return `Purging ${label}… Home Assistant determines the age-based batches.`;
    }
    if (progress.active || progress.status === "purging") {
      return `Purging ${label}: ${this._formatNumber(deleted)} of ${this._formatNumber(total)} pre-existing records deleted (${this._formatNumber(remaining)} remaining).`;
    }
    return "";
  }

  async _pollPurgeProgress() {
    try {
      const progress = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/get_purge_progress" });
      this._purgeProgress = progress;
      this._purgeHistory = progress?.history || this._purgeHistory;
      if (progress?.active) {
        this._purgeProgressSeenActive = true;
        this._collapsedSections.delete("maintenance-history");
      }
      const message = this._purgeProgressMessage(progress);
      if (message) this._message = message;
      this._render();
      const terminal = ["completed", "error", "interrupted"].includes(progress?.status);
      if (!progress?.active && terminal && progress?.job_id && this._lastHandledPurgeJobId !== progress.job_id) {
        this._lastHandledPurgeJobId = progress.job_id;
        await this._handlePurgeFinished(progress);
        return;
      }
      if (
        !progress?.active
        && (this._purgeProgressSeenActive || Date.now() >= this._purgeProgressStartDeadline)
      ) {
        this._stopPurgeProgressPolling();
      }
    } catch (_err) {
      // A transient polling failure must not start another purge or clear the
      // busy state. The main purge request reports the authoritative result.
    }
  }

  _startPurgeProgressPolling() {
    if (this._purgeProgressTimer) return;
    this._purgeProgressSeenActive = Boolean(this._purgeProgress?.active);
    this._purgeProgressStartDeadline = Date.now() + 15000;
    this._purgeProgressTimer = setInterval(() => this._pollPurgeProgress(), 1000);
    this._pollPurgeProgress();
  }

  _stopPurgeProgressPolling() {
    if (this._purgeProgressTimer) clearInterval(this._purgeProgressTimer);
    this._purgeProgressTimer = null;
    this._purgeProgressSeenActive = false;
    this._purgeProgressStartDeadline = 0;
  }

  async _handlePurgeFinished(progress) {
    this._stopPurgeProgressPolling();
    this._purgingAll = false;
    this._purgingExcluded = false;
    this._bulkPurging = false;
    for (const key of [...this._pending]) {
      if (String(key).startsWith("purge:")) this._pending.delete(key);
      if (String(key).startsWith("purge-device:")) this._pending.delete(key);
    }
    if (progress.status === "completed") {
      if (progress.kind === "selected") this._bulkSelected.clear();
      const deletedText = progress.determinate === false
        ? "completed"
        : `completed: ${this._formatNumber(progress.deleted_rows || 0)} records deleted`;
      this._message = `${progress.label || "Purge"} ${deletedText} in ${this._formatDuration(progress.started_at, progress.finished_at)}.`;
      if (progress.stats_refreshed) {
        this._dataMessage = "Data updated.";
        this._refreshRecommended = false;
        try {
          await this._refreshTreeData();
        } catch (err) {
          this._message += ` Could not refresh the entity list: ${err?.message || err}`;
        }
      } else {
        this._dataMessage = "Purge completed. Update data to refresh database statistics.";
        this._refreshRecommended = true;
      }
      if (progress.warning) this._message += ` ${progress.warning}`;
    } else {
      this._collapsedSections.delete("maintenance-history");
      this._saveCollapsedSections();
      this._message = `${progress.label || "Purge"} ${progress.status}: ${progress.error || "unknown error"}`;
      this._refreshRecommended = true;
    }
    this._render();
  }

  _confirmPurgePreview(details) {
    if (this._purgePreviewResolver) this._purgePreviewResolver(false);
    this._purgePreview = details;
    this._render();
    return new Promise((resolve) => {
      this._purgePreviewResolver = resolve;
    });
  }

  _resolvePurgePreview(confirmed) {
    const resolve = this._purgePreviewResolver;
    this._purgePreviewResolver = null;
    this._purgePreview = null;
    this._render();
    if (resolve) resolve(Boolean(confirmed));
  }

  _isMigrationPending() {
    const m = this._migration || {};
    return m.legacy_detected === true && !m.gui_include_active;
  }

  _entityMatches(entity, category = "entity") {
    if (!this._quickFilterMatches(entity, category)) return false;
    if (!this._filter) return true;
    const text = `${entity.entity_id} ${entity.name} ${entity.platform} ${entity.domain}`.toLowerCase();
    return text.includes(this._filter.toLowerCase());
  }

  _quickFilterMatches(entity, category = "entity") {
    if (this._quickFilter === "all") return true;
    if (this._quickFilter === "has_records") return Number(entity.record_count || 0) > 0;
    if (this._quickFilter === "excluded") return entity.recorded === false;
    if (this._quickFilter === "large") return Number(entity.record_count || 0) >= 100000;
    if (this._quickFilter === "orphans") return category === "orphan";
    if (this._quickFilter === "obsolete") return category === "obsolete";
    return true;
  }

  _deviceMatches(device) {
    const candidates = (device.entities || []).filter((entity) => this._quickFilterMatches(entity, "device"));
    if (!candidates.length) return false;
    if (!this._filter) return true;
    const own = `${device.name} ${device.manufacturer || ""} ${device.model || ""}`.toLowerCase();
    if (own.includes(this._filter.toLowerCase())) return true;
    return candidates.some((e) => this._entityMatches(e, "device"));
  }

  _deviceVisibleEntities(device) {
    const candidates = (device.entities || []).filter((entity) => this._quickFilterMatches(entity, "device"));
    if (!this._filter) return this._sortEntities(candidates);
    const own = `${device.name} ${device.manufacturer || ""} ${device.model || ""}`.toLowerCase();
    if (own.includes(this._filter.toLowerCase())) return this._sortEntities(candidates);
    return this._sortEntities(candidates.filter((entity) => this._entityMatches(entity, "device")));
  }

  _sortEntities(entities) {
    return [...entities].sort((a, b) => {
      if (this._sortMode === "name") return String(a.name || a.entity_id).localeCompare(String(b.name || b.entity_id));
      if (this._sortMode === "entity") return String(a.entity_id).localeCompare(String(b.entity_id));
      if (this._sortMode === "domain") return String(a.domain || "").localeCompare(String(b.domain || "")) || String(a.entity_id).localeCompare(String(b.entity_id));
      return Number(b.record_count || 0) - Number(a.record_count || 0) || String(a.entity_id).localeCompare(String(b.entity_id));
    });
  }

  _sortDevices(devices) {
    return [...devices].sort((a, b) => {
      if (this._sortMode === "name" || this._sortMode === "entity" || this._sortMode === "domain") {
        return String(a.name || "").localeCompare(String(b.name || ""));
      }
      return Number(b.record_count || 0) - Number(a.record_count || 0) || String(a.name || "").localeCompare(String(b.name || ""));
    });
  }

  _pageItems(items, section) {
    const pageCount = Math.max(1, Math.ceil(items.length / this._pageSize));
    const page = Math.min(Math.max(1, Number(this._pages[section] || 1)), pageCount);
    this._pages[section] = page;
    const start = (page - 1) * this._pageSize;
    return { items: items.slice(start, start + this._pageSize), page, pageCount };
  }

  _paginationMarkup(section, page, pageCount, total) {
    if (pageCount <= 1) return "";
    return `<div class="pagination">
      <button class="mini-button" data-page-section="${section}" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>Previous</button>
      <span class="subtle">Page ${page} of ${pageCount} · ${this._formatNumber(total)} items</span>
      <button class="mini-button" data-page-section="${section}" data-page="${page + 1}" ${page >= pageCount ? "disabled" : ""}>Next</button>
    </div>`;
  }

  _resetPages() {
    this._pages = { devices: 1, orphans: 1, obsolete: 1 };
  }

  _deviceState(device) {
    const visibleEntities = device.entities.filter((e) => this._entityMatches(e));
    const entities = visibleEntities.length ? visibleEntities : device.entities;
    const total = entities.length;
    const active = entities.filter((e) => e.recorded).length;
    if (active === 0) return { mode: "off", all: false, some: false, label: "OFF" };
    if (active === total) return { mode: "on", all: true, some: false, label: "ON" };
    return { mode: "mixed", all: false, some: true, label: `${active}/${total}` };
  }

  _allPurgeEntities() {
    const entities = new Map();
    for (const device of this._data.devices || []) {
      for (const entity of device.entities || []) entities.set(entity.entity_id, entity);
    }
    for (const entity of this._data.orphans || []) entities.set(entity.entity_id, entity);
    for (const entity of this._data.obsolete || []) entities.set(entity.entity_id, entity);
    return entities;
  }

  _pruneBulkSelection() {
    if (!this._bulkSelected) this._bulkSelected = new Set();
    const available = this._allPurgeEntities();
    this._bulkSelected = new Set(
      [...this._bulkSelected].filter((entityId) => Number(available.get(entityId)?.record_count || 0) > 0),
    );
  }

  _bulkSelectionSummary() {
    const available = this._allPurgeEntities();
    let records = 0;
    const entityIds = [];
    for (const entityId of this._bulkSelected) {
      const entity = available.get(entityId);
      if (!entity || Number(entity.record_count || 0) <= 0) continue;
      entityIds.push(entityId);
      records += Number(entity.record_count || 0);
    }
    return { entityIds: entityIds.sort(), records };
  }

  _bulkDeviceState(device) {
    const entities = (device.entities || []).filter((entity) => Number(entity.record_count || 0) > 0);
    const selected = entities.filter((entity) => this._bulkSelected.has(entity.entity_id)).length;
    return {
      checked: entities.length > 0 && selected === entities.length,
      indeterminate: selected > 0 && selected < entities.length,
      disabled: entities.length === 0,
    };
  }

  _setBulkMode(enabled) {
    this._bulkMode = Boolean(enabled);
    if (!this._bulkMode) this._bulkSelected.clear();
    this._render();
  }

  _setBulkEntity(entityId, selected) {
    if (selected) this._bulkSelected.add(entityId);
    else this._bulkSelected.delete(entityId);
    this._render();
  }

  _setBulkDevice(deviceId, selected) {
    const device = (this._data.devices || []).find((item) => item.device_id === deviceId);
    if (!device) return;
    for (const entity of device.entities || []) {
      if (Number(entity.record_count || 0) <= 0) continue;
      if (selected) this._bulkSelected.add(entity.entity_id);
      else this._bulkSelected.delete(entity.entity_id);
    }
    this._render();
  }

  _selectBulkVisible(entityIds) {
    const available = this._allPurgeEntities();
    for (const entityId of entityIds) {
      if (Number(available.get(entityId)?.record_count || 0) > 0) this._bulkSelected.add(entityId);
    }
    this._render();
  }

  _setBulkVisible(entityIds, selected) {
    const available = this._allPurgeEntities();
    for (const entityId of entityIds) {
      if (selected && Number(available.get(entityId)?.record_count || 0) > 0) this._bulkSelected.add(entityId);
      if (!selected) this._bulkSelected.delete(entityId);
    }
    this._render();
  }

  _clearBulkSelection() {
    this._bulkSelected.clear();
    this._render();
  }

  _toggleExpanded(deviceId) {
    if (this._expanded.has(deviceId)) this._expanded.delete(deviceId);
    else this._expanded.add(deviceId);
    this._render();
  }

  _setExpanded(deviceId, expanded) {
    if (expanded) this._expanded.add(deviceId);
    else this._expanded.delete(deviceId);
  }

  async _setEntity(entityId, recorded, deviceId = null) {
    this._pending.add(entityId);
    if (deviceId) this._expanded.add(deviceId);
    this._render();
    try {
      await this._hass.connection.sendMessagePromise({ type: "gui_recorder/set_entity", entity_id: entityId, recorded });
      this._message = "Changes saved to gui_recorder.yaml. Restart Home Assistant to apply them.";
      await this._load();
    } catch (err) {
      this._message = `Could not save ${entityId}: ${err?.message || err}`;
      this._render();
    } finally {
      this._pending.delete(entityId);
      this._render();
    }
  }

  async _setDevice(deviceId, recorded) {
    this._pending.add(`device:${deviceId}`);
    this._render();
    try {
      await this._hass.connection.sendMessagePromise({ type: "gui_recorder/set_device", device_id: deviceId, recorded });
      this._message = "Changes saved to gui_recorder.yaml. Restart Home Assistant to apply them.";
      await this._load();
    } catch (err) {
      this._message = `Could not save the device: ${err?.message || err}`;
      this._render();
    } finally {
      this._pending.delete(`device:${deviceId}`);
      this._render();
    }
  }

  async _setEntitiesBulk(entityIds, recorded, busyKey) {
    if (!entityIds.length || this._pending.has(busyKey)) return;
    const verb = recorded ? "Enabling" : "Disabling";
    if (!confirm(`${verb} recording for ${entityIds.length} entities. Continue?`)) return;
    this._pending.add(busyKey);
    this._render();
    try {
      await this._hass.connection.sendMessagePromise({ type: "gui_recorder/set_entities_bulk", entity_ids: entityIds, recorded });
      this._message = `${recorded ? "Enabled" : "Disabled"} recording for ${entityIds.length} entities. Restart Home Assistant to apply them.`;
      await this._load();
    } catch (err) {
      this._message = `Could not update entities: ${err?.message || err}`;
      this._render();
    } finally {
      this._pending.delete(busyKey);
      this._render();
    }
  }

  async _analyzeDb() {
    if (this._isPurgeBusy()) return;
    this._analyzing = true;
    this._dataMessage = "Updating data…";
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/analyze_db" });
      if (result?.stats?.error) this._dataMessage = `Could not update data: ${result.stats.error}`;
      else this._dataMessage = "Data updated.";
      this._refreshRecommended = false;
      await this._load();
    } catch (err) {
      this._dataMessage = `Error updating data: ${err?.message || err}`;
      this._render();
    } finally {
      this._analyzing = false;
      this._render();
    }
  }

  async _setAutoUpdateData(value) {
    const newValue = Boolean(value);
    const previous = this._autoUpdateDataValue;
    this._autoUpdateDataValue = newValue;
    this._togglingAutoUpdate = true;
    this._render();
    try {
      await this._hass.connection.sendMessagePromise({ type: "gui_recorder/set_auto_update_data", auto_update_data: newValue });
    } catch (err) {
      this._autoUpdateDataValue = previous;
      this._message = `Error updating auto-update setting: ${err?.message || err}`;
    } finally {
      this._togglingAutoUpdate = false;
      this._render();
    }
  }

  async _setRepackAfterManualPurge(value) {
    const newValue = Boolean(value);
    const previous = this._repackAfterManualPurgeValue;
    this._repackAfterManualPurgeValue = newValue;
    this._togglingRepack = true;
    this._render();
    try {
      await this._hass.connection.sendMessagePromise({ type: "gui_recorder/set_repack_after_manual_purge", repack_after_manual_purge: newValue });
    } catch (err) {
      this._repackAfterManualPurgeValue = previous;
      this._message = `Error updating repack setting: ${err?.message || err}`;
    } finally {
      this._togglingRepack = false;
      this._render();
    }
  }

  async _setNotifyOnPurgeFinish(value) {
    const newValue = Boolean(value);
    const previous = this._notifyOnPurgeFinishValue;
    this._notifyOnPurgeFinishValue = newValue;
    this._togglingNotifications = true;
    this._render();
    try {
      await this._hass.connection.sendMessagePromise({
        type: "gui_recorder/set_notify_on_purge_finish",
        notify_on_purge_finish: newValue,
      });
    } catch (err) {
      this._notifyOnPurgeFinishValue = previous;
      this._message = `Error updating notification setting: ${err?.message || err}`;
    } finally {
      this._togglingNotifications = false;
      this._render();
    }
  }


  async _saveGlobalOptions() {
    const keepDays = Number(this._keepDaysValue);
    const commitInterval = Number(this._commitIntervalValue);
    if (!Number.isInteger(keepDays) || keepDays < 1) {
      this._message = "Retention days must be an integer greater than or equal to 1.";
      this._render();
      return;
    }
    if (!Number.isInteger(commitInterval) || commitInterval < 0) {
      this._message = "Commit interval must be an integer greater than or equal to 0.";
      this._render();
      return;
    }
    this._savingGlobal = true;
    this._render();
    try {
      await this._hass.connection.sendMessagePromise({
        type: "gui_recorder/set_global_options",
        purge_keep_days: keepDays,
        auto_purge: Boolean(this._autoPurgeValue),
        auto_repack: Boolean(this._autoPurgeValue) ? Boolean(this._autoRepackValue) : false,
        commit_interval: commitInterval,
      });
      this._message = "Global configuration saved to gui_recorder.yaml. Restart Home Assistant to apply it.";
      await this._load();
    } catch (err) {
      this._message = `Could not save global configuration: ${err?.message || err}`;
      this._render();
    } finally {
      this._savingGlobal = false;
      this._render();
    }
  }

  async _saveManualExclusions() {
    this._manualExclusionsError = "";
    this._savingManualExclusions = true;
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "gui_recorder/set_manual_exclusions",
        yaml_text: this._manualExclusionsValue || "",
      });
      if (result?.ok) {
        this._message = result.changed
          ? "Manual exclusions saved to gui_recorder.yaml. Restart Home Assistant to apply them."
          : "No changes to save.";
        await this._load();
      } else {
        this._manualExclusionsError = result?.error || "Could not save the manual exclusions.";
      }
    } catch (err) {
      this._manualExclusionsError = err?.message || String(err);
    } finally {
      this._savingManualExclusions = false;
      this._render();
    }
  }

  async _purgeAll() {
    if (this._isPurgeBusy()) return;
    const confirmed = await this._confirmPurgePreview({
      title: "Purge old recorder data",
      description: `Delete recorder data older than ${this._keepDaysValue} day${Number(this._keepDaysValue) === 1 ? "" : "s"}. Recent data inside the retention window is kept.`,
      records: Number(this._data?.stats?.total_rows || 0),
      recordsLabel: "Current database records (only older rows will be deleted)",
      repack: Boolean(this._repackAfterManualPurgeValue),
      warning: "The exact number of age-based rows is determined by Home Assistant during the purge.",
    });
    if (!confirmed) return;
    this._purgingAll = true;
    this._message = "Starting recorder retention purge…";
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/purge_all" });
      this._purgeProgress = result?.job || this._purgeProgress;
      this._lastHandledPurgeJobId = null;
      this._message = `Background purge started (${this._purgeProgress?.job_id || "job pending"}). You may close or reload this page.`;
      this._startPurgeProgressPolling();
    } catch (err) {
      this._purgingAll = false;
      this._message = `Global purge error: ${err?.message || err}`;
    }
    this._render();
  }

  async _purgeExcludedEntities() {
    if (this._isPurgeBusy()) return;
    const excludedCount = Number(this._data?.stats?.configured_exclusions || 0);
    if (!excludedCount) return;
    const counts = this._data?.stats?.entity_counts || {};
    const excludedRows = (this._data?.matched_exclusions || []).reduce((sum, entityId) => sum + Number(counts[entityId] || 0), 0);
    const confirmed = await this._confirmPurgePreview({
      title: "Purge excluded entities",
      description: "Permanently delete the complete stored state history for every explicitly excluded entity.",
      entities: excludedCount,
      records: excludedRows,
      percent: this._formatPercent(excludedRows, Number(this._data?.stats?.total_rows || 0)),
      repack: Boolean(this._repackAfterManualPurgeValue),
      warning: "This ignores the global retention period and cannot be undone.",
    });
    if (!confirmed) return;
    this._purgingExcluded = true;
    this._message = "Starting excluded-entity purge…";
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/purge_excluded_entities" });
      this._purgeProgress = result?.job || this._purgeProgress;
      this._lastHandledPurgeJobId = null;
      this._message = `Background purge started for ${excludedCount} excluded entities. You may close or reload this page.`;
      this._startPurgeProgressPolling();
    } catch (err) {
      this._purgingExcluded = false;
      this._message = `Error starting excluded-entity purge: ${err?.message || err}`;
    }
    this._render();
  }


  async _purgeEntity(entityId, deviceId = null) {
    if (this._isPurgeBusy()) return;
    const entity = this._allPurgeEntities().get(entityId);
    const records = Number(entity?.record_count || 0);
    const confirmed = await this._confirmPurgePreview({
      title: `Purge ${entityId}`,
      description: "Permanently delete the complete stored state history for this entity.",
      entities: 1,
      records,
      percent: this._formatPercent(records, Number(this._data?.stats?.total_rows || 0)),
      repack: false,
      warning: "This cannot be undone.",
    });
    if (!confirmed) return;
    this._pending.add(`purge:${entityId}`);
    if (deviceId) this._expanded.add(deviceId);
    this._message = `Starting purge for ${entityId}…`;
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/purge_entity", entity_id: entityId });
      this._purgeProgress = result?.job || this._purgeProgress;
      this._lastHandledPurgeJobId = null;
      this._message = `Background purge started for ${entityId}. You may close or reload this page.`;
      this._startPurgeProgressPolling();
    } catch (err) {
      this._pending.delete(`purge:${entityId}`);
      this._message = `Error starting purge for ${entityId}: ${err?.message || err}`;
    }
    this._render();
  }

  async _purgeDevice(deviceId) {
    if (this._isPurgeBusy()) return;
    const device = (this._data.devices || []).find((d) => d.device_id === deviceId);
    const deviceName = device?.name || deviceId;
    const records = Number(device?.record_count || 0);
    const confirmed = await this._confirmPurgePreview({
      title: `Purge device: ${deviceName}`,
      description: "Permanently delete the complete stored state history for every entity belonging to this device.",
      entities: Number(device?.entities?.length || 0),
      records,
      percent: this._formatPercent(records, Number(this._data?.stats?.total_rows || 0)),
      repack: false,
      warning: "This cannot be undone.",
    });
    if (!confirmed) return;
    this._pending.add(`purge-device:${deviceId}`);
    this._message = `Starting purge for ${deviceName}…`;
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/purge_device", device_id: deviceId });
      this._purgeProgress = result?.job || this._purgeProgress;
      this._lastHandledPurgeJobId = null;
      this._message = `Background purge started for ${deviceName}. You may close or reload this page.`;
      this._startPurgeProgressPolling();
    } catch (err) {
      this._pending.delete(`purge-device:${deviceId}`);
      this._message = `Error starting purge for ${deviceName}: ${err?.message || err}`;
    }
    this._render();
  }

  async _purgeSelected() {
    if (this._isPurgeBusy()) return;
    const { entityIds, records } = this._bulkSelectionSummary();
    if (!entityIds.length) return;
    const entityWord = entityIds.length === 1 ? "entity" : "entities";
    const confirmed = await this._confirmPurgePreview({
      title: "Purge selected entities",
      description: `Permanently delete the complete stored state history for ${entityIds.length} selected ${entityWord}.`,
      entities: entityIds.length,
      records,
      percent: this._formatPercent(records, Number(this._data?.stats?.total_rows || 0)),
      repack: false,
      warning: "This cannot be undone.",
    });
    if (!confirmed) return;

    this._bulkPurging = true;
    this._message = `Purging ${entityIds.length} selected ${entityWord}…`;
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({
        type: "gui_recorder/purge_selected",
        entity_ids: entityIds,
      });
      this._purgeProgress = result?.job || this._purgeProgress;
      this._lastHandledPurgeJobId = null;
      this._message = `Background purge started for ${entityIds.length} selected ${entityWord}. You may close or reload this page.`;
      this._startPurgeProgressPolling();
    } catch (err) {
      this._bulkPurging = false;
      this._message = `Error starting selected-entity purge: ${err?.message || err}`;
    }
    this._render();
  }


  async _restartHomeAssistant() {
    if (!confirm("Home Assistant will restart now. Continue?")) return;
    this._message = "Restart request sent to Home Assistant…";
    this._render();
    try {
      await this._hass.connection.sendMessagePromise({ type: "gui_recorder/restart_homeassistant" });
      this._message = "Restart requested. The interface may disconnect for a moment.";
    } catch (err) {
      this._message = `Could not restart Home Assistant: ${err?.message || err}`;
    }
    this._render();
  }





  async _removeExclusion(entityId) {
    if (!confirm(`Remove ${entityId} from the configured exclusions?`)) return;
    this._pending.add(`remove:${entityId}`);
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/remove_exclusion", entity_id: entityId });
      await this._refreshTreeData();
      this._message = result?.removed
        ? `${entityId} removed from configured exclusions. Restart Home Assistant to apply the change.`
        : `Could not remove ${entityId} from configured exclusions.`;
      this._render();
    } catch (err) {
      this._message = `Could not remove ${entityId}: ${err?.message || err}`;
      this._render();
    } finally {
      this._pending.delete(`remove:${entityId}`);
      this._render();
    }
  }

  async _removeUnmatchedExclusions() {
    if (!confirm("Remove all unmatched exclusions from the configuration?")) return;
    this._pending.add("remove-unmatched");
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "gui_recorder/remove_unmatched_exclusions" });
      const removed = result?.removed || [];
      await this._refreshTreeData();
      this._message = removed.length
        ? `${removed.length} unmatched exclusions removed. Restart Home Assistant to apply the change.`
        : "No unmatched exclusions were found.";
      this._render();
    } catch (err) {
      this._message = `Could not remove unmatched exclusions: ${err?.message || err}`;
      this._render();
    } finally {
      this._pending.delete("remove-unmatched");
      this._render();
    }
  }

  _migrationSummaryLines() {
    const s = this._migration?.legacy_summary || {};
    const lines = [];
    if (!this._migration?.legacy_detected) return lines;
    if (s.auto_purge != null) lines.push(`auto_purge: ${s.auto_purge}`);
    if (s.auto_repack != null) lines.push(`auto_repack: ${s.auto_repack}`);
    if (s.purge_keep_days != null) lines.push(`purge_keep_days: ${s.purge_keep_days}`);
    if (s.commit_interval != null) lines.push(`commit_interval: ${s.commit_interval}`);
    if (s.db_url) lines.push(`db_url: ${s.db_url}`);
    if (s.exclude_entities_count) lines.push(`exclude.entities: ${s.exclude_entities_count}`);
    if (s.exclude_domains_count) lines.push(`exclude.domains: ${s.exclude_domains_count}`);
    if (s.exclude_globs_count) lines.push(`exclude.entity_globs: ${s.exclude_globs_count}`);
    if (s.include_entities_count) lines.push(`include.entities: ${s.include_entities_count}`);
    if (s.include_domains_count) lines.push(`include.domains: ${s.include_domains_count}`);
    if (s.include_globs_count) lines.push(`include.entity_globs: ${s.include_globs_count}`);
    return lines;
  }

  async _migrationAction(type) {
    this._migrationBusy = true;
    this._render();
    try {
      const result = await this._hass.connection.sendMessagePromise({ type });
      if (result?.ok) {
        if (type === "gui_recorder/import_legacy") {
          const imported = result.imported || {};
          const manualNote = imported.manual_exclusions
            ? " Unsupported filters (domains/entity_globs/event_types) were copied into the Manual exclusions field — review them there."
            : "";
          const dbNote = imported.db_url
            ? ` Custom database location preserved (${imported.db_url}).`
            : "";
          this._message = `Imported from existing configuration: ${imported.excluded_entities || 0} excluded entities and global recorder parameters.${dbNote}${manualNote}`;
        } else if (type === "gui_recorder/disable_legacy") {
          this._message = "Previous recorder configuration disabled in configuration.yaml.";
        } else if (type === "gui_recorder/enable_gui") {
          this._message = "GUI Recorder enabled in configuration.yaml.";
        }
      } else {
        this._message = result?.error || "Could not complete the migration action.";
      }
      await this._load();
    } catch (err) {
      this._message = `Migration error: ${err?.message || err}`;
      this._render();
    } finally {
      this._migrationBusy = false;
      this._render();
    }
  }

  _migrationCardMarkup() {
    const m = this._migration || {};
    const lines = this._migrationSummaryLines();
    const hasUnsupported = ((m.legacy_summary?.exclude_domains_count || 0) + (m.legacy_summary?.exclude_globs_count || 0) + (m.legacy_summary?.exclude_event_types_count || 0) + (m.legacy_summary?.include_entities_count || 0) + (m.legacy_summary?.include_domains_count || 0) + (m.legacy_summary?.include_globs_count || 0)) > 0;
    const includeEntitiesLost = (m.legacy_summary?.include_entities_count || 0) > 0;
    // Steps 2/3 must not run before the import: they would replace the user's
    // recorder config (exclusions, retention, a custom db_url) with defaults.
    const importRequired = m.legacy_detected === true && !m.legacy_imported_at;
    if (!m.legacy_detected && m.gui_ready) return '';

    return `
      <div class="card migration">
        <h2>Migration / configuration conflicts</h2>
        <div class="message warn"><strong>⚠ Back up Home Assistant before proceeding.</strong> Steps 2 and 3 modify <code>configuration.yaml</code> and require a restart. Step 1 is non-destructive.</div>
        ${m.gui_ready ? `<div class="message ok">GUI Recorder is enabled and no active configuration conflicts were detected.</div>` : ''}
        ${m.legacy_detected ? `
          <div class="message warn"><strong>Existing recorder configuration detected.</strong> ${m.legacy_source_path ? `Source: <code>${this._escapeHtml(m.legacy_source_path)}</code>.` : ''}</div>
          ${lines.length ? `<div class="row-note" style="margin:10px 0 12px;">Detected values: ${lines.map((l) => `<code>${this._escapeHtml(l)}</code>`).join(' · ')}</div>` : ''}
          ${hasUnsupported ? `<div class="message warn"><strong>Some legacy filters are not entity-level.</strong> <code>exclude.domains</code>, <code>exclude.entity_globs</code>, <code>exclude.event_types</code>, <code>include.domains</code> and <code>include.entity_globs</code> are copied automatically into the "Manual exclusions" field below when you import — review them there before completing migration.${includeEntitiesLost ? ` <code>include.entities</code> (allow-list mode) is not supported by GUI Recorder and will be lost.` : ''}</div>` : ''}
        ` : `<div class="message">No legacy recorder configuration was detected for import.</div>`}

        <div class="steps">
          <div class="step ${m.legacy_imported_at || !m.legacy_detected ? 'done' : ''}">
            <div class="step-title">Step 1: Import existing configuration${m.legacy_imported_at || !m.legacy_detected ? '<span class="step-check" aria-label="completed">✓</span>' : ''}</div>
            <div class="row-note">${m.legacy_imported_at ? `Imported on ${this._escapeHtml(m.legacy_imported_at)}.` : 'Use the legacy configuration as the starting point for GUI Recorder.'}</div>
            <button class="action-button" id="migration-import" ${!m.legacy_detected || m.legacy_imported_at || this._migrationBusy ? 'disabled' : ''}>Import detected configuration</button>
          </div>

          <div class="step ${m.legacy_active ? '' : 'done'}">
            <div class="step-title">Step 2: Disable previous recorder config${!m.legacy_active ? '<span class="step-check" aria-label="completed">✓</span>' : ''}</div>
            <div class="row-note">Comment the active <code>recorder:</code> block in <code>configuration.yaml</code> and create an automatic backup.</div>
            ${importRequired ? `<div class="row-note" style="margin-bottom:8px;"><strong>Complete Step 1 first.</strong> Disabling the legacy block before importing would replace your recorder settings with defaults.</div>` : ''}
            <button class="action-button" id="migration-disable" ${!m.legacy_active || importRequired || this._migrationBusy ? 'disabled' : ''}>Disable current recorder config</button>
          </div>

          <div class="step ${m.gui_include_active ? 'done' : ''}">
            <div class="step-title">Step 3: Enable GUI Recorder${m.gui_include_active ? '<span class="step-check" aria-label="completed">✓</span>' : ''}</div>
            <div class="row-note">Add <code>recorder: !include gui_recorder.yaml</code> if it is missing.</div>
            ${importRequired ? `<div class="row-note" style="margin-bottom:8px;"><strong>Complete Step 1 first.</strong></div>` : ''}
            <button class="action-button" id="migration-enable" ${m.gui_include_active || importRequired || this._migrationBusy ? 'disabled' : ''}>Enable gui_recorder.yaml</button>
          </div>
        </div>
      </div>
    `;
  }

  _escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  _globNote(entity) {
    if (!entity.glob_hidden) return "";
    return entity.recorded
      ? `<div class="row-note">kept by include.entities, above your exclude glob</div>`
      : `<div class="row-note">hidden by one of your manual exclude globs/domains — switch it on here to record it anyway</div>`;
  }

  _switchMarkup({ checked, mixed = false, disabled = false, entityId = "", deviceId = "", kind = "entity" }) {
    return `
      <label class="switch ${mixed ? "mixed" : ""} ${disabled ? "disabled" : ""}">
        <input
          type="checkbox"
          class="switch-input"
          ${checked ? "checked" : ""}
          ${disabled ? "disabled" : ""}
          data-kind="${kind}"
          ${entityId ? `data-entity-id="${this._escapeHtml(entityId)}"` : ""}
          ${deviceId ? `data-device-id="${this._escapeHtml(deviceId)}"` : ""}
        >
        <span class="switch-track"></span>
      </label>
    `;
  }

  _purgeButton(entityId, deviceId = null, disabled = false) {
    return `<button class="mini-button" ${disabled ? "disabled" : ""} data-purge-entity="${this._escapeHtml(entityId)}" ${deviceId ? `data-device-id="${this._escapeHtml(deviceId)}"` : ""}>Purge</button>`;
  }

  _render() {
    const prevFilter = this.shadowRoot?.getElementById?.("filter");
    const filterWasFocused = prevFilter && this.shadowRoot.activeElement === prevFilter;
    const filterSelStart = filterWasFocused ? prevFilter.selectionStart : null;
    const filterSelEnd = filterWasFocused ? prevFilter.selectionEnd : null;

    const filteredDevices = this._sortDevices((this._data.devices || []).filter((d) => this._deviceMatches(d)));
    const filteredOrphans = this._sortEntities((this._data.orphans || []).filter((e) => this._entityMatches(e, "orphan")));
    const visibleDeviceEntityIds = filteredDevices.flatMap((d) => this._deviceVisibleEntities(d).map((e) => e.entity_id));
    const visibleOrphanEntityIds = filteredOrphans.map((e) => e.entity_id);
    const filteredObsolete = this._sortEntities((this._data.obsolete || []).filter((e) => this._entityMatches(e, "obsolete")));
    const devicePage = this._pageItems(filteredDevices, "devices");
    const orphanPage = this._pageItems(filteredOrphans, "orphans");
    const obsoletePage = this._pageItems(filteredObsolete, "obsolete");
    const purgeEntities = this._allPurgeEntities();
    const visiblePurgeEntityIds = [...new Set([
      ...visibleDeviceEntityIds,
      ...visibleOrphanEntityIds,
      ...filteredObsolete.map((e) => e.entity_id),
    ])].filter((entityId) => Number(purgeEntities.get(entityId)?.record_count || 0) > 0);
    const bulkSummary = this._bulkSelectionSummary();
    const selectedDeviceEntities = visibleDeviceEntityIds.filter((entityId) => this._bulkSelected.has(entityId)).length;
    const selectedOrphans = visibleOrphanEntityIds.filter((entityId) => this._bulkSelected.has(entityId)).length;
    const selectedObsolete = filteredObsolete.filter((entity) => this._bulkSelected.has(entity.entity_id)).length;
    const filteredUnmatchedExclusions = (this._data.unmatched_exclusions || []).filter((e) => !this._filter || String(e).toLowerCase().includes(this._filter.toLowerCase()));
    const stats = this._data.stats || {};
    const totalRows = Number(stats.total_rows || 0);
    const purgeActionBusy = this._isPurgeBusy();
    const purgeProgress = this._purgeProgress;
    const purgeElapsedSeconds = purgeProgress?.started_at
      ? Math.max(1, (Date.now() - new Date(purgeProgress.started_at).getTime()) / 1000)
      : 0;
    const purgeRate = purgeElapsedSeconds ? Number(purgeProgress?.deleted_rows || 0) / purgeElapsedSeconds : 0;
    const migrationPending = this._isMigrationPending();

    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; padding:16px; box-sizing:border-box; color:var(--primary-text-color); }
        .wrap { max-width:1500px; margin:0 auto; }
        .toolbar, .card, .notice { background:var(--card-background-color); border-radius:12px; padding:16px; box-shadow:var(--ha-card-box-shadow, none); margin-bottom:16px; border:1px solid var(--divider-color); }
        .card.migration { border-color: var(--warning-color, #ff9800); }
        .notice.pending { border-color:var(--warning-color, #ff9800); background:color-mix(in srgb, var(--card-background-color) 82%, var(--warning-color, #ff9800)); }
        .notice.pending.sticky { position:sticky; top:0; z-index:20; box-shadow:0 4px 12px rgba(0,0,0,0.2); }
        .notice.purge-job { position:sticky; top:8px; z-index:30; border-color:var(--primary-color); box-shadow:0 6px 18px rgba(0,0,0,0.24); }
        .purge-job-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin-top:10px; }
        .purge-job-stat { padding:8px 10px; border-radius:8px; background:var(--primary-background-color); }
        .notice.stale { border-color:var(--warning-color, #ff9800); border-left:6px solid var(--warning-color, #ff9800); background:color-mix(in srgb, var(--card-background-color) 76%, var(--warning-color, #ff9800)); color:var(--primary-text-color); }
        .purge-progress { width:100%; height:10px; margin-top:10px; accent-color:var(--primary-color); }
        .toolbar-head { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }
        .toolbar-actions { display:flex; gap:10px; flex-wrap:wrap; }
        h1, h2, h3 { margin:0 0 12px; }
        h3 { font-size:1rem; }
        input[type="search"], input[type="number"] { width:100%; padding:10px 12px; border-radius:10px; border:1px solid var(--divider-color); background:var(--primary-background-color); color:var(--primary-text-color); box-sizing:border-box; }
        textarea { width:100%; padding:10px 12px; border-radius:10px; border:1px solid var(--divider-color); background:var(--primary-background-color); color:var(--primary-text-color); box-sizing:border-box; font-family:var(--code-font-family, monospace); font-size:0.9rem; resize:vertical; }
        .message { margin-top:12px; color:var(--secondary-text-color); }
        .message.warn { color: var(--warning-color, #ff9800); }
        .message.ok { color: var(--success-color, #43a047); }
        .steps { display:grid; gap:12px; margin-top:12px; }
        .step { padding:12px; border-radius:10px; border:1px solid var(--divider-color); background:var(--primary-background-color); }
        .step.done { opacity:0.85; border-color: var(--success-color, #43a047); }
        .step-title { font-weight:600; margin-bottom:6px; display:flex; align-items:center; gap:8px; }
        .step-check { color: var(--success-color, #43a047); font-weight:700; font-size:1.1em; line-height:1; }
        .menu-button { background:transparent; border:none; color:var(--primary-text-color); cursor:pointer; padding:6px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; }
        .menu-button:hover { background: var(--secondary-background-color); }
        .menu-button:focus-visible { outline:2px solid var(--primary-color); outline-offset:2px; }
        .settings-grid, .stats-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; }
        .stat-box { padding:12px; border-radius:10px; background:var(--primary-background-color); border:1px solid var(--divider-color); }
        .setting-box { padding:12px; border-radius:10px; background:var(--primary-background-color); border:1px solid var(--divider-color); }
        .setting-actions { display:flex; gap:10px; align-items:end; flex-wrap:wrap; }
        .stat-label { color:var(--secondary-text-color); font-size:0.9rem; margin-bottom:6px; }
        .stat-value { font-size:1.15rem; font-weight:600; word-break:break-word; }
        .action-button, .mini-button { border:1px solid var(--divider-color); background:var(--primary-background-color); color:var(--primary-text-color); border-radius:10px; padding:10px 14px; cursor:pointer; font:inherit; }
        .mini-button { padding:6px 10px; font-size:0.9rem; }
        .danger { border-color: var(--error-color, #f44336); }
        .action-button[disabled], .mini-button[disabled] { opacity:0.6; cursor:default; }
        .device-block { border-top:1px solid var(--divider-color); }
        .device-block:first-of-type { border-top:none; }
        .device-head { display:grid; grid-template-columns:auto 1fr auto auto auto auto; gap:12px; align-items:center; padding:14px 6px; }
        .device-head.bulk { grid-template-columns:auto auto 1fr auto auto auto auto; }
        .caret { width:28px; height:28px; display:inline-flex; align-items:center; justify-content:center; border:none; background:transparent; color:var(--secondary-text-color); cursor:pointer; border-radius:8px; }
        .caret:hover { background:var(--secondary-background-color); }
        .caret-icon { display:inline-block; transition:transform 0.2s ease; font-size:18px; line-height:1; }
        .caret-icon.expanded { transform:rotate(90deg); }
        .device-name { font-weight:600; }
        .device-meta { font-size:0.9rem; color:var(--secondary-text-color); margin-top:2px; }
        .state-pill { display:inline-block; padding:4px 8px; border-radius:999px; background:var(--secondary-background-color); font-size:0.8rem; color:var(--secondary-text-color); }
        .device-rows { padding:0 0 10px 40px; }
        table { width:100%; border-collapse:collapse; margin-top:2px; }
        th, td { text-align:left; padding:10px 8px; border-top:1px solid var(--divider-color); vertical-align:middle; font-size:0.95rem; }
        thead th { color:var(--secondary-text-color); font-weight:500; }
        tbody tr:nth-child(even) { background:color-mix(in srgb, var(--primary-text-color) 4%, transparent); }
        tbody tr:hover, .device-head:hover { background:color-mix(in srgb, var(--primary-color) 10%, transparent); }
        code { font-family:var(--code-font-family, monospace); font-size:0.9rem; }
        .switch { position:relative; display:inline-flex; width:38px; height:22px; vertical-align:middle; cursor:pointer; }
        .switch.disabled { cursor:default; opacity:0.6; }
        .switch-input { position:absolute; inset:0; opacity:0; margin:0; cursor:inherit; }
        .switch-track { width:100%; height:100%; background:var(--disabled-color, #7f848e); border-radius:999px; transition:background 0.18s ease; position:relative; }
        .switch-track::after { content:""; position:absolute; top:3px; left:3px; width:16px; height:16px; border-radius:50%; background:white; transition:transform 0.18s ease; box-shadow:0 1px 2px rgba(0,0,0,0.25); }
        .switch-input:checked + .switch-track { background:var(--switch-checked-color, var(--primary-color)); }
        .switch-input:checked + .switch-track::after { transform:translateX(16px); }
        .switch.mixed .switch-track { background:color-mix(in srgb, var(--switch-checked-color, var(--primary-color)) 50%, var(--disabled-color, #7f848e)); }
        .switch.mixed .switch-track::after { transform:translateX(8px); }
        .row-note { color:var(--secondary-text-color); font-size:0.85rem; }
        .orphans-head { display:flex; justify-content:space-between; align-items:center; gap:12px; }
        .collapsible-head { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }
        .section-toggle { flex:1; min-width:220px; display:flex; align-items:center; gap:9px; padding:4px 0; border:none; background:transparent; color:var(--primary-text-color); cursor:pointer; text-align:left; font:inherit; }
        .section-toggle:focus-visible { outline:2px solid var(--primary-color); outline-offset:4px; border-radius:6px; }
        .section-caret { display:inline-block; color:var(--secondary-text-color); font-size:22px; line-height:1; transition:transform 0.2s ease; }
        .section-caret.expanded { transform:rotate(90deg); }
        .section-title { font-size:1.5em; font-weight:600; }
        .section-summary { color:var(--secondary-text-color); font-size:0.9rem; font-weight:400; }
        .section-body { margin-top:12px; }
        .subtle { color:var(--secondary-text-color); }
        .pending-dot { width:8px; height:8px; border-radius:50%; background:var(--warning-color, #ff9800); display:inline-block; margin-left:8px; }
        .action-button.stale { border-color:var(--warning-color, #ff9800); animation:gr-border-pulse 1.4s ease-in-out infinite; }
        .toolbar-actions { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
        @keyframes gr-border-pulse {
          0%   { box-shadow:0 0 0 0 color-mix(in srgb, var(--warning-color, #ff9800) 70%, transparent); border-color:var(--warning-color, #ff9800); }
          70%  { box-shadow:0 0 0 8px color-mix(in srgb, var(--warning-color, #ff9800) 0%, transparent); border-color:color-mix(in srgb, var(--warning-color, #ff9800) 60%, transparent); }
          100% { box-shadow:0 0 0 0 color-mix(in srgb, var(--warning-color, #ff9800) 0%, transparent); border-color:var(--warning-color, #ff9800); }
        }
        .right { text-align:right; white-space:nowrap; }
        .actions-cell { display:flex; align-items:center; gap:10px; justify-content:flex-start; flex-wrap:wrap; }
        .bulk-bar { position:sticky; top:8px; z-index:19; border-color:var(--primary-color); display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
        .bulk-summary { display:flex; flex-direction:column; gap:3px; }
        .bulk-checkbox { width:18px; height:18px; accent-color:var(--primary-color); cursor:pointer; }
        .bulk-checkbox[disabled] { cursor:default; opacity:0.5; }
        .bulk-col { width:34px; text-align:center; }
        .modal-backdrop { position:fixed; inset:0; z-index:1000; background:rgba(0,0,0,0.62); display:flex; align-items:center; justify-content:center; padding:20px; box-sizing:border-box; }
        .modal-card { width:min(560px,100%); max-height:90vh; overflow:auto; background:var(--card-background-color); color:var(--primary-text-color); border:1px solid var(--divider-color); border-radius:14px; padding:20px; box-shadow:0 18px 50px rgba(0,0,0,0.45); }
        .preview-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin:16px 0; }
        .history-status { text-transform:capitalize; }
        .history-status.completed { color:var(--success-color,#43a047); }
        .history-status.error, .history-status.interrupted { color:var(--error-color,#f44336); }
        select { padding:9px 12px; border-radius:10px; border:1px solid var(--divider-color); background:var(--primary-background-color); color:var(--primary-text-color); font:inherit; }
        .filter-grid { display:grid; grid-template-columns:minmax(240px,1fr) auto auto; gap:10px; align-items:center; }
        .pagination { display:flex; justify-content:center; align-items:center; gap:12px; margin-top:14px; }
        @media (max-width:800px) { .filter-grid { grid-template-columns:1fr; } .device-rows { padding-left:0; overflow:auto; } table { min-width:760px; } }
      </style>
      <div class="wrap">
        ${this._data.pending_restart ? `<div class="notice pending sticky"><strong>Pending changes.</strong> Restart Home Assistant so Recorder can apply the new configuration.</div>` : ""}
        ${purgeProgress?.active ? `
          <div class="notice purge-job">
            <div class="orphans-head">
              <div><strong>${this._escapeHtml(purgeProgress.label || "Purge job")}</strong><div class="row-note">Job ${this._escapeHtml(purgeProgress.job_id || "starting")}</div></div>
              <span class="state-pill">${this._escapeHtml(purgeProgress.status || "working")}</span>
            </div>
            <div class="message" style="margin-top:8px;">${this._escapeHtml(this._purgeProgressMessage(purgeProgress))}</div>
            <div class="purge-job-grid">
              <div class="purge-job-stat"><div class="stat-label">Deleted</div><strong>${purgeProgress.determinate === false ? "—" : this._formatNumber(purgeProgress.deleted_rows || 0)}</strong></div>
              <div class="purge-job-stat"><div class="stat-label">Remaining</div><strong>${purgeProgress.determinate === false ? "Calculating…" : this._formatNumber(purgeProgress.remaining_rows || 0)}</strong></div>
              <div class="purge-job-stat"><div class="stat-label">Elapsed</div><strong>${this._escapeHtml(this._formatDuration(purgeProgress.started_at))}</strong></div>
              <div class="purge-job-stat"><div class="stat-label">Average rate</div><strong>${purgeRate > 0 ? `${this._formatNumber(Math.round(purgeRate))}/s` : "—"}</strong></div>
              <div class="purge-job-stat"><div class="stat-label">Last progress</div><strong>${this._escapeHtml(this._formatDate(purgeProgress.last_progress_at || purgeProgress.updated_at))}</strong></div>
            </div>
            <progress class="purge-progress" max="100" ${purgeProgress.determinate === false ? "" : `value="${Number(purgeProgress.percent || 0)}"`}></progress>
            <div class="row-note">This server-side job continues if you close or reload the page. A Home Assistant restart interrupts monitoring and is recorded in job history.</div>
          </div>
        ` : ""}

        <div class="toolbar">
          <div class="toolbar-head">
            <div>
              <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
                ${this._narrow ? `<button class="menu-button" id="gr-menu-button" aria-label="Open sidebar"><svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"><path fill="currentColor" d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z"/></svg></button>` : ''}
                <h1 style="margin:0;">GUI Recorder</h1>
                <span class="state-pill">v${this._escapeHtml(this._data.version || "0.10.1")}</span>
              </div>
            </div>
          </div>
        </div>



        ${this._migrationCardMarkup()}

        <div class="card">
          <h2>Global recorder configuration</h2>
          <div class="settings-grid">
            <div class="setting-box">
              <div class="stat-label">Global retention days (purge_keep_days)</div>
              <input id="purge-keep-days" type="number" min="1" step="1" value="${this._escapeHtml(this._keepDaysValue)}">
            </div>
            <div class="setting-box">
              <div class="stat-label">Nightly auto purge (auto_purge)</div>
              <div>${this._switchMarkup({ checked: this._autoPurgeValue, kind: "global", entityId: "auto_purge" })}</div>
            </div>
            <div class="setting-box">
              <div class="stat-label">Automatic repack (auto_repack)</div>
              <div>${this._switchMarkup({ checked: this._autoRepackValue, kind: "global", entityId: "auto_repack", disabled: !this._autoPurgeValue })}</div>
              ${!this._autoPurgeValue ? `<div class="row-note" style="margin-top:8px;">Has no effect when auto_purge is disabled.</div>` : ""}
            </div>
            <div class="setting-box">
              <div class="stat-label">Commit interval (seconds)</div>
              <input id="commit-interval" type="number" min="0" step="1" value="${this._escapeHtml(this._commitIntervalValue)}">
            </div>
          </div>
          <div class="setting-actions" style="margin-top:12px;">
            <button class="action-button" id="save-global-options" ${this._savingGlobal ? "disabled" : ""}>${this._savingGlobal ? "Saving…" : "Save global configuration"}</button>
          </div>
          <div class="row-note" style="margin-top:8px;">This will be written to <code>gui_recorder.yaml</code> and applied after a Home Assistant restart.</div>
        </div>

        <div class="card">
          <h2>Manual exclusions (advanced)</h2>
          <div class="row-note" style="margin-bottom:10px;">
            Raw YAML merged into the generated <code>gui_recorder.yaml</code>. Only <code>exclude.domains</code>, <code>exclude.entity_globs</code>, <code>exclude.event_types</code>, <code>include.domains</code> and <code>include.entity_globs</code> are allowed here.
            Individual entities are managed exclusively through the device/entity toggles below — do not list <code>entities</code> in this block.
          </div>
          <textarea id="manual-exclusions" rows="8" spellcheck="false" placeholder="exclude:&#10;  domains:&#10;    - sensor&#10;  entity_globs:&#10;    - sensor.weather_*">${this._escapeHtml(this._manualExclusionsValue)}</textarea>
          <div class="setting-actions" style="margin-top:12px;">
            <button class="action-button" id="save-manual-exclusions" ${this._savingManualExclusions ? "disabled" : ""}>${this._savingManualExclusions ? "Saving…" : "Save manual exclusions"}</button>
          </div>
          ${this._manualExclusionsError ? `<div class="message warn">${this._escapeHtml(this._manualExclusionsError)}</div>` : ""}
        </div>

        <div class="card">
          <h2>Data</h2>
          <div class="settings-grid">
            <div class="setting-box">
              <div class="stat-label">Last update</div>
              <div class="stat-value">${this._escapeHtml(this._formatDate(stats.generated_at))}</div>
              <div class="setting-actions" style="margin-top:12px;">
                <button class="action-button ${this._shouldFlagUpdate() ? "stale" : ""}" id="analyze-db" ${purgeActionBusy ? "disabled" : ""} ${this._shouldFlagUpdate() ? `title="${this._refreshRecommended ? "Data changed since last update. Click to refresh." : "Data is older than 24 hours. Click to refresh."}"` : ""}>${this._analyzing ? "Updating…" : "Update data"}</button>
              </div>
            </div>
          </div>
          ${this._dataMessage ? `<div class="message">${this._escapeHtml(this._dataMessage)}</div>` : ""}
        </div>

        <div class="card">
          <h2>Database statistics</h2>
          <div class="stats-grid">
            <div class="stat-box"><div class="stat-label">Analyzed records (states)</div><div class="stat-value">${this._formatNumber(totalRows)}</div></div>
            <div class="stat-box"><div class="stat-label">Entities with records</div><div class="stat-value">${this._formatNumber(Object.keys(stats.entity_counts || {}).length)}</div></div>
            <div class="stat-box"><div class="stat-label">Current records</div><div class="stat-value">${this._formatNumber(stats.current_rows || 0)}</div></div>
            <div class="stat-box"><div class="stat-label">Obsolete / unlinked records</div><div class="stat-value">${this._formatNumber(stats.obsolete_rows || 0)}</div></div>
            <div class="stat-box"><div class="stat-label">Configured exclusions</div><div class="stat-value">${this._formatNumber(stats.configured_exclusions || 0)}</div><div class="stat-subtle">Matched ${this._formatNumber(stats.matched_exclusions || 0)} · Unmatched ${this._formatNumber(stats.unmatched_exclusions || 0)}</div></div>
            <div class="stat-box"><div class="stat-label">DB size</div><div class="stat-value">${stats.db_size_bytes == null ? "Unavailable" : this._escapeHtml(this._formatBytes(stats.db_size_bytes))}</div></div>
            ${stats.backend === "mariadb" ? `<div class="stat-box"><div class="stat-label">MariaDB data</div><div class="stat-value">${stats.db_data_bytes == null ? "Unavailable" : this._escapeHtml(this._formatBytes(stats.db_data_bytes))}</div></div>` : ""}
            ${stats.backend === "mariadb" ? `<div class="stat-box"><div class="stat-label">MariaDB indexes</div><div class="stat-value">${stats.db_index_bytes == null ? "Unavailable" : this._escapeHtml(this._formatBytes(stats.db_index_bytes))}</div></div>` : ""}
            ${stats.backend === "mariadb" ? `<div class="stat-box"><div class="stat-label">MariaDB free allocation estimate</div><div class="stat-value">${stats.db_free_bytes == null ? "Unavailable" : this._escapeHtml(this._formatBytes(stats.db_free_bytes))}</div><div class="stat-subtle">Engine estimate; not guaranteed reclaimable filesystem space.</div></div>` : ""}
            <div class="stat-box"><div class="stat-label">Database</div><div class="stat-value">${this._escapeHtml(stats.database || stats.db_path || "home-assistant_v2.db")}</div></div>
          </div>
          ${stats.error ? `<div class="message">Error: ${this._escapeHtml(stats.error)}</div>` : ""}
        </div>

        ${stats.backend === "mariadb" ? `
        <div class="card">
          <div class="orphans-head">
            <h2>MariaDB Health</h2>
            <span class="history-status ${stats.db_health_status === "ok" ? "completed" : stats.db_health_status === "warning" ? "interrupted" : ""}">${this._escapeHtml(stats.db_health_status || "unavailable")}</span>
          </div>
          <div class="stats-grid">
            <div class="stat-box"><div class="stat-label">Server</div><div class="stat-value">${this._escapeHtml(stats.db_product || "MariaDB / MySQL")}</div><div class="stat-subtle">${this._escapeHtml(stats.db_server_version || "Version unavailable")}</div></div>
            <div class="stat-box"><div class="stat-label">Character set</div><div class="stat-value">${this._escapeHtml(stats.db_character_set || "Unavailable")}</div><div class="stat-subtle">${this._escapeHtml(stats.db_collation || "")}</div></div>
            <div class="stat-box"><div class="stat-label">Default storage engine</div><div class="stat-value">${this._escapeHtml(stats.db_default_engine || "Unavailable")}</div></div>
            <div class="stat-box"><div class="stat-label">Non-InnoDB tables</div><div class="stat-value">${stats.db_non_innodb_tables == null ? "Unavailable" : this._formatNumber(stats.db_non_innodb_tables)}</div></div>
          </div>
          ${(stats.db_health_issues || []).length ? `<div class="notice stale" style="margin-top:12px; margin-bottom:0;"><strong>Attention needed</strong><ul>${stats.db_health_issues.map((issue) => `<li>${this._escapeHtml(issue)}</li>`).join("")}</ul></div>` : ""}
          ${stats.db_health_status === "ok" ? `<div class="message ok">Recorder database settings checked successfully.</div>` : ""}
          ${stats.db_health_error ? `<div class="message warn">Health metadata unavailable: ${this._escapeHtml(stats.db_health_error)}</div>` : ""}
        </div>
        ` : ""}

        ${migrationPending ? `
        <div class="card" style="border-color:var(--warning-color,#ff9800);">
          <div class="message warn" style="margin-top:0;">
            <strong>Migration required.</strong> Complete the migration above before managing entities or running maintenance actions.
            Once <code>gui_recorder.yaml</code> is the active recorder source (Step 3 done), all sections will become available.
          </div>
        </div>
        ` : `
        <div class="card">
          <h2>Maintenance actions</h2>
          ${this._data.repack_in_progress ? `<div class="notice pending"><strong>Repack in progress.</strong> The database is being compacted; this can take a while on large databases. Other purge actions are temporarily disabled.</div>` : ""}
          <div class="settings-grid">
            <div class="setting-box">
              <div class="stat-label">Purge DB</div>
              <div class="row-note">Deletes recorder data older than the current global retention of <strong>${this._escapeHtml(this._keepDaysValue)}</strong> day${Number(this._keepDaysValue) === 1 ? "" : "s"}. Recent data inside that retention window is kept.</div>
              <div class="setting-actions" style="margin-top:12px;">
                <button class="action-button danger" id="purge-all" ${purgeActionBusy ? "disabled" : ""}>${this._purgingAll ? "Purging…" : "Purge DB"}</button>
              </div>
            </div>
            <div class="setting-box">
              <div class="stat-label">Purge excluded entities</div>
              <div class="row-note">Deletes the full stored history for all entities currently excluded by GUI Recorder. This ignores the global retention and removes all stored rows for those excluded entities.</div>
              <div class="setting-actions" style="margin-top:12px;">
                <button class="action-button danger" id="purge-excluded" ${(purgeActionBusy || !Number(this._data?.stats?.configured_exclusions || 0)) ? "disabled" : ""}>${this._purgingExcluded ? "Purging…" : "Purge excluded entities"}</button>
              </div>
            </div>
            <div class="setting-box">
              <div class="stat-label">Bulk purge</div>
              <div class="row-note">Select any combination of devices, entities without a device, or obsolete entities, then purge them as one tracked job.</div>
              <div class="setting-actions" style="margin-top:12px;">
                <button class="action-button" id="bulk-mode-toggle" ${purgeActionBusy ? "disabled" : ""}>${this._bulkMode ? "Close bulk selection" : "Select entities to purge"}</button>
              </div>
            </div>
            <div class="setting-box">
              <div class="stat-label">Auto-update data after purge</div>
              <div>${this._switchMarkup({ checked: this._autoUpdateDataValue, kind: "auto-update", entityId: "auto_update_data", disabled: this._togglingAutoUpdate })}${this._togglingAutoUpdate ? `<span class="pending-dot" title="Saving"></span>` : ""}</div>
              <div class="row-note" style="margin-top:8px;">When enabled, database analysis is refreshed automatically after each purge. When disabled, refresh manually from the Data card.</div>
            </div>
            <div class="setting-box">
              <div class="stat-label">Repack after manual purge</div>
              <div>${this._switchMarkup({ checked: this._repackAfterManualPurgeValue, kind: "repack-toggle", entityId: "repack_after_manual_purge", disabled: this._togglingRepack })}${this._togglingRepack ? `<span class="pending-dot" title="Saving"></span>` : ""}</div>
              <div class="row-note" style="margin-top:8px;">When enabled, Home Assistant runs the recorder backend's repack operation after <em>Purge DB</em> and <em>Purge excluded entities</em>. This may reclaim disk space but can take significantly longer, especially on large databases.</div>
            </div>
            <div class="setting-box">
              <div class="stat-label">Maintenance notifications</div>
              <div>${this._switchMarkup({ checked: this._notifyOnPurgeFinishValue, kind: "notification-toggle", entityId: "notify_on_purge_finish", disabled: this._togglingNotifications })}${this._togglingNotifications ? `<span class="pending-dot" title="Saving"></span>` : ""}</div>
              <div class="row-note" style="margin-top:8px;">Show a Home Assistant notification when a purge takes at least one minute, fails, or is interrupted. Short successful jobs stay quiet.</div>
            </div>
            <div class="setting-box">
              <div class="stat-label">Restart Home Assistant</div>
              <div class="row-note">Restarts Home Assistant so pending recorder configuration changes can take effect. The interface may disconnect briefly during restart.</div>
              <div class="setting-actions" style="margin-top:12px;">
                <button class="action-button" id="restart-ha" ${purgeActionBusy ? "disabled" : ""}>Restart HA</button>
              </div>
            </div>
          </div>
          ${this._message ? `<div class="message">${this._escapeHtml(this._message)}</div>` : ""}
        </div>

        <div class="card">
          <div class="collapsible-head">${this._sectionToggleMarkup("maintenance-history", "Maintenance history", `Latest ${this._purgeHistory.length}`)}</div>
          ${this._collapsedSections.has("maintenance-history") ? "" : `<div class="section-body">
          <div class="row-note" style="margin-bottom:10px;">Background purge results are retained for the latest 20 jobs. Interrupted means Home Assistant restarted while monitoring was active. Active-job checkpoints are saved every 30 seconds.</div>
          ${this._purgeHistory.length === 0 ? `<div>No maintenance jobs recorded yet.</div>` : `
            <table>
              <thead><tr><th>Started</th><th>Job</th><th>Status</th><th class="right">Entities</th><th class="right">Deleted</th><th>Duration</th><th>DB change</th></tr></thead>
              <tbody>${this._purgeHistory.map((job) => `
                <tr>
                  <td>${this._escapeHtml(this._formatDate(job.started_at))}</td>
                  <td><div>${this._escapeHtml(job.label || job.kind || "Purge")}</div><div class="row-note">${this._escapeHtml(job.job_id || "")}</div></td>
                  <td><span class="history-status ${this._escapeHtml(job.status || "")}">${this._escapeHtml(job.status || "unknown")}</span>${job.error ? `<div class="row-note">${this._escapeHtml(job.error)}</div>` : ""}${job.warning ? `<div class="row-note">${this._escapeHtml(job.warning)}</div>` : ""}</td>
                  <td class="right">${this._formatNumber(job.entity_count || 0)}</td>
                  <td class="right">${job.determinate === false ? "—" : this._formatNumber(job.deleted_rows || 0)}</td>
                  <td>${this._escapeHtml(this._formatDuration(job.started_at, job.finished_at))}</td>
                  <td>${job.db_size_before != null && job.db_size_after != null ? `${this._escapeHtml(this._formatBytes(job.db_size_before))} → ${this._escapeHtml(this._formatBytes(job.db_size_after))}${job.reclaimed_bytes ? `<div class="row-note">${this._escapeHtml(this._formatBytes(job.reclaimed_bytes))} smaller</div>` : ""}` : "—"}</td>
                </tr>
              `).join("")}</tbody>
            </table>
          `}
          </div>`}
        </div>

        <div class="card">
          <div class="orphans-head"><h2>Filter</h2><div class="actions-cell"><button class="mini-button" id="expand-all-sections">Expand all sections</button><button class="mini-button" id="collapse-all-sections">Collapse all sections</button></div></div>
          <div class="filter-grid">
            <input id="filter" type="search" placeholder="Filter by entity_id, name, domain, or platform" value="${this._escapeHtml(this._filter)}">
            <select id="quick-filter" aria-label="Quick filter">
              <option value="all" ${this._quickFilter === "all" ? "selected" : ""}>All entities</option>
              <option value="has_records" ${this._quickFilter === "has_records" ? "selected" : ""}>Has database records</option>
              <option value="excluded" ${this._quickFilter === "excluded" ? "selected" : ""}>Excluded from Recorder</option>
              <option value="large" ${this._quickFilter === "large" ? "selected" : ""}>100,000+ records</option>
              <option value="orphans" ${this._quickFilter === "orphans" ? "selected" : ""}>Without a device</option>
              <option value="obsolete" ${this._quickFilter === "obsolete" ? "selected" : ""}>Obsolete / unlinked</option>
            </select>
            <select id="sort-mode" aria-label="Sort results">
              <option value="records_desc" ${this._sortMode === "records_desc" ? "selected" : ""}>Most records first</option>
              <option value="name" ${this._sortMode === "name" ? "selected" : ""}>Name</option>
              <option value="entity" ${this._sortMode === "entity" ? "selected" : ""}>Entity ID</option>
              <option value="domain" ${this._sortMode === "domain" ? "selected" : ""}>Domain</option>
            </select>
          </div>
          <div class="row-note" style="margin-top:8px;">Lists are paged at ${this._pageSize} rows to keep the panel responsive with thousands of entities.</div>
        </div>

        ${this._bulkMode ? `
        <div class="notice bulk-bar">
          <div class="bulk-summary">
            <strong>Bulk selection</strong>
            <span>${this._formatNumber(bulkSummary.entityIds.length)} entities · ${this._formatNumber(bulkSummary.records)} analyzed records</span>
            <span class="row-note">Select all visible follows the current filter. The selection is temporary and does not change Recorder settings.</span>
          </div>
          <div class="actions-cell">
            <button class="mini-button" id="bulk-select-visible" ${!visiblePurgeEntityIds.length || purgeActionBusy ? "disabled" : ""}>Select all visible</button>
            <button class="mini-button" id="bulk-select-none" ${!bulkSummary.entityIds.length || purgeActionBusy ? "disabled" : ""}>Select none</button>
            <button class="action-button danger" id="bulk-purge-selected" ${!bulkSummary.entityIds.length || purgeActionBusy ? "disabled" : ""}>${this._bulkPurging ? "Purging…" : "Purge selected"}</button>
            <button class="mini-button" id="bulk-close" ${this._bulkPurging ? "disabled" : ""}>Close</button>
          </div>
        </div>
        ` : ""}

        <div class="card">
          <div class="collapsible-head">
            ${this._sectionToggleMarkup("devices", "Devices", `${filteredDevices.length} devices${this._bulkMode ? ` · ${selectedDeviceEntities} selected` : ""}`)}
            <div class="actions-cell">
              ${this._bulkMode ? `<button class="mini-button" data-bulk-section-select="devices">Select section</button><button class="mini-button" data-bulk-section-clear="devices">Clear section</button>` : ""}
              <button class="mini-button" id="devices-select-all" ${!visibleDeviceEntityIds.length || this._pending.has("bulk:devices:select") ? "disabled" : ""}>Record all</button>
              <button class="mini-button" id="devices-deselect-all" ${!visibleDeviceEntityIds.length || this._pending.has("bulk:devices:deselect") ? "disabled" : ""}>Exclude all</button>
            </div>
          </div>
          ${this._collapsedSections.has("devices") ? "" : `<div class="section-body">
          ${filteredDevices.length === 0 ? `<div>No devices to display.</div>` : devicePage.items.map((device) => {
            const state = this._deviceState(device);
            const expanded = this._expanded.has(device.device_id);
            const entities = this._deviceVisibleEntities(device);
            const deviceBusy = this._pending.has(`device:${device.device_id}`);
            const devicePurgeBusy = this._pending.has(`purge-device:${device.device_id}`);
            const bulkState = this._bulkDeviceState(device);
            return `
              <div class="device-block" data-device-block="${this._escapeHtml(device.device_id)}">
                <div class="device-head ${this._bulkMode ? "bulk" : ""}">
                  <button class="caret" data-toggle-device="${this._escapeHtml(device.device_id)}" aria-label="Expand or collapse device"><span class="caret-icon ${expanded ? "expanded" : ""}">›</span></button>
                  ${this._bulkMode ? `<input class="bulk-checkbox" type="checkbox" data-bulk-device="${this._escapeHtml(device.device_id)}" aria-label="Select ${this._escapeHtml(device.name)} for purge" ${bulkState.checked ? "checked" : ""} ${bulkState.disabled || purgeActionBusy ? "disabled" : ""}>` : ""}
                  <div>
                    <div class="device-name">${this._escapeHtml(device.name)}</div>
                    <div class="device-meta">${this._escapeHtml([device.manufacturer || "", device.model || ""].filter(Boolean).join(" · "))}</div>
                  </div>
                  <div class="state-pill">${this._escapeHtml(state.label)}</div>
                  <div class="right">${this._formatNumber(device.record_count || 0)}</div>
                  <div class="actions-cell"><button class="mini-button" ${purgeActionBusy || devicePurgeBusy || !device.record_count ? "disabled" : ""} data-purge-device="${this._escapeHtml(device.device_id)}">Purge</button>${devicePurgeBusy ? `<span class="pending-dot" title="Purging"></span>` : ""}</div>
                  <div>${this._switchMarkup({ checked: state.all, mixed: state.some, disabled: deviceBusy, deviceId: device.device_id, kind: "device" })}${deviceBusy ? `<span class="pending-dot" title="Saving"></span>` : ""}</div>
                </div>
                ${expanded ? `
                  <div class="device-rows">
                    <table>
                      <thead>
                        <tr>
                          ${this._bulkMode ? `<th class="bulk-col">Select</th>` : ""}
                          <th>Entity</th>
                          <th>Name</th>
                          <th>Platform</th>
                          <th class="right">Records</th>
                          <th class="right">%</th>
                          <th>Recorder</th>
                          <th>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${entities.map((entity) => {
                          const entityBusy = this._pending.has(entity.entity_id);
                          const purgeBusy = this._pending.has(`purge:${entity.entity_id}`);
                          return `
                            <tr>
                              ${this._bulkMode ? `<td class="bulk-col"><input class="bulk-checkbox" type="checkbox" data-bulk-entity="${this._escapeHtml(entity.entity_id)}" aria-label="Select ${this._escapeHtml(entity.entity_id)} for purge" ${this._bulkSelected.has(entity.entity_id) ? "checked" : ""} ${purgeActionBusy || !entity.record_count ? "disabled" : ""}></td>` : ""}
                              <td><code>${this._escapeHtml(entity.entity_id)}</code></td>
                              <td><div>${this._escapeHtml(entity.name || "")}</div>${entity.disabled_by || entity.hidden_by ? `<div class="row-note">${this._escapeHtml([entity.disabled_by ? `disabled:${entity.disabled_by}` : "", entity.hidden_by ? `hidden:${entity.hidden_by}` : ""].filter(Boolean).join(" · "))}</div>` : ""}</td>
                              <td>${this._escapeHtml(entity.platform || "")}</td>
                              <td class="right">${this._formatNumber(entity.record_count || 0)}</td>
                              <td class="right">${this._escapeHtml(this._formatPercent(entity.record_count || 0, totalRows))}</td>
                              <td>${this._switchMarkup({ checked: entity.recorded, disabled: entityBusy, entityId: entity.entity_id, deviceId: device.device_id, kind: "entity" })}${entityBusy ? `<span class="pending-dot" title="Saving"></span>` : ""}${this._globNote(entity)}</td>
                              <td><div class="actions-cell">${this._purgeButton(entity.entity_id, device.device_id, purgeActionBusy || purgeBusy || !entity.record_count)}${purgeBusy ? `<span class="pending-dot" title="Purging"></span>` : ""}</div></td>
                            </tr>
                          `;
                        }).join("")}
                      </tbody>
                    </table>
                  </div>
                ` : ""}
              </div>
            `;
          }).join("")}
          ${this._paginationMarkup("devices", devicePage.page, devicePage.pageCount, filteredDevices.length)}
          </div>`}
        </div>

        <div class="card">
          <div class="collapsible-head">
            ${this._sectionToggleMarkup("orphans", "Entities without a device", `${filteredOrphans.length}${this._bulkMode ? ` · ${selectedOrphans} selected` : ""}`)}
            <div class="actions-cell">
              ${this._bulkMode ? `<button class="mini-button" data-bulk-section-select="orphans">Select section</button><button class="mini-button" data-bulk-section-clear="orphans">Clear section</button>` : ""}
              <button class="mini-button" id="orphans-select-all" ${!visibleOrphanEntityIds.length || this._pending.has("bulk:orphans:select") ? "disabled" : ""}>Record all</button>
              <button class="mini-button" id="orphans-deselect-all" ${!visibleOrphanEntityIds.length || this._pending.has("bulk:orphans:deselect") ? "disabled" : ""}>Exclude all</button>
            </div>
          </div>
          ${this._collapsedSections.has("orphans") ? "" : `<div class="section-body">
          ${filteredOrphans.length === 0 ? `<div>No unassigned entities.</div>` : `
            <table>
              <thead>
                <tr>
                  ${this._bulkMode ? `<th class="bulk-col">Select</th>` : ""}
                  <th>Entity</th>
                  <th>Name</th>
                  <th>Platform</th>
                  <th class="right">Records</th>
                  <th class="right">%</th>
                  <th>Recorder</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                ${orphanPage.items.map((entity) => {
                  const entityBusy = this._pending.has(entity.entity_id);
                  const purgeBusy = this._pending.has(`purge:${entity.entity_id}`);
                  return `
                    <tr>
                      ${this._bulkMode ? `<td class="bulk-col"><input class="bulk-checkbox" type="checkbox" data-bulk-entity="${this._escapeHtml(entity.entity_id)}" aria-label="Select ${this._escapeHtml(entity.entity_id)} for purge" ${this._bulkSelected.has(entity.entity_id) ? "checked" : ""} ${purgeActionBusy || !entity.record_count ? "disabled" : ""}></td>` : ""}
                      <td><code>${this._escapeHtml(entity.entity_id)}</code></td>
                      <td>${this._escapeHtml(entity.name || "")}</td>
                      <td>${this._escapeHtml(entity.platform || "")}</td>
                      <td class="right">${this._formatNumber(entity.record_count || 0)}</td>
                      <td class="right">${this._escapeHtml(this._formatPercent(entity.record_count || 0, totalRows))}</td>
                      <td>${this._switchMarkup({ checked: entity.recorded, disabled: entityBusy, entityId: entity.entity_id, kind: "entity" })}${entityBusy ? `<span class="pending-dot" title="Saving"></span>` : ""}${this._globNote(entity)}</td>
                      <td><div class="actions-cell">${this._purgeButton(entity.entity_id, null, purgeActionBusy || purgeBusy || !entity.record_count)}${purgeBusy ? `<span class="pending-dot" title="Purging"></span>` : ""}</div></td>
                    </tr>
                  `;
                }).join("")}
              </tbody>
            </table>
            ${this._paginationMarkup("orphans", orphanPage.page, orphanPage.pageCount, filteredOrphans.length)}
          `}
          </div>`}
        </div>

        <div class="card">
          <div class="collapsible-head">
            ${this._sectionToggleMarkup("unmatched", "Unmatched configured exclusions", `${filteredUnmatchedExclusions.length}`)}
            <div style="display:flex; align-items:center; gap:8px;">
              ${filteredUnmatchedExclusions.length ? `<button class="secondary" id="remove-unmatched-exclusions" ${this._pending.has("remove-unmatched") ? "disabled" : ""}>Remove all unmatched</button>` : ""}
            </div>
          </div>
          ${this._collapsedSections.has("unmatched") ? "" : `<div class="section-body">
          <div class="message">Configured exclusions stored by GUI Recorder that do not match any current entity_id in Home Assistant.</div>
          ${filteredUnmatchedExclusions.length === 0 ? `<div>No unmatched exclusions detected.</div>` : `
            <table>
              <thead>
                <tr>
                  <th>Entity</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                ${filteredUnmatchedExclusions.map((entityId) => `
                  <tr>
                    <td><code>${this._escapeHtml(entityId)}</code></td>
                    <td>Stored in configuration only</td>
                    <td><div class="actions-cell"><button class="secondary" data-remove-exclusion="${this._escapeHtml(entityId)}" ${this._pending.has(`remove:${entityId}`) ? "disabled" : ""}>Remove</button>${this._pending.has(`remove:${entityId}`) ? `<span class="pending-dot" title="Saving"></span>` : ""}</div></td>
                  </tr>
                `).join("")}
              </tbody>
            </table>
          `}
          </div>`}
        </div>

        <div class="card">
          <div class="collapsible-head">${this._sectionToggleMarkup("obsolete", "Obsolete / unlinked records", `${filteredObsolete.length}${this._bulkMode ? ` · ${selectedObsolete} selected` : ""}`)}<div class="actions-cell">${this._bulkMode ? `<button class="mini-button" data-bulk-section-select="obsolete">Select section</button><button class="mini-button" data-bulk-section-clear="obsolete">Clear section</button>` : ""}</div></div>
          ${this._collapsedSections.has("obsolete") ? "" : `<div class="section-body">
          <div class="message">Entities present in the database but not in the current Home Assistant entity registry.</div>
          ${filteredObsolete.length === 0 ? `<div>No obsolete / unlinked records detected.</div>` : `
            <table>
              <thead>
                <tr>
                  ${this._bulkMode ? `<th class="bulk-col">Select</th>` : ""}
                  <th>Entity</th>
                  <th>Platform</th>
                  <th class="right">Records</th>
                  <th class="right">%</th>
                  <th>Recorder</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                ${obsoletePage.items.map((entity) => {
                  const entityBusy = this._pending.has(entity.entity_id);
                  const purgeBusy = this._pending.has(`purge:${entity.entity_id}`);
                  return `
                    <tr>
                      ${this._bulkMode ? `<td class="bulk-col"><input class="bulk-checkbox" type="checkbox" data-bulk-entity="${this._escapeHtml(entity.entity_id)}" aria-label="Select ${this._escapeHtml(entity.entity_id)} for purge" ${this._bulkSelected.has(entity.entity_id) ? "checked" : ""} ${purgeActionBusy || !entity.record_count ? "disabled" : ""}></td>` : ""}
                      <td><code>${this._escapeHtml(entity.entity_id)}</code></td>
                      <td>${this._escapeHtml(entity.platform || "")}</td>
                      <td class="right">${this._formatNumber(entity.record_count || 0)}</td>
                      <td class="right">${this._escapeHtml(this._formatPercent(entity.record_count || 0, totalRows))}</td>
                      <td>${this._switchMarkup({ checked: entity.recorded, disabled: entityBusy, entityId: entity.entity_id, kind: "entity" })}${entityBusy ? `<span class="pending-dot" title="Saving"></span>` : ""}${this._globNote(entity)}</td>
                      <td><div class="actions-cell">${this._purgeButton(entity.entity_id, null, purgeActionBusy || purgeBusy || !entity.record_count)}${purgeBusy ? `<span class="pending-dot" title="Purging"></span>` : ""}</div></td>
                    </tr>
                  `;
                }).join("")}
              </tbody>
            </table>
            ${this._paginationMarkup("obsolete", obsoletePage.page, obsoletePage.pageCount, filteredObsolete.length)}
          `}
          </div>`}
        </div>
        `}

      </div>
      ${this._purgePreview ? `
        <div class="modal-backdrop" role="presentation">
          <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="purge-preview-title">
            <h2 id="purge-preview-title">${this._escapeHtml(this._purgePreview.title || "Confirm purge")}</h2>
            <div>${this._escapeHtml(this._purgePreview.description || "")}</div>
            <div class="preview-grid">
              ${this._purgePreview.entities != null ? `<div class="stat-box"><div class="stat-label">Entities</div><div class="stat-value">${this._formatNumber(this._purgePreview.entities)}</div></div>` : ""}
              ${this._purgePreview.records != null ? `<div class="stat-box"><div class="stat-label">${this._escapeHtml(this._purgePreview.recordsLabel || "Analyzed records")}</div><div class="stat-value">${this._formatNumber(this._purgePreview.records)}</div>${this._purgePreview.percent ? `<div class="stat-subtle">${this._escapeHtml(this._purgePreview.percent)} of analyzed state rows</div>` : ""}</div>` : ""}
              <div class="stat-box"><div class="stat-label">Repack</div><div class="stat-value">${this._purgePreview.repack ? "Yes" : "No"}</div></div>
              <div class="stat-box"><div class="stat-label">Refresh statistics afterward</div><div class="stat-value">${this._autoUpdateDataValue ? "Yes" : "No"}</div></div>
            </div>
            ${this._purgePreview.warning ? `<div class="notice stale">${this._escapeHtml(this._purgePreview.warning)}</div>` : ""}
            <div class="actions-cell" style="justify-content:flex-end; margin-top:16px;">
              <button class="action-button" id="purge-preview-cancel">Cancel</button>
              <button class="action-button danger" id="purge-preview-confirm">Permanently purge</button>
            </div>
          </div>
        </div>
      ` : ""}
    `;

    if (filterWasFocused) {
      const newFilter = this.shadowRoot.getElementById("filter");
      if (newFilter) {
        newFilter.focus();
        try {
          newFilter.setSelectionRange(filterSelStart, filterSelEnd);
        } catch (_err) { /* type=search may not support setSelectionRange in all browsers */ }
      }
    }

    this.shadowRoot.getElementById("filter")?.addEventListener("input", (ev) => {
      this._filter = ev.target.value;
      this._resetPages();
      clearTimeout(this._filterDebounceTimer);
      this._filterDebounceTimer = setTimeout(() => this._render(), 250);
    });

    this.shadowRoot.getElementById("quick-filter")?.addEventListener("change", (ev) => {
      this._quickFilter = ev.currentTarget.value;
      this._resetPages();
      this._render();
    });
    this.shadowRoot.getElementById("sort-mode")?.addEventListener("change", (ev) => {
      this._sortMode = ev.currentTarget.value;
      this._resetPages();
      this._render();
    });
    this.shadowRoot.querySelectorAll("[data-collapse-section]").forEach((button) => {
      button.addEventListener("click", () => this._toggleSection(button.getAttribute("data-collapse-section")));
    });
    this.shadowRoot.getElementById("expand-all-sections")?.addEventListener("click", () => this._setAllSections(false));
    this.shadowRoot.getElementById("collapse-all-sections")?.addEventListener("click", () => this._setAllSections(true));
    this.shadowRoot.querySelectorAll("[data-page-section]").forEach((button) => {
      button.addEventListener("click", () => {
        const section = button.getAttribute("data-page-section");
        this._pages[section] = Number(button.getAttribute("data-page") || 1);
        this._render();
      });
    });

    this.shadowRoot.getElementById("purge-preview-cancel")?.addEventListener("click", () => this._resolvePurgePreview(false));
    this.shadowRoot.getElementById("purge-preview-confirm")?.addEventListener("click", () => this._resolvePurgePreview(true));

    this.shadowRoot.getElementById("analyze-db")?.addEventListener("click", () => this._analyzeDb());
    this.shadowRoot.getElementById("purge-all")?.addEventListener("click", () => this._purgeAll());
    this.shadowRoot.getElementById("purge-excluded")?.addEventListener("click", () => this._purgeExcludedEntities());
    this.shadowRoot.getElementById("bulk-mode-toggle")?.addEventListener("click", () => this._setBulkMode(!this._bulkMode));
    this.shadowRoot.getElementById("bulk-select-visible")?.addEventListener("click", () => this._selectBulkVisible(visiblePurgeEntityIds));
    this.shadowRoot.getElementById("bulk-select-none")?.addEventListener("click", () => this._clearBulkSelection());
    this.shadowRoot.getElementById("bulk-purge-selected")?.addEventListener("click", () => this._purgeSelected());
    this.shadowRoot.getElementById("bulk-close")?.addEventListener("click", () => this._setBulkMode(false));
    this.shadowRoot.getElementById("restart-ha")?.addEventListener("click", () => this._restartHomeAssistant());
    this.shadowRoot.getElementById("remove-unmatched-exclusions")?.addEventListener("click", () => this._removeUnmatchedExclusions());
    this.shadowRoot.querySelectorAll("[data-remove-exclusion]").forEach((button) => button.addEventListener("click", () => this._removeExclusion(button.getAttribute("data-remove-exclusion"))));
    this.shadowRoot.getElementById("purge-keep-days")?.addEventListener("input", (ev) => { this._keepDaysValue = ev.target.value; });
    this.shadowRoot.getElementById("commit-interval")?.addEventListener("input", (ev) => { this._commitIntervalValue = ev.target.value; });
    this.shadowRoot.getElementById("save-global-options")?.addEventListener("click", () => this._saveGlobalOptions());
    this.shadowRoot.getElementById("manual-exclusions")?.addEventListener("input", (ev) => { this._manualExclusionsValue = ev.target.value; });
    this.shadowRoot.getElementById("save-manual-exclusions")?.addEventListener("click", () => this._saveManualExclusions());

    this.shadowRoot.getElementById("devices-select-all")?.addEventListener("click", () => this._setEntitiesBulk(visibleDeviceEntityIds, true, "bulk:devices:select"));
    this.shadowRoot.getElementById("devices-deselect-all")?.addEventListener("click", () => this._setEntitiesBulk(visibleDeviceEntityIds, false, "bulk:devices:deselect"));
    this.shadowRoot.getElementById("orphans-select-all")?.addEventListener("click", () => this._setEntitiesBulk(visibleOrphanEntityIds, true, "bulk:orphans:select"));
    this.shadowRoot.getElementById("orphans-deselect-all")?.addEventListener("click", () => this._setEntitiesBulk(visibleOrphanEntityIds, false, "bulk:orphans:deselect"));

    this.shadowRoot.querySelectorAll("input[data-bulk-entity]").forEach((el) => {
      el.addEventListener("change", (ev) => {
        this._setBulkEntity(ev.currentTarget.getAttribute("data-bulk-entity"), ev.currentTarget.checked);
      });
    });

    this.shadowRoot.querySelectorAll("input[data-bulk-device]").forEach((el) => {
      const deviceId = el.getAttribute("data-bulk-device");
      const device = (this._data.devices || []).find((item) => item.device_id === deviceId);
      if (device) el.indeterminate = this._bulkDeviceState(device).indeterminate;
      el.addEventListener("change", (ev) => {
        this._setBulkDevice(ev.currentTarget.getAttribute("data-bulk-device"), ev.currentTarget.checked);
      });
    });

    const bulkSectionIds = {
      devices: visibleDeviceEntityIds,
      orphans: visibleOrphanEntityIds,
      obsolete: filteredObsolete.map((entity) => entity.entity_id),
    };
    this.shadowRoot.querySelectorAll("[data-bulk-section-select]").forEach((button) => {
      button.addEventListener("click", () => this._setBulkVisible(bulkSectionIds[button.getAttribute("data-bulk-section-select")] || [], true));
    });
    this.shadowRoot.querySelectorAll("[data-bulk-section-clear]").forEach((button) => {
      button.addEventListener("click", () => this._setBulkVisible(bulkSectionIds[button.getAttribute("data-bulk-section-clear")] || [], false));
    });

    this.shadowRoot.querySelectorAll("[data-toggle-device]").forEach((el) => {
      el.addEventListener("click", (ev) => {
        const deviceId = ev.currentTarget.getAttribute("data-toggle-device");
        this._toggleExpanded(deviceId);
      });
    });

    this.shadowRoot.querySelectorAll("input[data-kind='entity']").forEach((el) => {
      if (el.dataset.bound === "1") return;
      el.dataset.bound = "1";
      el.addEventListener("change", (ev) => {
        const entityId = ev.currentTarget.getAttribute("data-entity-id");
        const deviceId = ev.currentTarget.getAttribute("data-device-id");
        this._setEntity(entityId, ev.currentTarget.checked, deviceId || null);
      });
    });

    this.shadowRoot.querySelectorAll("input[data-kind='device']").forEach((el) => {
      if (el.dataset.bound === "1") return;
      el.dataset.bound = "1";
      if (el.closest(".switch")?.classList.contains("mixed")) el.indeterminate = true;
      el.addEventListener("change", (ev) => {
        const deviceId = ev.currentTarget.getAttribute("data-device-id");
        this._setDevice(deviceId, ev.currentTarget.checked);
      });
    });



    this.shadowRoot.querySelectorAll("input[data-kind='global']").forEach((el) => {
      if (el.dataset.bound === "1") return;
      el.dataset.bound = "1";
      el.addEventListener("change", (ev) => {
        const key = ev.currentTarget.getAttribute("data-entity-id");
        const checked = ev.currentTarget.checked;
        if (key === "auto_purge") {
          this._autoPurgeValue = checked;
          if (!checked) this._autoRepackValue = false;
        }
        if (key === "auto_repack") this._autoRepackValue = checked;
        this._render();
      });
    });

    this.shadowRoot.querySelectorAll("input[data-kind='auto-update']").forEach((el) => {
      if (el.dataset.bound === "1") return;
      el.dataset.bound = "1";
      el.addEventListener("change", (ev) => {
        this._setAutoUpdateData(ev.currentTarget.checked);
      });
    });

    this.shadowRoot.querySelectorAll("input[data-kind='repack-toggle']").forEach((el) => {
      if (el.dataset.bound === "1") return;
      el.dataset.bound = "1";
      el.addEventListener("change", (ev) => {
        this._setRepackAfterManualPurge(ev.currentTarget.checked);
      });
    });

    this.shadowRoot.querySelectorAll("input[data-kind='notification-toggle']").forEach((el) => {
      if (el.dataset.bound === "1") return;
      el.dataset.bound = "1";
      el.addEventListener("change", (ev) => {
        this._setNotifyOnPurgeFinish(ev.currentTarget.checked);
      });
    });

    this.shadowRoot.getElementById("migration-import")?.addEventListener("click", () => this._migrationAction("gui_recorder/import_legacy"));
    this.shadowRoot.getElementById("migration-disable")?.addEventListener("click", () => this._migrationAction("gui_recorder/disable_legacy"));
    this.shadowRoot.getElementById("migration-enable")?.addEventListener("click", () => this._migrationAction("gui_recorder/enable_gui"));

    this.shadowRoot.getElementById("gr-menu-button")?.addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
    });

    this.shadowRoot.querySelectorAll("[data-purge-entity]").forEach((el) => {
      if (el.dataset.bound === "1") return;
      el.dataset.bound = "1";
      el.addEventListener("click", (ev) => {
        const entityId = ev.currentTarget.getAttribute("data-purge-entity");
        const deviceId = ev.currentTarget.getAttribute("data-device-id");
        this._purgeEntity(entityId, deviceId || null);
      });
    });
    this.shadowRoot.querySelectorAll("[data-purge-device]").forEach((el) => {
      if (el.dataset.bound === "1") return;
      el.dataset.bound = "1";
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const deviceId = ev.currentTarget.getAttribute("data-purge-device");
        this._purgeDevice(deviceId);
      });
    });
  }
}

customElements.define("gui-recorder-panel", GuiRecorderPanel);
