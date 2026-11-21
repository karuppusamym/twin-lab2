"""Conditional updates of owner-edited resources (an assignment, a scenario, an application's
purpose and setup).

Each such row has a `revision` (1 when created, +1 on every change; an assignment not saved yet
is revision 0). An update may say which revision it was made from: `expected_revision` in the
body (or the query, for a DELETE) or an `If-Match: "<revision>"` header. When that revision is
no longer the current one, someone else changed the resource since it was loaded: the update is
refused with 409 problem `code: conflict`, and the problem's `current` is the resource as the
server has it now, so the page can show "yours" next to "theirs". An update that names no
revision is applied as before (scripts that mean to overwrite).

The check and the write happen in one statement (`UPDATE ... WHERE revision = ?`): two owners
saving at once cannot both win.
"""

from __future__ import annotations

from twinlab2.rest import Problem, g

CONFLICT = "conflict"


def expected(data: dict | None = None) -> int | None:
    """The revision an update was made from: `expected_revision` of the body or the query, else
    the If-Match header; None when the update names none."""
    value = (data or {}).get("expected_revision")
    request = g.request
    if value is None and request is not None:
        value = request.query_params.get("expected_revision")
        if value is None:
            match = request.headers.get("if-match", "").strip()
            value = match.removeprefix("W/").strip('"') if match and match != "*" else None
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise Problem(400, "“expected_revision” must be a revision number.",
                      errors=[{"field": "expected_revision", "message": "not a number"}]) from None
    if number < 0:
        raise Problem(400, "“expected_revision” must be a revision number.",
                      errors=[{"field": "expected_revision", "message": "negative"}])
    return number


def conflict(current: dict, what: str = "this") -> Problem:
    """The 409 to raise: `current` is the resource as the server has it now."""
    path = g.request.url.path if g.request is not None else ""
    body = {"type": "about:blank", "title": "Conflict", "status": 409, "code": CONFLICT, "instance": path,
            "detail": f"Someone else changed {what} since you opened it (now revision {current.get('revision')}). "
                      "Compare your changes with theirs, then keep yours or take theirs.",
            "current": current}
    return Problem(409, body["detail"], CONFLICT, body=body, headers={"content-type": "application/problem+json"})
