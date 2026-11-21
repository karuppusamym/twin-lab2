# Roll back a migration

Migrations go **forward only**. Each one is a numbered entry in `server/twinlab2/migrations.py`, recorded in
`schema_migrations`. There are no down-migrations. Rolling back means running the previous image on a schema it
can live with, or restoring the database from before the migration.

## 1. Can the old image run on the new schema?

`python -m twinlab2.migrations status` (in a pod) lists what is applied.

- **Additive migrations** (new tables, new nullable or defaulted columns, new indexes, which is what nearly every
  TwinLab2 migration is): the old code ignores what it does not know. However, the old portal's `/readyz` compares
  the applied version with its own `latest()`, and reports ready because the catalog is *ahead*, not behind.
  **Roll back the image only:**
  ```
  oc rollout undo deploy/twinlab2 deploy/twinlab2-worker
  ```
  Keep the extra tables. When the fixed version is deployed, it finds the migration already applied.
- **A migration that rewrote or dropped data**: the old image may misread it. Restore instead (step 2).

Migration 15 (data lifecycle) is additive. It adds `tombstones`, `retention_runs` and `audit_prune_window`, and
columns on `artifacts`, and it replaces the audit log's delete guard. An old image runs on it unchanged.
Downloads of files deleted after the upgrade then fail with "not found" instead of 410 `gone`.

## 2. Restore the database from before the migration

1. Stop writes: `oc scale deploy/twinlab2 deploy/twinlab2-worker --replicas=0`.
2. Restore the backup you took before the rollout into a **new** database ([restore.md](restore.md)). Keep the
   current database until the restore verifies.
3. Point `database-url` in the `twinlab2` Secret at the restored database, then deploy the **old** image tag.
4. `python -m twinlab2.admin verify-backup` (live catalog against the store). Objects added after the backup are
   in the bucket but not in the catalog. That is harmless, but they cannot be seen in the portal.
5. Scale back up.

Anything people did between the backup and the restore is lost from the catalog. The audit export of the newer
database (Operations page, or `GET /api/v1/admin/audit-export?since=…`) tells you what to redo.

## 3. Never

- Delete rows from `schema_migrations` to "re-run" a migration. The steps assume they run once.
- Edit a migration that has shipped. Add the next number instead.
