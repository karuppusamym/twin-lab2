# A stuck job, or no worker

Builds, Verify and retention runs are jobs in the `jobs` table, worked by `python -m twinlab2.worker`
(`server/twinlab2/jobqueue.py`). A worker leases a job and extends the lease while the job runs. When a worker dies, its
lease runs out and the next worker takes the job over. A job reclaimed 5 times in a row fails instead of looping.

## Look

```
oc get pods -l app.kubernetes.io/component=worker
oc logs deploy/twinlab2-worker --tail=200 | grep -E '"level": "(ERROR|WARNING)"|"outcome"'
```

The Build setup page (Setup, Build worker) lists the workers seen and the latest jobs. In SQL:

```sql
SELECT id, type, state, attempts, run_after, lease_owner, lease_until, error FROM jobs
 WHERE state IN ('queued', 'running') ORDER BY run_after;
SELECT id, host, seen_at, current_job, stopped_at FROM workers ORDER BY seen_at DESC LIMIT 5;
```

## By alert

- **TwinLab2NoWorker** (`twinlab2_workers_alive == 0`): the worker Deployment is down or cannot reach the
  database. Check `oc describe deploy/twinlab2-worker` and the pod's events: crash loops, OOMKilled (raise the
  memory limit; large builds need more), or image pull errors. The worker's probe passes while the database is
  down, so in that case look at `TwinLab2CatalogUnreachable` too.
- **TwinLab2JobQueueBacklog** (a due job has waited more than 15 minutes): there are workers, but they are busy or
  do not take that type (`--types`). Scale up with `oc scale deploy/twinlab2-worker --replicas=2`, which is safe
  because jobs are leased, or find the job that holds a worker for a long time (`current_job`).
- **TwinLab2JobLeaseExpired** (a `running` job whose lease ran out and that nobody took over): usually no worker
  is alive. Once one is, it takes the job over.
- **TwinLab2JobFailures** / **TwinLab2VerifyErrors**: read `jobs.error` and the worker's log lines for those
  `job_id`s (the `error` field holds the traceback). Common causes are a provider or GitHub token that expired
  (Build setup page), a deployment URL that no longer answers (Verify), or a bug. In that last case, take the
  `job_id` and the traceback to the developers.
- **TwinLab2RetentionNotRunning**: the daily retention job failed or was dropped. Open the Operations page, Retention runs.
  A failed run shows its error. Saving the policy again with **Run every day** queues the daily job again.

## Unstick one job by hand

Do this only when the cause is known and fixed. Prefer the portal's own actions: retry or cancel a build
on its page, or start Verify again.

```sql
-- try again now (counts reset)
UPDATE jobs SET state = 'queued', attempts = 0, run_after = to_char(now() at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
       lease_owner = NULL, lease_until = NULL, error = NULL WHERE id = '<job id>';
-- give up on it
UPDATE jobs SET state = 'failed', error = 'given up by an operator: <why>', lease_owner = NULL, lease_until = NULL
 WHERE id = '<job id>';
```

A build job that is given up this way leaves its build in its last state. Cancel the build on its page so the
application can start a new one.
