"""Windows C/C++ indexing goes through Docker (no native scip-clang)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codeview import scip_index as si


def test_docker_missing_raises_clear_error(monkeypatch):
    monkeypatch.setattr(si, "_which", lambda _name: None)
    with pytest.raises(RuntimeError, match="Docker Desktop"):
        si._docker()


def test_index_clang_routes_to_docker_on_windows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(si.os, "name", "nt")
    called: list[Path] = []

    def fake_docker(root: Path, out: Path, progress) -> Path:
        called.append(root)
        out.write_bytes(b"scip")
        return out

    monkeypatch.setattr(si, "_index_clang_docker", fake_docker)
    out = tmp_path / "index.scip"
    got = si._index_clang(tmp_path, out, lambda *_: None)
    assert called == [tmp_path]
    assert got == out
    assert out.read_bytes() == b"scip"


def test_index_clang_docker_happy_path(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(si, "_docker", lambda: "docker")
    monkeypatch.setattr(si, "_ensure_scip_clang_docker_image", lambda _p: "codeview-scip-clang:test")

    root = tmp_path / "proj"
    root.mkdir()
    out = tmp_path / "cache" / "index.scip"
    staged = root / ".codeview-index.scip"

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "docker"
        assert "run" in cmd
        staged.write_bytes(b"fake-scip")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(si.subprocess, "run", fake_run)
    msgs: list[str] = []
    got = si._index_clang_docker(root, out, lambda _pct, msg: msgs.append(msg))
    assert got == out
    assert out.read_bytes() == b"fake-scip"
    assert not staged.exists()
    assert any("Docker" in m for m in msgs)


def test_ensure_image_skips_build_when_present(monkeypatch):
    monkeypatch.setattr(si, "_docker", lambda: "docker")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(si.subprocess, "run", fake_run)
    image = si._ensure_scip_clang_docker_image(lambda *_: None)
    assert image == si.SCIP_CLANG_DOCKER_IMAGE
    assert len(calls) == 1
    assert calls[0][:3] == ["docker", "image", "inspect"]
