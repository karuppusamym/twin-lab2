# Back up and restore

TwinLab2's state is in two places: the **catalog** (PostgreSQL) and the **objects** (the S3 bucket, or the local
artifact folder). The catalog holds the SHA-256 of every object. A restore is good when every catalog row finds its
object with that hash, which is what `verify-backup` checks.

## What to back up

| Part | Primary backup | Check |
| --- | --- | --- |
| PostgreSQL | the operator's continuous backup (CloudNativePG `Backup`/`ScheduledBackup`, Crunchy PGO pgBackRest), or a nightly `pg_dump -Fc` | restore drill below |
| S3 bucket | **versioning on** (required), plus replication to a second bucket or a backup vault, and a lifecycle rule that keeps noncurrent versions for at least as long as database backups | `verify-backup` after a restore |
| Both, portable | `python -m twinlab2.admin backup DIR`: every table as JSON lines plus every live object, with a manifest of hashes. Works with SQLite and PostgreSQL, and can restore into either. | `verify-backup DIR` |

Take the database backup **after** the bucket is safe. An object that appears after the database backup is
harmless, but a catalog row whose object is gone is a problem.

### pg_dump

```
oc exec <postgres-pod> -- pg_dump -Fc -d twinlab2 > twinlab2-$(date +%F).dump
```

### The portable backup

```
oc exec deploy/twinlab2-worker -- python -m twinlab2.admin backup /backup/twinlab2-$(date +%F)
oc exec deploy/twinlab2-worker -- python -m twinlab2.admin verify-backup /backup/twinlab2-$(date +%F)
```

(`/backup` is a volume you mount for this.) It refuses a folder that is not empty. `--no-objects` leaves out the
objects when the bucket has its own backup. When `TWINLAB2_ARTIFACT_KEY` is set, objects are stored sealed, and
the backup needs that key to restore.

## Restore

Always restore into a **new, empty** database. Keep the damaged one until the new one verifies.

1. Stop writes: `oc scale deploy/twinlab2 deploy/twinlab2-worker --replicas=0`.
2. Create the database with `--locale=C --encoding=UTF8` and an owner role.
3. Load it, in one of two ways:
   - **pg_dump**: `pg_restore --no-owner --role=twinlab2 -d twinlab2_restored twinlab2-DATE.dump`
   - **operator**: restore the cluster from its backup (a new cluster with `bootstrap.recovery` in CloudNativePG,
     or a PGO restore), to a point in time before the damage if you need to.
   - **portable backup**: `python -m twinlab2.admin restore DIR --database postgresql://…/twinlab2_restored`.
     It applies the migrations, loads every row, moves the id sequences past them, writes each object to the
     configured store, then runs the checks. Add `--no-objects` when the bucket still has them.
4. Objects: if the bucket was damaged, restore it from replication or the backup vault. With versioning, you can
   also restore specific versions. The catalog names each object's exact version, so restore
   **those versions**, not just the current ones.
5. Check it:
   ```
   DATABASE_URL=postgresql://…/twinlab2_restored python -m twinlab2.admin verify-backup [DIR]
   ```
   It checks that the schema is current. It checks that every artifact row not deleted has its object with the
   right SHA-256 and size, and that every row that refers to an artifact finds it. Every frozen package must match
   its approved hash, and with `DIR`, the row counts must equal the backup's. Exit status 0 and
   `OK: everything checked out.` mean the restore is good. Otherwise each problem is listed.
6. Point the `database-url` Secret at the restored database, scale up, and check `/readyz`.

## Restore drill (do it every quarter)

Do steps 2 to 5 into a scratch database and a scratch bucket prefix, without touching production, and write down how
long it took. The test `tests/server/test_operations.py::BackupRestoreTest` does the same on every CI run, on
SQLite and PostgreSQL.

## TwinLab2CatalogUnreachable

The portal cannot open the database. First check the PostgreSQL pods, connections (`max_connections`) and the
Secret. Restore only if the data is gone or damaged. An outage alone means waiting, because the pods become
unready and stop serving until the database answers again.
