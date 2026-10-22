# GUI Recorder

A Home Assistant sidebar panel to manage your `recorder` configuration and database maintenance (SQLite or MariaDB/MySQL) from a UI — no `configuration.yaml` editing required.

## What it does

- Enable or disable recording per device and per entity with a toggle.
- Database statistics: total / current / obsolete records, matched exclusions, and database size/location when available, including MariaDB data/index/free-allocation estimates and a MariaDB Health check.
- Background maintenance jobs with preflight summaries, 30-second checkpoints, optional Home Assistant notifications, live progress, and the latest 20 results.
- Purge the database, excluded entities, individual devices/entities, or a temporary multi-selection.
- Quick filters, sorting, collapsible sections, section-level selection, and pagination for large installations.
- Optional analysis refresh and repack after purge, plus Home Assistant restart.
- Detects orphan exclusions (entries that no longer match any entity) and removes them in bulk.
- Guided migration from a legacy `recorder:` block in `configuration.yaml`.
- Safe atomic YAML writes, managed-file recovery, and redacted Home Assistant diagnostics.

## Requirements

- Home Assistant 2024.1.0+
- Recorder database on **SQLite** or **MariaDB/MySQL** (PostgreSQL is not supported).
- MariaDB `db_url` values must use a Home Assistant `!secret` reference before guided migration.

## Setup

After installing, add the integration from **Settings → Devices & services → Add integration → GUI Recorder**. The panel appears in the sidebar.
