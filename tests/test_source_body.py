"""Unit tests for callable body span extraction."""
from __future__ import annotations

from codeview.service import _brace_body_end, _callable_body_end, _indent_body_end


def test_indent_body_covers_full_python_function():
    src = '''\
def main() -> None:
    if not cli_main:
        raise RuntimeError("x")
    cli_main()

def other():
    pass
'''
    lines = src.splitlines()
    end = _callable_body_end(lines, 1, "python")
    assert end == 4
    assert "cli_main()" in "\n".join(lines[0:end])


def test_indent_body_single_import_line():
    src = '''\
from fastapi_cli.cli import main as cli_main

def main() -> None:
    cli_main()
'''
    lines = src.splitlines()
    end = _callable_body_end(lines, 1, "python")
    assert end == 1


def test_brace_body_covers_function():
    src = """\
int main() {
  foo();
  return 0;
}

int other() { return 1; }
"""
    lines = src.splitlines()
    end = _brace_body_end(lines, 0) + 1
    assert end == 4
    assert _callable_body_end(lines, 1, "c") == 4


def test_indent_trailing_blank_excluded_before_next_stmt():
    lines = ["def f():", "    return 1", "", "x = 1"]
    assert _indent_body_end(lines, 0) == 1
