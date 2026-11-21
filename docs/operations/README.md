# Operating TwinLab2

> **Not yet run on a cluster.** These runbooks follow the code and the tests (`tests/server/test_operations.py`,
> `tests/server/test_deploy_manifests.py`). The `oc` steps have not been tried on a real OpenShift cluster.
> When you run one for real, note what differed in [VERIFICATION.md](../VERIFICATION.md).

| Runbook | When |
| --- | --- |
| [deploy.md](deploy.md) | a new version, the first install, `TwinLab2PortalDown`, `TwinLab2HighErrorRate`, `TwinLab2SlowRequests` |
| [migration-rollback.md](migration-rollback.md) | a release with a migration has to be undone |
| [rotate-secrets.md](rotate-secrets.md) | scheduled rotation, a person with access leaves, a secret leaked |
| [restore.md](restore.md) | lost or damaged database or bucket, `TwinLab2CatalogUnreachable`, restore drills |
| [stuck-job.md](stuck-job.md) | `TwinLab2NoWorker`, `TwinLab2JobQueueBacklog`, `TwinLab2JobLeaseExpired`, `TwinLab2JobFailures`, `TwinLab2VerifyErrors`, `TwinLab2RetentionNotRunning` |
| [full-disk.md](full-disk.md) | `TwinLab2ArtifactDiskFull`, the database volume filling |
| [leaked-picture.md](leaked-picture.md) | a picture or file with data that must not be kept is found in TwinLab2 |

## What you have to work with

- **Probes**: `/livez`, `/readyz` (database at the current migration, artifact store answers). See
  [deploying-on-openshift.md](../deploying-on-openshift.md).
- **Metrics**: `GET /metrics` on the pod's port 9090 only (the Route never reaches it), or on any port with
  `Authorization: Bearer $TWINLAB2_METRICS_TOKEN`. `deploy/openshift-monitoring.yaml` holds the
  ServiceMonitor and the alerts. Request metrics are labelled with the route template
  (`/api/v1/apps/{app_id}`). The queue, worker, Verify and storage gauges are read from the database at scrape time.
- **Logs**: with `TWINLAB2_LOG_FORMAT=json` (the image's default) each line is one JSON object with `request_id`,
  `user`, `app_id`, `route`, `status` and `ms`. Worker lines carry `job_id`, `job_type` and `outcome`.
  Tokens, passwords in URLs and signed download links are scrubbed. Evidence content is never logged.
  To find one failing request, take the `Request-Id` from its response (`request_id` in a problem body) and run
  `oc logs deploy/twinlab2 -c portal | grep '"request_id": "<id>"'`.
- **Tracing** (opt-in): `TWINLAB2_OTEL=1` plus the usual `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_SERVICE_NAME`.
  Each request is a span named after its route template, and each worker job is a span `job <type>`.
  An incoming `traceparent` is continued.
- **Admin command** (in any portal or worker pod): `python -m twinlab2.admin backup|restore|verify-backup|rewrap-local|purge|retention`.
  Run it with `--help` for the options.
- **Operations page** (`/admin/operations`, portal admins): storage and encryption, the retention policy and its runs,
  the audit export, tombstones, purge by SHA-256, and deleting a recording.

## Data lifecycle in one paragraph

Each stored file is an `artifacts` row plus an object (S3 or a local file). Deleting a file removes the object.
The row stays, marked `deleted_at`, and a **tombstone** records what, when, who and why: `retention`,
`owner_request` or `purge`. Release pages list their application's tombstones. A download of a deleted
file answers 410 `gone`. Retention (Operations page, or `TWINLAB2_RETENTION_<KIND>_DAYS` as defaults) runs as a
worker job, either on request or daily. A dry run only counts. Audit rows are append-only. Only retention may remove
rows older than its cutoff, through `audit_prune_window`, and an update is always refused.

## Encryption at rest

- **S3**: `TWINLAB2_S3_SSE=AES256` (SSE-S3) or `aws:kms` with `TWINLAB2_S3_SSE_KMS_KEY_ID`. Every put and multipart
  upload asks for it. Also turn on default encryption on the bucket, so objects that recorders upload straight to
  the bucket through presigned part URLs are covered too. The Operations page shows both the setting and the
  bucket's default.
- **Local files**: `TWINLAB2_ARTIFACT_KEY` (32 random bytes, base64: `openssl rand -base64 32`, from a Secret)
  turns on envelope encryption. Each file gets its own AES-256-GCM data key, wrapped by this key.
  `TWINLAB2_ARTIFACT_KEYS_PREVIOUS` lists old keys that may still be read. Files written before encryption was on
  stay readable until `rewrap-local` seals them. An upload in progress is staged unencrypted under
  `uploads/` until it completes or is discarded.
- **Backups** made with `twinlab2.admin backup` store objects sealed with the same key when one is set.
