# Requesting access to an application

Roles are per application (viewer < builder < owner; see the README's *Access* section). When
someone opens an application page they have no role for, or one that needs a higher role, the
portal answers 403 with `code: role`. The web app then shows, under the refusal:

- **who can grant the role**: the application's owners by name, when the person already holds a
  role on it (or is an admin). Someone with no role on it learns only "the application's owners,
  or a portal admin": a 403 does not reveal whether an application exists or who owns it, and
  the request below keeps that so;
- **Request access**: a role (pre-selected: the lowest role that page needs) and a reason. One
  pending request per person and application; asking again changes its role and reason. The
  answer is the same whether or not the application exists.

The problem's members (`api/openapi.yaml`, `Problem`): `app_id`, `requested_role`, and `owners`
where the caller may see them.

## Deciding

Pending requests appear on the **Home** page of the application's owners (and of every portal
admin, who manages role assignments) under *Access requests*: who asks, for which role, on which
application, and why. **Approve** assigns the role exactly as the application's access table does
(an approval never lowers a role the person holds by then); **Decline** assigns nothing. Either can
carry a note for the requester.

The requester sees the outcome on their own Home page (*Access requests*: waiting, approved or
declined, by whom, with the note), and on the refused page while it is pending.

## Audit

Every step is in the application's audit history (`audit_log`, *Audit history*):

| Action | When | Details |
| --- | --- | --- |
| `access.request` | A request is sent or changed | user, role, reason, the role held then |
| `access.approve` | An owner or admin approves | user, role given, role requested, note |
| `access.decline` | An owner or admin declines | user, role requested, note |
| `role.assign` | The role an approval gives | user, role, previous role, `via: access request` |

## API

- `POST /api/v1/apps/{app_id}/access-requests` `{role, reason}` → 202 (any signed-in user)
- `GET /api/v1/access-requests` → `{mine, to_decide}`
- `POST /api/v1/access-requests/{id}/decision` `{decision: approve | decline, role?, note?}` (owners
  of the application, portal admins) → the request; 409 `state_conflict` once decided
