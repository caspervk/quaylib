import asyncio
import email.utils
import json
import os
import subprocess
import tempfile
import textwrap
import time
from datetime import UTC, datetime
from typing import Any

import httpx

# Token for Quay HTTP API
QUAY_API_KEY = os.environ["QUAY_API_KEY"]

# Registry authentication for skopeo
AUTH_JSON = {
    "auths": {
        # To avoid rate limits
        # https://docs.docker.com/docker-hub/usage/
        "docker.io": {
            "auth": os.environ["DOCKER_REGISTRY_AUTH"],
        },
        # Must be owner of the organisation
        "quay.io": {
            "auth": os.environ["QUAY_REGISTRY_AUTH"],
        },
    }
}


class DockerClient(httpx.AsyncClient):
    async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
        """Back off and retry requests if rate-limited."""
        # https://docs.docker.com/reference/api/hub/latest/#tag/rate-limiting
        r = await super().request(*args, **kwargs)
        if r.status_code != 429:  # Too Many Requests
            return r
        await asyncio.sleep(int(r.headers["Retry-After"]) - time.time())
        return await self.request(*args, **kwargs)


# https://docs.docker.com/reference/api/hub/latest/
docker = DockerClient(
    base_url="https://hub.docker.com/v2/",
    # Reading pages of a hundred elements can take some time
    timeout=30,
)

# https://docs.quay.io/api/
quay = httpx.AsyncClient(
    base_url="https://quay.io/api/v1/",
    headers={"Authorization": f"Bearer {QUAY_API_KEY}"},
    # Reading pages of a hundred elements can take some time
    timeout=30,
)


async def ensure_quay_repo(repo: str) -> None:
    """Ensure Quay repo exists with the correct description."""
    # Check if repo has logo on Docker Hub
    logo_url = f"https://hub.docker.com/api/media/repos_logo/v1/library%2F{repo}"
    r = await docker.get(logo_url)  # cannot be HEAD
    logo = (
        f'<img src="{logo_url}" height="34"/>'
        if r.is_success or r.is_redirect
        else None
    )

    # Compose description from Docker Hub
    r = await docker.get(f"namespaces/library/repositories/{repo}")
    r.raise_for_status()
    info = r.json()
    preamble = textwrap.dedent(
        f"""\
        {logo or ""} {info["description"]}

        ---

        > _**NOTE**: Digests for multi-platform images may differ from Docker
        Hub if manifests were converted to OCI format. Please pin to a
        [platform-specific sha256 digest](https://hub.docker.com/_/{repo}/tags?name=latest)
        instead._

        ---

        """,
    )
    description = preamble + info["full_description"]

    # Create Quay repo if it doesn't exist
    try:
        r = await quay.get(f"repository/lib/{repo}")
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
        r = await quay.post(
            "repository",
            json={
                "namespace": "lib",
                "repository": repo,
                "description": description,
                "visibility": "public",
                "repo_kind": "image",
            },
        )
        r.raise_for_status()
        return

    # Ensure repo has correct description
    if r.json()["description"] == description:
        return
    r = await quay.put(
        f"repository/lib/{repo}",
        json={
            "description": description,
        },
    )
    r.raise_for_status()


async def get_tags_docker(repo: str) -> dict[str, datetime]:
    tags = {}
    url = f"namespaces/library/repositories/{repo}/tags?page_size=100"
    while url:
        r = await docker.get(url)
        r.raise_for_status()
        j = r.json()
        for result in j["results"]:
            tags[result["name"]] = datetime.fromisoformat(result["last_updated"])
        url = j["next"]
    return tags


async def get_tags_quay(repo: str) -> dict[str, datetime]:
    tags = {}
    page = 1
    while page:
        r = await quay.get(
            url=f"repository/lib/{repo}/tag/",
            params={
                "page": page,
                "limit": 100,
                # there can be more 'latest' tags if it was moved
                "onlyActiveTags": True,
            },
        )
        r.raise_for_status()
        j = r.json()
        for tag in j["tags"]:
            d = email.utils.parsedate_to_datetime(tag["last_modified"])
            if d.tzinfo is None:
                d = d.replace(tzinfo=UTC)
            tags[tag["name"]] = d
        if j["has_additional"]:
            page += 1
        else:
            page = None
    return tags


async def sync(repo: str, auth_file: str) -> None:
    print("Synchronising", repo)
    _, tags_docker, tags_quay = await asyncio.gather(
        ensure_quay_repo(repo),
        get_tags_docker(repo),
        get_tags_quay(repo),
    )
    for tag, docker_modified in tags_docker.items():
        if tag in tags_quay and tags_quay[tag] >= docker_modified:
            continue
        subprocess.run(
            args=[
                "skopeo",
                "sync",
                # Transport types
                "--src=docker",
                "--dest=docker",
                # Use temporary credentials file for registry authentication
                f"--authfile={auth_file}",
                # Copy image for all OS and architecture variants
                "--all",
                # Source/destination
                f"docker.io/library/{repo}:{tag}",
                "quay.io/lib",
                # Quay only supports OCI manifests
                "--format=oci",
            ],
            check=True,
        )


async def main() -> None:
    with tempfile.NamedTemporaryFile(mode="w+") as auth_file:
        # Skopeo does not support credentials through environment variables
        json.dump(AUTH_JSON, auth_file)
        auth_file.flush()

        # Get all docker.io/library repositories
        url = "namespaces/library/repositories?page_size=100"
        while url:
            r = await docker.get(url)
            r.raise_for_status()
            j = r.json()
            for result in j["results"]:
                await sync(result["name"], auth_file.name)
            url = j["next"]


def cli() -> None:
    asyncio.run(main())
