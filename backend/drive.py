"""Minimal Google Drive v3 client (drive.file scope). Standard library only.

The browser signs the user in with Google and passes a short-lived access token with each
delivery request. The backend uses it for that job only and never stores or logs it.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPE = "https://www.googleapis.com/auth/drive.file"
SIGN_IN_AGAIN = "Your Google sign-in expired or was revoked. Click Save to Drive again to sign in."


class DriveError(RuntimeError):
    """A failure safe to show to users; never contains credentials."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def quote(value: str) -> str:
    """A Drive query string literal: backslash-escape backslashes and single quotes."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def multipart_body(metadata: dict, content: bytes, mime_type: str) -> tuple[bytes, str]:
    """A multipart/related body: JSON metadata first, then the media. Returns (body, content type)."""
    boundary = f"luma-{uuid4().hex}"
    head = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n"
    ).encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode(), f"multipart/related; boundary={boundary}"


class DriveClient:
    """The few Drive calls delivery needs, in the signed-in user's My Drive. Tests substitute a fake."""

    def __init__(self, access_token: str):
        self._access_token = access_token

    def _request(self, method: str, url: str, params: dict | None = None,
                 body: bytes | None = None, content_type: str | None = None) -> dict:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise DriveError(SIGN_IN_AGAIN, 401) from None
            detail = exc.read().decode(errors="replace")[:300]
            raise DriveError(f"Google Drive returned {exc.code}: {detail}", exc.code) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DriveError(f"Could not reach Google Drive: {exc}") from None

    def check_access(self) -> None:
        """One cheap call that fails fast when the token is expired or lacks Drive access."""
        self._request("GET", FILES_URL, {"pageSize": "1", "fields": "files(id)"})

    def find_root_files(self, name: str) -> list[str]:
        """IDs of non-trashed files (not folders) with this exact name at the top of My Drive."""
        query = f"name = {quote(name)} and 'root' in parents and mimeType != '{FOLDER_MIME}' and trashed = false"
        found = self._request("GET", FILES_URL, {"q": query, "fields": "files(id)"})
        return [item["id"] for item in found.get("files", [])]

    def upload_file(self, path: Path, name: str, mime_type: str) -> tuple[str, str]:
        """Create a file at the top of My Drive; returns (file_id, web_view_link). Multipart suits files up to ~5 MB."""
        body, content_type = multipart_body({"name": name}, path.read_bytes(), mime_type)
        created = self._request("POST", UPLOAD_URL, {"uploadType": "multipart", "fields": "id,webViewLink"},
                                body, content_type)
        return created["id"], created["webViewLink"]

    def overwrite_file(self, file_id: str, path: Path, mime_type: str) -> tuple[str, str]:
        """Replace a file's content in place (same ID and link); Drive keeps the old content as a revision."""
        body, content_type = multipart_body({"mimeType": mime_type}, path.read_bytes(), mime_type)
        updated = self._request("PATCH", f"{UPLOAD_URL}/{urllib.parse.quote(file_id, safe='')}",
                                {"uploadType": "multipart", "fields": "id,webViewLink"}, body, content_type)
        return updated["id"], updated["webViewLink"]


def client(access_token: str) -> DriveClient:
    return DriveClient(access_token)
