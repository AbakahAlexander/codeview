"""SCIP provider — ingest indexes produced by scip-java, scip-python, etc."""

from codeview.providers.scip.provider import ScipProvider, find_scip_index

__all__ = ["ScipProvider", "find_scip_index"]
