"""GitHub OAuth device flow and repository publishing helpers."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Callable

import requests


GITHUB_API_URL = "https://api.github.com"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_VERSION = "2022-11-28"
MAX_CONTENT_API_FILE_BYTES = 20 * 1024 * 1024
PUBLISH_IGNORED_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".DS_Store",
    ".coverage",
    "htmlcov",
}
PUBLISH_IGNORED_SUFFIXES = (".pyc", ".pyo")


class GitHubError(RuntimeError):
    """A safe, user-displayable GitHub integration error."""


def _response_error(response, fallback):
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = {}
    message = payload.get("error_description") or payload.get("message") or fallback
    return f"{message} (GitHub returned HTTP {response.status_code})."


def _api_headers(token):
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def request_device_code(client_id, timeout=20, http=requests):
    """Begin GitHub's OAuth device flow without navigating away from Streamlit."""
    response = http.post(
        GITHUB_DEVICE_CODE_URL,
        headers={"Accept": "application/json"},
        data={"client_id": client_id, "scope": "repo"},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise GitHubError(_response_error(response, "Could not start GitHub authorization"))
    payload = response.json()
    required = {"device_code", "user_code", "verification_uri", "expires_in"}
    if not required.issubset(payload):
        raise GitHubError("GitHub returned an incomplete device authorization response.")
    return payload


def poll_device_token(client_id, device_code, timeout=20, http=requests):
    """Check a pending device authorization once.

    Returns a status dictionary so the UI can ask the user to retry without
    blocking a Streamlit worker in a polling loop.
    """
    response = http.post(
        GITHUB_ACCESS_TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        timeout=timeout,
    )
    if response.status_code != 200:
        raise GitHubError(_response_error(response, "Could not complete GitHub authorization"))
    payload = response.json()
    if payload.get("access_token"):
        return {"status": "complete", "access_token": payload["access_token"]}

    error = payload.get("error")
    if error in {"authorization_pending", "slow_down"}:
        return {
            "status": "pending",
            "message": payload.get("error_description")
            or "GitHub authorization is still pending.",
        }
    if error == "access_denied":
        return {"status": "denied", "message": "GitHub authorization was denied."}
    if error == "expired_token":
        return {"status": "expired", "message": "The GitHub device code expired."}
    raise GitHubError(payload.get("error_description") or "GitHub authorization failed.")


def get_authenticated_user(token, timeout=20, http=requests):
    response = http.get(
        f"{GITHUB_API_URL}/user",
        headers=_api_headers(token),
        timeout=timeout,
    )
    if response.status_code != 200:
        raise GitHubError(_response_error(response, "GitHub authorization is invalid"))
    return response.json()


def collect_publish_files(directory):
    """Return deterministic, preflighted files for the Contents API."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise GitHubError("The generated project directory is unavailable.")

    files = []
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if any(part in PUBLISH_IGNORED_PARTS for part in relative_path.parts):
            continue
        if path.name.endswith(PUBLISH_IGNORED_SUFFIXES):
            continue
        if path.is_symlink():
            raise GitHubError(f"Generated project contains an unsupported link: {path.name}")
        if not path.is_file():
            continue
        relative = relative_path.as_posix()
        size = path.stat().st_size
        if size > MAX_CONTENT_API_FILE_BYTES:
            raise GitHubError(
                f"{relative} is larger than the 20 MB direct-publishing limit. "
                "Use the ZIP download for this project."
            )
        files.append((relative, path))

    if not files:
        raise GitHubError("The generated project contains no files to publish.")
    # Initializing an empty repository with README makes the branch behavior
    # predictable. Fall back to the first path if a README is not present.
    files.sort(key=lambda item: (item[0].lower() != "readme.md", item[0]))
    return files


def publish_directory(
    token,
    repo_name,
    directory,
    *,
    private=False,
    timeout=20,
    http=requests,
    progress: Callable[[int, int, str], None] | None = None,
):
    """Create a repository and publish a generated bundle serially."""
    files = collect_publish_files(directory)
    response = http.post(
        f"{GITHUB_API_URL}/user/repos",
        headers=_api_headers(token),
        json={"name": repo_name, "private": private, "auto_init": False},
        timeout=timeout,
    )
    if response.status_code != 201:
        raise GitHubError(_response_error(response, "Could not create the repository"))

    repository = response.json()
    full_name = repository["full_name"]
    default_branch = repository.get("default_branch") or "main"
    total = len(files)

    for index, (relative, path) in enumerate(files):
        with path.open("rb") as source:
            content = base64.b64encode(source.read()).decode("ascii")
        body = {"message": f"Add {relative}", "content": content}
        # Omitting branch on the first Contents API call initializes an empty repo.
        if index:
            body["branch"] = default_branch
        put_response = http.put(
            f"{GITHUB_API_URL}/repos/{full_name}/contents/{relative}",
            headers=_api_headers(token),
            json=body,
            timeout=timeout,
        )
        if put_response.status_code not in {200, 201}:
            raise GitHubError(
                _response_error(put_response, f"Could not publish {relative}")
            )
        if progress:
            progress(index + 1, total, relative)

    return {
        "full_name": full_name,
        "html_url": repository.get("html_url") or f"https://github.com/{full_name}",
        "files_published": total,
    }
