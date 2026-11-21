# A leaked picture: purge it everywhere

Use this when a stored file holds data that must not be kept, such as a screenshot showing a customer's card number,
a password on screen, or personal data recorded by mistake. The goal is that no copy stays anywhere TwinLab2 put it,
and the record shows that it was removed, when and why, without keeping the content.

## 1. Contain

- Note who reported it and the incident number. Do **not** download the file again or share it to "show" it.
- If it is still being shared (a release ZIP that was published, or a package handed to a builder), tell the
  people who have it to delete their copies. TwinLab2 cannot reach copies outside it.

## 2. Find its SHA-256

TwinLab2 stores files by content, so one SHA-256 finds every copy.

- If you have the file: `sha256sum picture.png`.
- If you know where it is in TwinLab2: the attachment, package or release pages list file checksums. A frozen
  package's `evidence.json` and its manifest list each file's SHA-256.
- If it came in a recording, delete the whole recording (step 4) instead, or as well.

## 3. Purge

On the **Operations page** (portal admins): *Purge a file everywhere*. Enter the SHA-256 and select **Find**. The
page lists the stored copies and the stored ZIPs that contain it (frozen packages, release downloads, reports,
recording bundles). Enter the reason, including the incident number, and select **Purge**.

Or from a pod:

```
python -m twinlab2.admin purge <sha256> --dry-run          # where it is
python -m twinlab2.admin purge <sha256> --note "INC-1234: card number on screen" --actor <your name>
```

This deletes:

- every artifact that *is* the file, and on S3 **every version** of its content-addressed key, including versions
  the catalog never recorded;
- the attachments that pointed at it;
- every stored ZIP that contains it, **whole**, because a ZIP cannot be partly purged. A frozen package or release
  download that held it is then gone. Its row and hash remain, its downloads answer 410 `gone`, and a tombstone on the
  application's pages says what was removed and why;
- nothing in the audit history except the record of the purge itself.

Run the dry run again afterwards. It must find nothing.

## 4. If it came in a recording

The application's owner, or an admin, deletes the recording on the Operations page (*Delete a recording*) or with
`POST /api/v1/recordings/{id}/deletion` and `{"reason": "…", "purge_packages": true}`. This deletes the steps,
the assignments, the uploaded bundle and the frozen packages that include the recording. The recorder's machine
may still have its local copy in the upload queue until the upload confirmed. Have the machine's user clear the
recorder's queue folder.

## 5. Outside the portal

| Where | What to do |
| --- | --- |
| S3 replication target / backup vault | Delete the same key's versions there, because replication does not replay deletes of versions. Use the keys from the tombstones: `details.containers` and the object keys in `artifacts` for the listed ids. |
| `admin backup` folders | The file is under `objects/<sha256>`, and a ZIP that held it under its own hash. Delete both from every backup set, then run `verify-backup DIR`, which reports exactly those objects as missing. Record that in the incident. |
| PostgreSQL backups | They hold catalog rows only, not the picture (the catalog stores hashes and steps, never pictures). Nothing to do, unless it was in an old catalog's blob column. |
| Builders' repositories | A package handed to a build session may be in the built application's repository. Ask the repository owner to remove it from history. |
| Logs | The portal never logs file content. Nothing to do. |

## 6. Close

Export the audit history for the day (Operations page, *Audit export*). The `purge` row and the tombstones show
what was removed, by whom and why. Attach the export to the incident.
