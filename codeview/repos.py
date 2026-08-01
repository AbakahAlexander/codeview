from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

_GIT_URL_RE = re.compile(
    r"^(?:https?://|git@|ssh://|git://)",
    re.IGNORECASE,
)


def looks_like_git_url(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.startswith("github.com/") or text.startswith("gitlab.com/"):
        return True
    return bool(_GIT_URL_RE.match(text))


def normalize_git_url(value: str) -> str:
    text = value.strip().rstrip("/")
    if text.startswith("github.com/") or text.startswith("gitlab.com/"):
        text = "https://" + text
    return text


def repo_cache_dir(url: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", url.strip())[:120]
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return Path.home() / ".codeview" / "repos" / f"{safe}-{digest}"


def resolve_target(target: str | Path) -> Path:
    """Resolve a local path or public git URL to a local directory."""
    if isinstance(target, Path):
        path = target.expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Not a directory: {path}")
        return path

    text = str(target).strip()
    if looks_like_git_url(text):
        url = normalize_git_url(text)
        dest = repo_cache_dir(url)
        if (dest / ".git").exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # Incomplete previous clone
            import shutil

            shutil.rmtree(dest)
        print(f"Cloning {url} → {dest}", flush=True)
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", url, str(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"git clone failed for {url}")
        return dest

    path = Path(text).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"Not a directory: {path}")
    return path
