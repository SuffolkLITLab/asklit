"""Private original-document/export storage, independent of API/worker host disks."""

import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask


def checked_key(key):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:pdf|docx|txt|md|html|htm|zip)", key):
        raise ValueError("Invalid object key.")
    return key


class FilesystemStorage:
    def __init__(self, root):
        self.root = root

    def put_bytes(self, key, data):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / checked_key(key)).write_bytes(data)

    def put_file(self, key, source):
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, self.root / checked_key(key))

    @contextmanager
    def materialize(self, key):
        path = self.root / checked_key(key)
        if not path.is_file():
            raise FileNotFoundError("Object not found.")
        yield path

    def download_response(self, key):
        with self.materialize(key) as path:
            return FileResponse(
                path, filename="asklit-streamlit.zip", media_type="application/zip"
            )


class SupabaseStorage:
    def __init__(self, settings):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", settings.supabase_bucket):
            raise ValueError("Invalid Supabase bucket name.")
        self.base = settings.supabase_url.rstrip("/") + "/storage/v1"
        self.bucket = quote(settings.supabase_bucket, safe="")
        self.headers = {
            "apikey": settings.supabase_service_role_key,
            "Authorization": "Bearer " + settings.supabase_service_role_key,
        }

    def ensure_private(self, client):
        response = client.get(f"{self.base}/bucket/{self.bucket}")
        if response.status_code != 200:
            raise RuntimeError("Cannot access the configured Supabase storage bucket.")
        if response.json().get("public") is not False:
            raise RuntimeError("AskLit requires a private Supabase bucket.")

    def put_bytes(self, key, data):
        with httpx.Client(headers=self.headers, timeout=120) as client:
            self.ensure_private(client)
            response = client.post(
                f"{self.base}/object/{self.bucket}/{checked_key(key)}",
                content=data,
                headers={
                    "Content-Type": "application/octet-stream",
                    "x-upsert": "false",
                },
            )
            if response.status_code not in {200, 201}:
                raise RuntimeError(
                    "Supabase upload failed. Check bucket size limits and server credentials."
                )

    def put_file(self, key, source):
        with (
            httpx.Client(headers=self.headers, timeout=300) as client,
            open(source, "rb") as handle,
        ):
            self.ensure_private(client)
            response = client.post(
                f"{self.base}/object/{self.bucket}/{checked_key(key)}",
                content=iter(lambda: handle.read(1024 * 1024), b""),
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(Path(source).stat().st_size),
                    "x-upsert": "false",
                },
            )
            if response.status_code not in {200, 201}:
                raise RuntimeError(
                    "Supabase export upload failed. Check bucket size limits and server credentials."
                )

    def download(self, key, target):
        with httpx.Client(headers=self.headers, timeout=300) as client:
            self.ensure_private(client)
            with client.stream(
                "GET",
                f"{self.base}/object/authenticated/{self.bucket}/{checked_key(key)}",
            ) as response:
                if response.status_code in {400, 404}:
                    raise FileNotFoundError("Stored object is unavailable.")
                if response.status_code != 200:
                    raise RuntimeError("Cannot read the Supabase object.")
                size = 0
                with open(target, "wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > 2 * 1024 * 1024 * 1024:
                            raise RuntimeError(
                                "Stored object exceeds the download limit."
                            )
                        handle.write(chunk)

    @contextmanager
    def materialize(self, key):
        with tempfile.TemporaryDirectory(prefix="asklit-object-") as directory:
            path = Path(directory) / checked_key(key)
            self.download(key, path)
            yield path

    def download_response(self, key):
        directory = Path(tempfile.mkdtemp(prefix="asklit-download-"))
        path = directory / checked_key(key)
        try:
            self.download(key, path)
        except Exception:
            shutil.rmtree(directory)
            raise
        return FileResponse(
            path,
            filename="asklit-streamlit.zip",
            media_type="application/zip",
            background=BackgroundTask(shutil.rmtree, directory),
        )


def object_store(settings):
    if settings.storage_backend == "supabase":
        return SupabaseStorage(settings)
    return FilesystemStorage(settings.storage)
