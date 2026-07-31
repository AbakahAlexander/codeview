"""Backward-compatible import shims. Prefer codeview.providers.tree_sitter.* """
from codeview.providers.tree_sitter.java import TreeSitterJavaProvider

__all__ = ["TreeSitterJavaProvider"]
