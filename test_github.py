import pytest

from asklit import github


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class DeviceHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(200, self.payload)


def test_device_flow_requests_repo_scope():
    http = DeviceHttp(
        {
            "device_code": "device-secret",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
    )

    result = github.request_device_code("client-id", http=http)

    assert result["user_code"] == "ABCD-EFGH"
    assert http.calls[0][1]["data"] == {"client_id": "client-id", "scope": "repo"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": "authorization_pending"}, "pending"),
        ({"access_token": "user-token", "token_type": "bearer"}, "complete"),
        ({"error": "access_denied"}, "denied"),
        ({"error": "expired_token"}, "expired"),
    ],
)
def test_poll_device_token_reports_safe_status(payload, expected):
    result = github.poll_device_token(
        "client-id", "device-code", http=DeviceHttp(payload)
    )
    assert result["status"] == expected


class PublishHttp:
    def __init__(self):
        self.posts = []
        self.puts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Response(
            201,
            {
                "full_name": "professor/demo",
                "default_branch": "main",
                "html_url": "https://github.com/professor/demo",
            },
        )

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return Response(201, {"content": {"path": url.rsplit("/", 1)[-1]}})


def test_publish_initializes_empty_repo_then_uses_default_branch(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    http = PublishHttp()
    progress = []

    result = github.publish_directory(
        "oauth-token",
        "demo",
        tmp_path,
        http=http,
        progress=lambda done, total, path: progress.append((done, total, path)),
    )

    assert result == {
        "full_name": "professor/demo",
        "html_url": "https://github.com/professor/demo",
        "files_published": 2,
    }
    assert http.posts[0][1]["json"] == {
        "name": "demo",
        "private": False,
        "auto_init": False,
    }
    assert "branch" not in http.puts[0][1]["json"]
    assert http.puts[0][0].endswith("/contents/README.md")
    assert http.puts[1][1]["json"]["branch"] == "main"
    assert http.puts[1][1]["headers"]["Authorization"] == "Bearer oauth-token"
    assert progress[-1] == (2, 2, "app.py")


def test_publish_preflights_large_files_before_creating_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(github, "MAX_CONTENT_API_FILE_BYTES", 3)
    (tmp_path / "large.pdf").write_bytes(b"four")
    http = PublishHttp()

    with pytest.raises(github.GitHubError, match="20 MB"):
        github.publish_directory("token", "demo", tmp_path, http=http)

    assert http.posts == []


def test_publish_omits_nested_python_and_tool_caches(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "asklit").mkdir()
    (tmp_path / "asklit" / "app.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "asklit" / "__pycache__").mkdir()
    (tmp_path / "asklit" / "__pycache__" / "app.pyc").write_bytes(b"cache")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "state").write_text("cache", encoding="utf-8")
    http = PublishHttp()

    result = github.publish_directory("token", "demo", tmp_path, http=http)

    published_urls = [url for url, _kwargs in http.puts]
    assert result["files_published"] == 2
    assert any(url.endswith("/contents/README.md") for url in published_urls)
    assert any(url.endswith("/contents/asklit/app.py") for url in published_urls)
    assert not any("__pycache__" in url for url in published_urls)
    assert not any(".pytest_cache" in url for url in published_urls)


def test_publish_can_explicitly_create_a_private_repository(tmp_path):
    (tmp_path / "README.md").write_text("# Private\n", encoding="utf-8")
    http = PublishHttp()

    github.publish_directory("token", "private-demo", tmp_path, private=True, http=http)

    assert http.posts[0][1]["json"]["private"] is True
