# A full disk

## Where space goes

| Where | What fills it | Metric |
| --- | --- | --- |
| S3 bucket | evidence, packages, release ZIPs, reports, recording bundles, and **noncurrent versions** | `twinlab2_artifact_bytes{kind}` (live objects only), plus the bucket's own metrics |
| local artifact folder (compose, single node) | the same, as files under `objects/`, plus uploads in progress under `uploads/` | `twinlab2_artifact_store_free_bytes`, `twinlab2_artifact_store_size_bytes` (alert `TwinLab2ArtifactDiskFull` below 10% free) |
| PostgreSQL volume | the catalog: steps, build messages, the audit history, and blobs of old catalogs not yet moved | your operator's volume metrics |
| pod ephemeral storage | the image's temp files while files are uploaded or built (`TWINLAB2_ARTIFACT_TMP`) | kubelet eviction events |

## Free space now

1. **See what retention would free**: Operations page, *Would go now*, or
   `python -m twinlab2.admin retention` (a dry run that prints counts and bytes per kind).
2. **Apply a policy** you can stand behind. Raw recording bundles (`raw_recordings`) are usually the biggest, and
   the steps and frozen packages remain without them. Do a **Dry run**, then **Apply now**. Every deleted
   object leaves a tombstone, and the run is in the audit history.
3. **Abandoned uploads** (local): files in `uploads/` older than a day belong to recorders that gave up. The portal
   deletes them when an upload is rejected. You can remove old `.part` files by hand
   (`find uploads -name '*.part' -mtime +2 -delete`).
4. **S3 noncurrent versions**: a lifecycle rule (e.g. expire noncurrent versions after 90 days, which must be
   longer than your database backups are kept) frees what deletions left behind. Purges already delete every
   version.
5. **Database**: `VACUUM (VERBOSE, ANALYZE)` after a large retention run of `build_logs` or `audit`. Run
   `SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;`
   to see the biggest tables. Blob columns of very old catalogs (`attachments.content`, `brief_packages.archive`,
   `releases.archive`) are emptied when their copy in artifact storage verifies (`artifacts.purge_blobs`).

## Grow it instead

- PVC: `oc patch pvc <name> -p '{"spec":{"resources":{"requests":{"storage":"200Gi"}}}}'`, if the storage class
  allows expansion.
- Move the local store to S3: set `TWINLAB2_ARTIFACT_BACKEND=s3`. Existing local artifacts stay readable only on the
  local backend, so copy them first. Do that with `admin backup`, then `admin restore` against a portal configured for S3.

## While it is full

Uploads fail (the recorder retries later and loses nothing, because it keeps its queue). Freezing a package
or making a release fails with a 500 whose log line says `No space left on device`. Reads keep working.
