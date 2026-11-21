# Deploying on OpenShift

> **Not yet run on a cluster.** Everything below was checked locally only. The manifest passes a
> kubeconform schema check against the Kubernetes 1.30 (OpenShift 4.17) schemas in strict mode,
> and the structural tests in `tests/server/test_deploy_manifests.py`. In the `image` CI job, the
> image runs as an arbitrary UID in group 0 and answers its probes with a pod-IP `Host` header.
> Nothing here has been applied to an OpenShift cluster, and the OAuth proxy, Route, serving
> certificate and rollout behaviour are unverified. Review the manifest before using it, and
> record what you find in [VERIFICATION.md](VERIFICATION.md).

## What gets deployed

`deploy/openshift-portal.yaml` is a `List` with these objects:

| Object | What it is |
| --- | --- |
| ServiceAccount `twinlab2` | used by the OAuth proxy to sign people in with OpenShift accounts |
| Deployment `twinlab2` (2 replicas) | the portal (gunicorn on `127.0.0.1:8080`) and its `oauth-proxy` sidecar (`:8443`) |
| PodDisruptionBudget `twinlab2` | keeps one portal pod serving during a node drain |
| Deployment `twinlab2-worker` (1 replica) | the build worker (`python -m twinlab2.worker`), from the same image |
| Service `twinlab2` | the proxy's port 8443, with an OpenShift serving certificate (`twinlab2-tls`) |
| Route `twinlab2` | TLS re-encrypt to the Service; plain HTTP redirects |

The portal keeps no state in the pod. PostgreSQL holds the catalog and the sign-in sessions, and S3
holds the artifacts. That is why it can run as 2 replicas and roll pods one at a time.

## Before you apply

1. **PostgreSQL**: a database and an owner role for TwinLab2, created with `--locale=C
   --encoding=UTF8` (byte-order collation, as the tests use). The portal applies its migrations at
   start, under an advisory lock.
2. **S3 bucket** with versioning enabled. Give the pods credentials through their role, or through
   `AWS_*` variables from a Secret you add.
3. **Image**: build `deploy/Dockerfile` and push it, then set `REGISTRY/twinlab2-portal:TAG` in
   both Deployments (they must use the same tag).
4. **Host**: set `twinlab2.apps.example.test` in `TWINLAB2_HOSTS` and in the Route.
5. **OAuth proxy image**: the manifest uses `ose-oauth-proxy:v4.17`. Use the tag that matches your
   cluster.
6. **The Secret** (the tests check that every `secretKeyRef` in the manifest names one of these
   keys):

   ```
   oc create secret generic twinlab2 \
     --from-literal=secret-key=$(openssl rand -hex 32) \
     --from-literal=cookie-secret=$(openssl rand -base64 32 | head -c 32) \
     --from-literal=database-url='postgresql://twinlab2:PASSWORD@POSTGRES-HOST:5432/twinlab2?sslmode=require'
   ```

   The portal and the worker both read `secret-key` and `database-url`. The proxy reads
   `cookie-secret`. `twinlab2-tls` is created by OpenShift from the Service annotation.

Then run `oc apply -f deploy/openshift-portal.yaml`.

## Settings the manifest fixes

- `TWINLAB2_AUTH_MODE=trusted-proxy`: the user comes only from the proxy's `X-Forwarded-User`, and
  only on requests from `TWINLAB2_TRUSTED_PROXIES` (the pod's loopback). There is no local sign-in
  form.
- `TWINLAB2_SECURE_COOKIES=1`: the session cookie is sent over HTTPS only.
- Recorders reach `/api/agent/` and `/api/uploads` through the proxy without a sign-in, using their
  machine token. The kubelet reaches `/livez`, `/readyz` and `/healthz` the same way.

## Health checks

| Endpoint | Answers 200 when | Otherwise |
| --- | --- | --- |
| `/livez` | the process answers | (no answer: the container is restarted) |
| `/readyz` | the database answers at the current migration, and the artifact store answers (S3 `HeadBucket`, or the local folder is writable) | 503, `Retry-After: 5`, and `checks` says which part failed |
| `/healthz` | alias of `/readyz`, kept for older monitors | |

All three answer whatever the `Host` header says (a kubelet sends `<pod-ip>:<port>`), and they
are not logged. Every other path still checks the host against `TWINLAB2_HOSTS`.

The probes in the manifest:

- **portal**: the portal is bound to `127.0.0.1`, so the kubelet cannot reach it directly.
  - Readiness is an `httpGet` of `/readyz` through the proxy on `:8443`, the same path the Route
    uses.
  - Liveness and startup run `python -m twinlab2.probe http http://127.0.0.1:8080/livez` inside
    the container.
  - The startup probe allows 5 minutes, because migrations run before gunicorn's workers start
    listening.
- **oauth-proxy**: `/oauth/healthz` for readiness and liveness.
- **worker**: `python -m twinlab2.probe worker` checks that the worker loop, or the heartbeat of
  the job it is running, has written `workers.seen_at` for this pod within 180 s. If the database
  doesn't answer, the probe passes, because restarting the worker wouldn't fix that.

## Rollouts without 502s

- `replicas: 2`, `maxUnavailable: 0`, `maxSurge: 1`: a new pod must pass `/readyz` before an old one
  is taken out.
- The portal's `preStop` waits 10 s, so the endpoints controller and the router can stop sending
  to the pod while it still serves. The proxy's `preStop` uses Kubernetes' built-in `sleep`
  action, which needs **Kubernetes 1.30+ / OpenShift 4.17+**. On older clusters, drop it and
  expect a few failed requests per rollout.
- `terminationGracePeriodSeconds: 40` covers the 10 s drain plus gunicorn's 30 s graceful
  shutdown.
- **Single replica**: a SQLite catalog on a volume (no `DATABASE_URL`) allows only one replica, with
  `strategy: Recreate`. Each rollout is then a short outage.

## Verifying a deployment

```
oc rollout status deploy/twinlab2 deploy/twinlab2-worker
oc exec deploy/twinlab2 -c portal -- python -m twinlab2.probe http http://127.0.0.1:8080/readyz
oc exec deploy/twinlab2-worker -- python -m twinlab2.probe worker
curl -sI https://<host>/api/v1/session | grep -i request-id
```

Every portal log line has the request id in brackets. A problem response repeats it as
`request_id`, so quote it when reporting an error.

## What only a real cluster can prove

- The OAuth proxy passes `X-Forwarded-User` to the portal and the sign-in flow works with the
  ServiceAccount redirect reference.
- The `skip-auth-regex` paths, including the kubelet's HTTPS readiness probe through the proxy,
  really bypass sign-in.
- A rolling update under load produces no 502s, with the preStop timings tuned for your router.
- The arbitrary UID that the restricted-v2 SCC assigns can write what the image needs. CI tests UID
  123456 in group 0 only.
- Reaching S3 from the pods (role or keys, network egress) and PostgreSQL over `sslmode=require`.
- Resource requests and limits under real use, including the worker during large builds.
- The `ose-oauth-proxy` tag and the native `preStop.sleep` on your cluster version.
