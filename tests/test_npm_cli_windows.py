"""Prefer Windows ``.cmd`` npm shims over Unix scripts (WinError 193)."""

from __future__ import annotations

from pathlib import Path

from codeview import scip_index as si


def test_ensure_npm_package_prefers_cmd_on_windows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(si, "tools_dir", lambda: tmp_path)
    monkeypatch.setattr(si.os, "name", "nt")
    bindir = tmp_path / "npm" / "sourcegraph__scip-python" / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    (bindir / "scip-python").write_text("#!/bin/sh\n", encoding="utf-8")
    (bindir / "scip-python.cmd").write_text("@echo off\n", encoding="utf-8")
    got = si._ensure_npm_package("@sourcegraph/scip-python", "scip-python")
    assert got.name == "scip-python.cmd"


def test_ensure_npm_package_unix_shim(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(si, "tools_dir", lambda: tmp_path)
    monkeypatch.setattr(si.os, "name", "posix")
    bindir = tmp_path / "npm" / "sourcegraph__scip-python" / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    (bindir / "scip-python").write_text("#!/bin/sh\n", encoding="utf-8")
    got = si._ensure_npm_package("@sourcegraph/scip-python", "scip-python")
    assert got.name == "scip-python"
