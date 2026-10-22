# GUI Recorder

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub release](https://img.shields.io/github/v/release/Lisnol/gui-recorder)](https://github.com/Lisnol/gui-recorder/releases)

A Home Assistant custom component that adds a sidebar panel to manage your `recorder` configuration (which entities get recorded) and database maintenance (purge, repack, stats) — without editing `configuration.yaml`.

![GUI Recorder overview](images/overview.png)

> **Supported recorder backends:** SQLite and MariaDB/MySQL. PostgreSQL is still rejected during setup.

> [!WARNING]
> **Use with care.** Purge operations permanently delete recorder history and cannot be undone. The migration flow modifies `configuration.yaml` directly. Always create a full Home Assistant backup before using maintenance or migration features. The authors take no responsibility for data loss.

## Features

- **Sidebar panel** integrated directly into the Home Assistant navigation bar.
- **Per-device and per-entity control**: enable or disable recording with a toggle — no YAML editing required.
- **Database statistics**: total / current / obsolete records, matched exclusions, database location and disk/schema size when available. MariaDB also shows data, index, and free-allocation estimates separately.
- **MariaDB Health**: reports server version, database character set/collation, default engine, non-InnoDB tables, and known Recorder compatibility warnings without exposing credentials.
- **Maintenance actions**:
  - Purge the database (uses global retention setting).
  - Purge excluded entities (all state history, ignores retention; Home Assistant keeps long-term statistics separately).
  - Select devices and entities across the filtered views and purge them together as one tracked job.
  - Review a preflight summary with affected entities, analyzed rows, database share, repack, and refresh choices before deletion.
  - Auto-update stats after each purge (optional).
  - Repack after manual purge (optional).
  - Restart Home Assistant.
- **Background purge jobs**: jobs continue on the Home Assistant server if the panel is closed or reloaded, with a sticky live progress card showing deleted rows, remaining rows, elapsed time, rate, and last progress.
- **Resilient checkpoints and notifications**: active progress is saved every 30 seconds; long jobs, failures, and interrupted jobs can create a persistent Home Assistant notification.
- **Maintenance history**: keeps the latest 20 completed, failed, or interrupted jobs with duration and size change when available.
- **Collapsible large sections**: maintenance history and entity/device lists can be expanded or collapsed together or individually; the browser remembers the layout.
- **Large-installation tools**: quick filters, sorting, section-level select/clear, and pagination at 100 rows per page.
- **Orphan exclusion detection**: shows entries in `gui_recorder.yaml` that no longer match any entity, with a one-click bulk-remove.
- **Guided migration**: imports your existing `recorder` configuration from `configuration.yaml` in 3 steps (import → disable old block → activate `!include`).
- **Cache busting**: the panel JS is served with `?v=VERSION` to prevent stale versions after updates.
- **Tracked purges**: global, device, entity, selected, excluded-entity, and repack operations use Home Assistant's Recorder queue and are reported complete only after the final internal database batch. A no-progress warning appears after 10 minutes, while the job remains active so another destructive job cannot overlap it. After that warning, the panel exposes a guarded Home Assistant restart as the escape hatch for a truly stuck Recorder task.
- **Fail-safe configuration writes**: YAML files are validated and atomically replaced, migration backups cannot collide, and missing integration storage is recovered from an active `gui_recorder.yaml` instead of overwriting it with defaults.
- **Downloadable diagnostics**: Home Assistant diagnostics include health, settings, and maintenance state while redacting database locations, secret names, entity IDs, job labels, and raw YAML.

## Screenshots

**Per-device and per-entity control**, with purge actions per row:

![Devices view](images/devices.png)

**Orphan exclusion detection and obsolete record cleanup**:

![Unmatched exclusions and obsolete records](images/exclusions-obsolete.png)

## Installation

### Via HACS custom repository (recommended)

This MariaDB-enabled fork is installed as a custom HACS repository.

[![Open your Home Assistant instance and open this repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Lisnol&repository=gui-recorder&category=integration)

1. Click the button above, or add `https://github.com/Lisnol/gui-recorder` under **HACS → Integrations → Custom repositories** with category **Integration**.
2. Search for **GUI Recorder** and install it.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and choose **GUI Recorder**.

### Manual

1. Copy `custom_components/gui_recorder/` into `<config>/custom_components/gui_recorder/`.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → GUI Recorder**.

## Usage

After installing and configuring the integration, a **GUI Recorder** panel appears in the sidebar. From there you can:

- Filter entities by `entity_id`, friendly name, domain, or platform.
- Apply quick filters, sort by record count/name/entity/domain, and page through large entity collections.
- Toggle recording for entire devices or individual entities.
- Run database analysis and purges (global, per-device, per-entity, or a temporary multi-selection).
- Import your existing `recorder` config from `configuration.yaml`.

Purge jobs are owned by Home Assistant rather than the browser, so navigating away or reloading the panel does not stop them. Active progress is checkpointed every 30 seconds. Restarting Home Assistant does stop GUI Recorder's in-memory monitor: the job is recorded as **interrupted** with its last saved checkpoint, no purge is automatically retried, and you should refresh the database analysis before deciding whether to run it again.

The generated configuration is written to `gui_recorder.yaml`, included from `configuration.yaml` via:

```yaml
recorder: !include gui_recorder.yaml
```

The integration writes that file automatically. If you already had a `recorder:` block in `configuration.yaml`, the panel's migration flow guides you through replacing it.

## Uninstalling

Removing the integration itself doesn't touch your recorder database or delete any data. Purge buttons do delete the history described in their confirmation dialog while the integration is installed. The generated `gui_recorder.yaml` lives in your config directory, not inside the integration, so it isn't removed either: `recorder: !include gui_recorder.yaml` is plain Home Assistant YAML and keeps working with the integration gone, preserving the current recording behavior.

To leave `configuration.yaml` clean (recommended), fold `gui_recorder.yaml` back into a plain inline `recorder:` block instead of leaving it pointed at a generated file:

1. Open `gui_recorder.yaml` and copy everything **below** the two `#` comment lines at the top.
2. In `configuration.yaml`, replace:
   ```yaml
   recorder: !include gui_recorder.yaml
   ```
   with `recorder:` followed by that copied content, indented one level underneath, e.g.:
   ```yaml
   recorder:
     auto_purge: true
     auto_repack: true
     purge_keep_days: 10
     commit_interval: 5
     exclude:
       entities:
         - sensor.example
   ```
3. Remove the integration: **Settings → Devices & services → GUI Recorder → Delete**. For a manual install, also delete `custom_components/gui_recorder/`.
4. Restart Home Assistant and confirm everything looks right.
5. Optional cleanup, only after confirming the restart worked: delete `gui_recorder.yaml`. Leave `.storage/gui_recorder.data` in place unless you deliberately want to discard GUI Recorder's saved settings and maintenance history; Home Assistant manages `.storage` files and they should not normally be edited or removed by hand.

## Requirements

- Home Assistant **2024.1.0** or later.
- Recorder database on **SQLite** or **MariaDB/MySQL**. PostgreSQL is not supported.
- MariaDB recorder URLs must be stored in `secrets.yaml` and referenced with `db_url: !secret recorder_db_url` before using the guided migration. The integration stores only the secret name, never the resolved URL or password.

### MariaDB notes

- GUI Recorder reuses Home Assistant's existing SQLAlchemy recorder connection; it does not need separate database credentials or a Python database driver.
- Entity counts use Home Assistant's current recorder schema and run on the recorder database executor.
- Schema data size, index size, and `DATA_FREE` are read from `information_schema.TABLES`. `DATA_FREE` is an engine allocation estimate, not a guarantee that repack will return the same number of filesystem bytes. If the recorder user cannot read that metadata, sizes are shown as unavailable while the rest of the analysis continues to work.
- Purge and repack actions run in Home Assistant's Recorder task queue and use
  Recorder's own bounded purge implementation; GUI Recorder does not open a
  second database connection or issue independent delete SQL.

## Development checks

The validation workflow runs Python compilation and unit tests, JavaScript syntax and frontend tests, JSON validation, HACS validation, and Hassfest. The same local tests can be run with:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
node --check custom_components/gui_recorder/frontend/gui-recorder-panel.js
node tests/frontend.test.js
```

## Support

- Issues: https://github.com/Lisnol/gui-recorder/issues

## License

[MIT](LICENSE)
