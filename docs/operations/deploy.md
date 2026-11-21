# Deploy a new version

The portal and the worker run from one image and must have the **same tag**. The portal applies pending migrations at start,
under an advisory lock, so two replicas starting together are safe. A migration never runs twice.

## Before

1. Read the release notes for **migrations** (`server/twinlab2/migrations.py`, `MIGRATIONS`).
   A migration that only adds columns or tables can be rolled back by redeploying the old image (see
   [migration-rollback.md](migration-rollback.md)). One that changes or drops data needs a backup first.
2. Take a backup: an on-demand backup through your PostgreSQL operator, or `pg_dump` (see [restore.md](restore.md)).
   Then check it:
   ```
   oc exec deploy/twinlab2-worker -- python -m twinlab2.admin verify-backup
   ```
   That checks the live catalog against the artifact store. It should end with `OK: everything checked out.`
3. Build and push `deploy/Dockerfile`, then note the tag.

## Roll out

```
oc set image deploy/twinlab2 portal=REGISTRY/twinlab2-portal:NEW
oc set image deploy/twinlab2-worker worker=REGISTRY/twinlab2-portal:NEW
oc rollout status deploy/twinlab2 deploy/twinlab2-worker --timeout=10m
```

`maxUnavailable: 0` keeps old pods serving until a new one passes `/readyz`. The startup probe gives the
migrations 5 minutes.

## Check

```
oc exec deploy/twinlab2 -c portal -- python -m twinlab2.probe http http://127.0.0.1:8080/readyz
oc exec deploy/twinlab2-worker -- python -m twinlab2.probe worker
oc exec deploy/twinlab2 -c portal -- python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9090/metrics').read().decode()[:400])"
```

In Prometheus, `twinlab2_catalog_up` is 1 and `twinlab2_workers_alive` is at least 1. The 5xx rate
(`TwinLab2HighErrorRate`) stays flat.

## If it goes wrong

- **New pods never become ready**: `oc describe pod`, then `oc logs <pod> -c portal`.
  A refused setting (`ConfigError`) names the variable. `readyz` reports `migrating (at N, needs M)` while a
  migration runs or after one failed. Look for the migration's error in the first pod's log.
- **Errors after the rollout**: `oc rollout undo deploy/twinlab2 deploy/twinlab2-worker`.
  If the release had a migration, read [migration-rollback.md](migration-rollback.md) first.
- **`TwinLab2PortalDown`**: Prometheus cannot scrape a pod. Check that the pod is Running and that port 9090 is in
  the Service (`oc get svc twinlab2 -o yaml`). Also check that `PROMETHEUS_MULTIPROC_DIR` is writable (the `metrics`
  emptyDir).
- **`TwinLab2SlowRequests`**: find the slow route with
  `topk(5, histogram_quantile(0.95, sum by (le, route) (rate(twinlab2_http_request_duration_seconds_bucket[5m]))))`.
  Then check the database (connections, locks) and the node's CPU throttling.
