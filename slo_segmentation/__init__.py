"""Offline SLO anomaly segmentation.

This package only analyzes pre-aggregated, offline monitoring data. It never
queries live services or online monitoring systems.
"""

from .models import (
    AnalysisResult,
    Bucket,
    Segment,
    SegmentType,
    Thresholds,
)
from .segmentation import analyze, analyze_buckets

__all__ = [
    "AnalysisResult",
    "Bucket",
    "Segment",
    "SegmentType",
    "Thresholds",
    "analyze",
    "analyze_buckets",
]

__version__ = "1.0.0"
