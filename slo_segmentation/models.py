"""Data models for offline SLO anomaly segmentation.

All inputs represent already-aggregated offline monitoring buckets. Nothing in
this module performs live collection or network access.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List


class SegmentType(str, Enum):
    """Mutually exclusive classification of every analyzed time bucket."""

    NORMAL = "normal"
    COLLECTION_MISSING = "collection_missing"
    SAMPLE_DRIFT = "sample_drift"
    TRAFFIC_DROP = "traffic_drop"
    REAL_ERROR_GROWTH = "real_error_growth"
    UNATTRIBUTABLE = "unattributable"

    @property
    def actionable(self) -> bool:
        """Whether the segment points at a concrete, assignable cause."""
        return self in (
            SegmentType.COLLECTION_MISSING,
            SegmentType.SAMPLE_DRIFT,
            SegmentType.TRAFFIC_DROP,
            SegmentType.REAL_ERROR_GROWTH,
        )


@dataclass(frozen=True)
class Bucket:
    """One pre-aggregated monitoring bucket.

    ``error_rate`` is a fraction in the closed range [0.0, 1.0].
    ``request_count`` is the request volume observed for the bucket.
    ``sample_rate`` is the sampling fraction in (0.0, 1.0]; 1.0 means
    full sampling. ``collector_up`` reports whether the collector was healthy
    for the whole bucket.
    """

    timestamp: datetime
    error_rate: float
    request_count: float
    sample_rate: float = 1.0
    collector_up: bool = True
    dependency: str = "default"

    def __post_init__(self) -> None:
        self._validate("error_rate", self.error_rate, 0.0, 1.0)
        self._validate("sample_rate", self.sample_rate, 0.0, 1.0,
                       allow_zero=False)
        if self.request_count < 0:
            raise ValueError("request_count must be >= 0")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime instance")
        if self.timestamp.tzinfo is None:
            object.__setattr__(
                self, "timestamp",
                self.timestamp.replace(tzinfo=timezone.utc),
            )

    @staticmethod
    def _validate(name: str, value: float, lower: float, upper: float,
                  allow_zero: bool = True) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("%s must be a number" % name)
        value = float(value)
        if value != value:
            raise ValueError("%s must not be NaN" % name)
        if value < lower or value > upper:
            raise ValueError("%s out of range [%s, %s]"
                             % (name, lower, upper))
        if not allow_zero and value == 0.0:
            raise ValueError("%s must be > 0" % name)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Bucket":
        timestamp = _coerce_timestamp(data["timestamp"])
        return cls(
            timestamp=timestamp,
            error_rate=float(data["error_rate"]),
            request_count=float(data["request_count"]),
            sample_rate=float(data.get("sample_rate", 1.0)),
            collector_up=bool(data.get("collector_up", True)),
            dependency=str(data.get("dependency", "default")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "error_rate": self.error_rate,
            "request_count": self.request_count,
            "sample_rate": self.sample_rate,
            "collector_up": self.collector_up,
            "dependency": self.dependency,
        }


@dataclass(frozen=True)
class Thresholds:
    """Tuning knobs for rule-based segmentation.

    Ratios are evaluated against the per-dependency baseline computed from
    healthy, fully sampled buckets.
    """

    sample_drift_ratio: float = 0.2
    traffic_drop_ratio: float = 0.5
    error_growth_ratio: float = 2.0
    min_error_rate_delta: float = 0.01

    def __post_init__(self) -> None:
        for name in ("sample_drift_ratio", "traffic_drop_ratio",
                     "min_error_rate_delta"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ValueError("%s must be in (0, 1]" % name)
        if self.error_growth_ratio <= 1.0:
            raise ValueError("error_growth_ratio must be > 1")


@dataclass(frozen=True)
class Segment:
    """A maximal run of consecutive buckets sharing one SegmentType.

    Segments tile the full analysis window without gaps or overlap, so each
    boundary bucket belongs to exactly one segment.
    """

    segment_type: SegmentType
    start: datetime
    end: datetime
    dependency: str
    bucket_count: int
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return self.segment_type.actionable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segment_type": self.segment_type.value,
            "actionable": self.actionable,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "dependency": self.dependency,
            "bucket_count": self.bucket_count,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class AnalysisResult:
    """Full segmentation output for one offline analysis window."""

    window_start: datetime
    window_end: datetime
    bucket_interval_seconds: int
    segments: List[Segment]

    @property
    def actionable_segments(self) -> List[Segment]:
        return [segment for segment in self.segments
                if segment.actionable]

    @property
    def unattributable_segments(self) -> List[Segment]:
        return [segment for segment in self.segments
                if segment.segment_type == SegmentType.UNATTRIBUTABLE]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "bucket_interval_seconds": self.bucket_interval_seconds,
            "segments": [segment.to_dict() for segment in self.segments],
            "actionable_segments": [
                segment.to_dict() for segment in self.actionable_segments
            ],
            "unattributable_segments": [
                segment.to_dict()
                for segment in self.unattributable_segments
            ],
        }


def _coerce_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        timestamp = datetime.fromisoformat(text)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        raise TypeError("unsupported timestamp: %r" % (value,))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp
