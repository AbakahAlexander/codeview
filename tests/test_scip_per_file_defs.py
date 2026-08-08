"""Regression: duplicate SCIP symbols (e.g. C main) stay per-file."""

from __future__ import annotations

from codeview.providers.scip.provider import _definition_path_rank, _stable_id


def test_definition_path_rank_prefers_src_over_deps():
    assert _definition_path_rank("src/server.c") < _definition_path_rank(
        "deps/jemalloc/test/analyze/prof_bias.c"
    )
    assert _definition_path_rank("src/server.c") < _definition_path_rank(
        "utils/lru/lfu-simulation.c"
    )


def test_stable_id_differs_per_defining_file():
    scip = "cxx . . $ main()."
    a = _stable_id("scip", scip, "src/server.c")
    b = _stable_id("scip", scip, "deps/hiredis/examples/example.c")
    assert a != b
