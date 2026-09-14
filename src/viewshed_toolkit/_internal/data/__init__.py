"""Minimal execution contracts required by the standalone pipeline service."""

from .contracts import StageResult, ValidationReport
from .parquet import (
    atomic_sink_parquet,
    scan_required,
    validate_parquet_schema,
)

__all__ = [
    "StageResult",
    "ValidationReport",
    "atomic_sink_parquet",
    "scan_required",
    "validate_parquet_schema",
]
