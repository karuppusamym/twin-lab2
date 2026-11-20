"""Shared test steps: freezing a builder package the way an owner does."""

import re


def freeze(client, token, app_path, summary=""):
    """Preview the draft, approve its SHA-256 and freeze it (a draft identical to the latest
    revision is not frozen again), then download the latest builder package."""
    page = client.get(app_path + "/build-pack/preview")
    if page.status_code != 200:
        return page
    sha = re.search(r'id="archive-sha256">([0-9a-f]{64})<', page.data.decode()).group(1)
    response = client.post(app_path + "/packages", data={"token": token, "approve": "yes", "sha256": sha,
                                                         "summary": summary})
    assert response.status_code in (302, 409), response.data
    return client.get(app_path + "/build-pack")
