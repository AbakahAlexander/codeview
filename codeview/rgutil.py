"""Locate or fetch a ripgrep binary so users need not install it themselves."""

from __future__ import annotations

import io
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

RG_VERSION = "15.2.0"
RG_BASE = f"https://github.com/BurntSushi/ripgrep/releases/download/{RG_VERSION}"


from codeview.paths import bin_dir


def _cache_dir() -> Path:
    return bin_dir()


def _bundled_candidate() -> Path | None:
    """Optional vendored binary next to the package (future releases)."""
    here = Path(__file__).resolve().parent
    name = "rg.exe" if os.name == "nt" else "rg"
    for path in (
        here / "vendor" / "rg" / name,
        here / "vendor" / "bin" / name,
    ):
        if path.is_file():
            return path
    return None


def _platform_asset() -> tuple[str, str] | None:
    """Return (archive_filename, member_suffix) for the current OS/arch."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "aarch64"
    else:
        return None

    if system == "linux":
        # musl build is static and runs on most distros.
        return (f"ripgrep-{RG_VERSION}-{arch}-unknown-linux-musl.tar.gz", "rg")
    if system == "darwin":
        return (f"ripgrep-{RG_VERSION}-{arch}-apple-darwin.tar.gz", "rg")
    if system == "windows":
        return (f"ripgrep-{RG_VERSION}-{arch}-pc-windows-msvc.zip", "rg.exe")
    return None


def _extract_rg(archive: Path, member_name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip" or archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.filename.endswith(member_name) and not info.is_dir():
                    with zf.open(info) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    break
            else:
                raise RuntimeError(f"{member_name} not found in {archive.name}")
    else:
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                if member.name.endswith("/" + member_name) or member.name == member_name:
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    with open(dest, "wb") as out:
                        shutil.copyfileobj(extracted, out)
                    break
            else:
                raise RuntimeError(f"{member_name} not found in {archive.name}")

    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def ensure_rg(*, force_download: bool = False) -> str:
    """Return a path to an executable ``rg``, downloading one if needed."""
    cached = _cache_dir() / ("rg.exe" if os.name == "nt" else "rg")
    if not force_download and cached.is_file() and os.access(cached, os.X_OK):
        return str(cached)

    if not force_download:
        found = shutil.which("rg")
        if found:
            return found
        for candidate in (
            _bundled_candidate(),
            Path.home() / ".cargo" / "bin" / "rg",
            Path("/usr/share/cursor/resources/app/node_modules/@vscode/ripgrep/bin/rg"),
            Path("/usr/bin/rg"),
        ):
            if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

    asset = _platform_asset()
    if asset is None:
        raise RuntimeError(
            f"No bundled ripgrep for {platform.system()} / {platform.machine()}. "
            "Install ripgrep manually and ensure `rg` is on PATH."
        )
    archive_name, member = asset
    url = f"{RG_BASE}/{archive_name}"
    print(f"Downloading ripgrep {RG_VERSION}…", flush=True)
    with tempfile.TemporaryDirectory(prefix="codeview-rg-") as tmp:
        archive_path = Path(tmp) / archive_name
        with urllib.request.urlopen(url, timeout=120) as resp, open(archive_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        _extract_rg(archive_path, member, cached)
    print(f"Installed ripgrep → {cached}", flush=True)
    return str(cached)
