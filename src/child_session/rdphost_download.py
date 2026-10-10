"""Lazy download + integrity check for the RdpHost.exe helper.

The self-contained helper (~146 MiB) is not committed to git (GitHub's 100 MiB
limit) and is not built on end-user machines. It is published as a GitHub Release
asset (see `.github/workflows/rdphost-release.yml`), together with a `.sha256`
sidecar. This module downloads it on first use when it is missing from
`assets/native/RdpHost/`, and re-downloads it whenever the configured release URL
changes (tracked via a `.url` marker next to the exe), so publishing a new
RdpHost.exe release tag rolls out automatically without deleting the cached file.
"""

import hashlib
import os
import urllib.request

from ok import Logger
from ok.core.downloads import download_file_with_progress

from src.child_session.rdp_host import RdpHostProcess

logger = Logger.get_logger(__name__)

_SHA256_URL_SUFFIX = ".sha256"
_MARKER_SUFFIX = ".url"
_TIMEOUT_SECONDS = 30


class RdpHostDownloadError(RuntimeError):
    pass


def _config():
    # Imported lazily so this module stays cheap to import in headless contexts;
    # main.py does `from config import config` at startup anyway.
    from config import config
    return config.get("rdphost") or {}


def _url():
    url = _config().get("url")
    if not url:
        raise RdpHostDownloadError("未配置 RdpHost 下载地址（config.py 的 rdphost.url）。")
    return url


def _fetch_sha256(url):
    sha256_url = url + _SHA256_URL_SUFFIX
    with urllib.request.urlopen(sha256_url, timeout=_TIMEOUT_SECONDS) as response:
        text = response.read().decode("ascii", "ignore").strip()
    # Accept "hex  filename" or bare hex (first whitespace-separated token).
    return text.split()[0].lower()


def _verify_sha256(path, expected):
    sha256_hash = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(4096)
            if not block:
                break
            sha256_hash.update(block)
    return sha256_hash.hexdigest() == expected


def _is_current(exe, url):
    """True when the cached exe was downloaded from `url` (marker sidecar matches)."""
    try:
        with open(exe + _MARKER_SUFFIX, "r", encoding="ascii") as f:
            return f.read().strip() == url
    except OSError:
        return False


def _write_marker(exe, url):
    try:
        with open(exe + _MARKER_SUFFIX, "w", encoding="ascii") as f:
            f.write(url)
    except OSError as exc:
        logger.warning(f"写入 RdpHost 版本标记失败：{exc}")


def ensure_rdphost(callback=None):
    """Return the path to RdpHost.exe, downloading + verifying it if missing or stale.

    `callback(percent_float, size_str)` is invoked during the download (same
    contract as `ok.core.downloads.download_file_with_progress`).

    The exe is re-downloaded when the configured `rdphost.url` differs from the
    `.url` marker written beside it, so bumping the release tag in `config.py`
    rolls the new helper out to users automatically.
    """
    exe = RdpHostProcess.exe_path()
    url = _url()
    if os.path.isfile(exe) and _is_current(exe, url):
        return exe

    missing = not os.path.isfile(exe)
    expected_sha256 = _fetch_sha256(url)
    logger.info(f"RdpHost.exe {'缺失' if missing else '版本过期'}，开始下载：{url}")
    download_file_with_progress(url, exe, callback=callback)

    if expected_sha256 and not _verify_sha256(exe, expected_sha256):
        try:
            os.remove(exe)
        except OSError:
            pass
        raise RdpHostDownloadError("RdpHost.exe 下载校验失败（SHA256 不匹配），已删除损坏文件。")

    _write_marker(exe, url)
    logger.info(f"RdpHost.exe 就绪：{exe}")
    return exe
