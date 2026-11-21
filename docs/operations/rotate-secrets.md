# Rotate secrets

All secrets are in the Secret `twinlab2` (see the header of `deploy/openshift-portal.yaml`). After you change a key,
roll both Deployments so every pod reads the new value:

```
oc rollout restart deploy/twinlab2 deploy/twinlab2-worker
```

| Secret | What changing it does | How |
| --- | --- | --- |
| `secret-key` (`TWINLAB2_SECRET_KEY`) | signs session cookies, CSRF tokens and local download links. Everyone is signed out, and open download links stop working. | `oc patch secret twinlab2 -p "{\"stringData\":{\"secret-key\":\"$(openssl rand -hex 32)\"}}"`, then restart. All replicas must have the same value. |
| `cookie-secret` (OAuth proxy) | proxy sessions end, and people sign in with OpenShift again | as above, with `openssl rand -base64 32 \| head -c 32`, then restart |
| `database-url` | the database password | Add a second role, or set a new password on the role. Update the Secret, restart, then revoke the old password. PostgreSQL operators often rotate this themselves: point the Secret at the operator's generated Secret. |
| S3 credentials | — | Prefer the pod's role (no keys). With `AWS_*` keys: create a new key, update the Secret, restart, then delete the old key. |
| `TWINLAB2_METRICS_TOKEN` (if used) | Prometheus must send the new token | Update the Secret and the ServiceMonitor's `bearerTokenSecret` together. |
| GitHub and provider tokens | set on the Build setup page (stored in the `settings` table) | Replace them there, then revoke the old ones at the provider. |
| Recorder machine tokens | each paired machine has its own token | Revoke the machine (Machines page), then pair it again. |

## The local artifact key (`TWINLAB2_ARTIFACT_KEY`)

Only for the local artifact backend (compose, or a single-node install). S3 is covered by SSE.

1. Generate a new key: `openssl rand -base64 32`.
2. Set `TWINLAB2_ARTIFACT_KEY` to the **new** key, and `TWINLAB2_ARTIFACT_KEYS_PREVIOUS` to the **old** one.
   Restart. New files use the new key, and old files still read.
3. Re-wrap every file with the new key. This rewrites only each file's 81-byte header, and it also seals files from
   before encryption was on:
   ```
   python -m twinlab2.admin rewrap-local
   ```
   It prints `sealed: N, rewrapped: N, current: N, missing: N`. `missing` should be 0.
4. `python -m twinlab2.admin verify-backup` must end with `OK`.
5. Remove the old key from `TWINLAB2_ARTIFACT_KEYS_PREVIOUS` and restart.
6. Backups made with the old key still need it. Keep it with those backups until they expire.

If the key leaked, also rotate it in the backups. Restore each backup into a scratch catalog with both keys, then
back it up again with only the new key. Do this before you destroy the old key.

## After a person with access leaves

Rotate `secret-key`, which signs everyone out, and any token they could read. Remove their admin rights
(`TWINLAB2_ADMINS`, the Users page) and their application roles. Export the audit history for the period they had
access and keep it with the offboarding record.
