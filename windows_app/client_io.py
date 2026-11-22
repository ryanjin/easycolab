"""HTTP and file boundaries for the Qt desktop client."""

import ntpath
import os
import stat
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import requests


HF_ORIGIN = "https://huggingface.co"


def hf_catalog_hosts(token):
    return (HF_ORIGIN,) if token else (HF_ORIGIN, "https://hf-mirror.com")


class _HFCatalogSession(requests.Session):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def send(self, request, **kwargs):
        # requests calls send() again for each redirect. Check its prepared URL
        # and replace any credentials supplied by redirects, cookies or .netrc.
        url = urlsplit(request.url)
        if url.scheme != "https" or not url.hostname or url.username is not None:
            raise requests.exceptions.InvalidURL("Expected a public HTTPS URL")
        request.headers.pop("Cookie", None)
        if self.token:
            if url.hostname != "huggingface.co" or url.port not in (None, 443):
                raise requests.exceptions.InvalidURL("Authenticated catalog requests require huggingface.co")
            request.headers["Authorization"] = f"Bearer {self.token}"
        else:
            request.headers.pop("Authorization", None)
        return super().send(request, **kwargs)


def hf_catalog_get(url, token, timeout):
    with _HFCatalogSession(token) as session:
        return session.get(url, headers={"User-Agent": "EasyColab-Windows/2.0", "Accept": "application/json"},
                           timeout=timeout)


def save_media(data: bytes, filename: str, directory: Path, prefix: str = "") -> Path:
    extensions = {".mp4", ".webm", ".mov", ".webp", ".gif", ".png", ".jpg", ".jpeg"}
    reserved = getattr(ntpath, "isreserved", None)
    if (not filename or filename.startswith(".") or filename.endswith((" ", "."))
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in filename)
            or (reserved(filename) if reserved else PureWindowsPath(filename).is_reserved())
            or Path(filename).suffix.lower() not in extensions):
        raise ValueError("Invalid media filename")
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("Media directory must be a regular directory")
    destination = directory / (prefix + filename)
    staged = tempfile.NamedTemporaryFile(dir=directory, prefix=".easycolab-", delete=False)
    try:
        with staged:
            staged.write(data)
        os.replace(staged.name, destination)
    finally:
        Path(staged.name).unlink(missing_ok=True)
    return destination
