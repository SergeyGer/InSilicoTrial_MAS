"""Medallion-architecture storage backends (Delta Lake and a local equivalent)."""

from .base import BaseStore, WriteResult
from .factory import MemoryStore, create_store
from .local_store import LocalVersionedStore

__all__ = ["BaseStore", "LocalVersionedStore", "MemoryStore", "WriteResult", "create_store"]
