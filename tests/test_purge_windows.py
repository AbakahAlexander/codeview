"""Windows-safe purge clears read-only files (git pack objects)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from codeview import paths as paths_mod


def test_rmtree_force_deletes_readonly_nested_files(tmp_path: Path):
    root = tmp_path / ".codeview"
    pack = root / "repos" / "demo" / ".git" / "objects" / "pack"
    pack.mkdir(parents=True)
    idx = pack / "pack-deadbeef.idx"
    idx.write_bytes(b"fake-pack")
    os.chmod(idx, stat.S_IREAD)  # read-only, like Windows git objects

    paths_mod.rmtree_force(root)

    assert not root.exists()


def test_purge_codeview_data_clears_readonly(tmp_path: Path, monkeypatch):
    home = tmp_path / ".codeview"
    pack = home / "repos" / "demo.git" / ".git" / "objects" / "pack"
    pack.mkdir(parents=True)
    idx = pack / "pack-abc.idx"
    idx.write_bytes(b"x")
    os.chmod(idx, stat.S_IREAD)

    monkeypatch.setattr(paths_mod, "codeview_home", lambda: home)
    result = paths_mod.purge_codeview_data()

    assert result["purged"] is True
    assert result["existed"] is True
    assert not home.exists()
