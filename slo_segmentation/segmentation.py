"""Rule-based segmentation of offline, pre-aggregated SLO data.

The analyzer consumes aggregated buckets (error rate, request volume, sampling
fraction, collector health, dependency and a fixed time window). Every bucket
in the window receives exactly one mutually exclusive label, so boundary
buckets are never double-counted. Maximal runs of equal labels become
segments; each segment carries the evidence behind its label.

Label priority (first match wins):

1. ``collection_missing`` -- the slot is absent or the collector was down.
2. ``sample_drift`` -- sampling fraction moved materially vs. its baseline.
3. ``traffic_drop`` -- error rate looks elevated but volume collapsed.
4. ``real_error_growth`` -- elevated error rate with healthy volume/sampling.
5. ``normal`` -- no elevated error rate.
6. ``unattributable`` -- elevated error rate but no sound baseline to judge it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Dict, List, Optional, Sequence

from .models import (
    AnalysisResult,
    Bucket,
    Segment,
    SegmentType,
    Thresholds,
)


@dataclass(frozen=True)
class _Baseline:
    error_rate: float
    request_count: float
    sample_rate: float
    usable: bool


@dataclass(frozen=True)
class _Label:
    segment_type: SegmentType
    reason: str
    details: Dict[str, Any]


def analyze(
    records: Sequence[Dict[str, Any]],
    window_start: Any,
    window_end: Any,
    bucket_interval_seconds: int,
    thresholds: Optional[Thresholds] = None,
) -> AnalysisResult:
    """Analyze raw mapping records (e.g. parsed from JSON).

    ``window_start``/``window_end`` accept :class:`datetime`, ISO-8601
    strings or epoch seconds. The window is treated as a half-open
    ``[start, end)`` range tiled by fixed-length buckets.
    """
    buckets = [Bucket.from_dict(record) for record in records]
    return analyze_buckets(
        buckets,
        window_start=window_start,
        window_end=window_end,
        bucket_interval_seconds=bucket_interval_seconds,
        thresholds=thresholds,
    )


def analyze_buckets(
    buckets: Sequence[Bucket],
    window_start: Any,
    window_end: Any,
    bucket_interval_seconds: int,
    thresholds: Optional[Thresholds] = None,
) -> AnalysisResult:
    """Segment pre-aggregated buckets inside one offline analysis window."""
    from .models import _coerce_timestamp

    start = _coerce_timestamp(window_start)
    end = _coerce_timestamp(window_end)
    if bucket_interval_seconds <= 0:
        raise ValueError("bucket_interval_seconds must be > 0")
    interval = timedelta(seconds=bucket_interval_seconds)
    if end <= start:
        raise ValueError("window_end must be after window_start")
    if (end - start).total_seconds() % bucket_interval_seconds != 0:
        raise ValueError("window length must be a multiple of bucket interval")
    if thresholds is None:
        thresholds = Thresholds()

    grid = _build_grid(start, end, interval)
    slot_index = {timestamp: index for index, timestamp in enumerate(grid)}
    dependencies = sorted({bucket.dependency for bucket in buckets})

    all_segments: List[Segment] = []
    for dependency in dependencies:
        dep_buckets = [b for b in buckets if b.dependency == dependency]
        by_slot = _index_dependency(dep_buckets, slot_index, interval)
        baseline = _compute_baseline(dep_buckets)
        labels = [
            _classify_slot(by_slot.get(index), baseline, thresholds)
            for index in range(len(grid))
        ]
        all_segments.extend(
            _build_segments(labels, grid, interval, dependency, baseline)
        )

    all_segments.sort(key=lambda s: (s.dependency, s.start))
    return AnalysisResult(
        window_start=start,
        window_end=end,
        bucket_interval_seconds=bucket_interval_seconds,
        segments=all_segments,
    )


def _build_grid(
    start: datetime, end: datetime, interval: timedelta
) -> List[datetime]:
    grid: List[datetime] = []
    cursor = start
    while cursor < end:
        grid.append(cursor)
        cursor += interval
    return grid


def _index_dependency(
    buckets: Sequence[Bucket],
    slot_index: Dict[datetime, int],
    interval: timedelta,
) -> Dict[int, Bucket]:
    by_slot: Dict[int, Bucket] = {}
    for bucket in buckets:
        if bucket.timestamp not in slot_index:
            raise ValueError(
                "bucket %s is outside the analysis window or misaligned"
                % bucket.timestamp.isoformat()
            )
        index = slot_index[bucket.timestamp]
        if index in by_slot:
            raise ValueError(
                "duplicate bucket at %s" % bucket.timestamp.isoformat()
            )
        by_slot[index] = bucket
    return by_slot


def _compute_baseline(buckets: Sequence[Bucket]) -> _Baseline:
    """Median baseline over healthy, fully sampled buckets."""
    reference = [
        bucket
        for bucket in buckets
        if bucket.collector_up and bucket.sample_rate >= 1.0
    ]
    if not reference:
        return _Baseline(0.0, 0.0, 0.0, usable=False)
    return _Baseline(
        error_rate=median(b.error_rate for b in reference),
        request_count=median(b.request_count for b in reference),
        sample_rate=median(b.sample_rate for b in reference),
        usable=True,
    )


def _classify_slot(
    bucket: Optional[Bucket],
    baseline: _Baseline,
    thresholds: Thresholds,
) -> _Label:
    if bucket is None:
        return _Label(
            SegmentType.COLLECTION_MISSING,
            "no bucket reported for this slot",
            {"collector_up": None, "missing_slot": True},
        )

    if not bucket.collector_up:
        return _Label(
            SegmentType.COLLECTION_MISSING,
            "collector was down for this slot",
            {
                "collector_up": False,
                "missing_slot": False,
                "observed_error_rate": bucket.error_rate,
                "observed_request_count": bucket.request_count,
            },
        )

    if not baseline.usable or baseline.request_count <= 0:
        return _Label(
            SegmentType.UNATTRIBUTABLE,
            "no healthy fully-sampled baseline exists for this dependency",
            {
                "observed_error_rate": bucket.error_rate,
                "observed_request_count": bucket.request_count,
                "observed_sample_rate": bucket.sample_rate,
            },
        )

    sample_floor = baseline.sample_rate * (1.0 - thresholds.sample_drift_ratio)
    traffic_floor = (
        baseline.request_count * (1.0 - thresholds.traffic_drop_ratio)
    )
    elevated = (
        bucket.error_rate
        >= baseline.error_rate * thresholds.error_growth_ratio
        and bucket.error_rate - baseline.error_rate
        >= thresholds.min_error_rate_delta
    )

    other_signals: List[str] = []
    if bucket.sample_rate <= sample_floor:
        if elevated and bucket.request_count <= traffic_floor:
            other_signals.append("traffic_drop")
        if elevated:
            other_signals.append("elevated_error_rate")
        return _Label(
            SegmentType.SAMPLE_DRIFT,
            "sample rate dropped below %.0f%% of baseline"
            % ((1.0 - thresholds.sample_drift_ratio) * 100),
            {
                "observed_sample_rate": bucket.sample_rate,
                "baseline_sample_rate": baseline.sample_rate,
                "sample_rate_floor": round(sample_floor, 6),
                "observed_error_rate": bucket.error_rate,
                "baseline_error_rate": baseline.error_rate,
                "other_signals": other_signals,
            },
        )

    if elevated:
        if bucket.request_count <= traffic_floor:
            return _Label(
                SegmentType.TRAFFIC_DROP,
                "error rate rose while request volume fell below traffic floor",
                {
                    "observed_request_count": bucket.request_count,
                    "baseline_request_count": baseline.request_count,
                    "traffic_floor": round(traffic_floor, 6),
                    "observed_error_rate": bucket.error_rate,
                    "baseline_error_rate": baseline.error_rate,
                },
            )
        return _Label(
            SegmentType.REAL_ERROR_GROWTH,
            "error rate grew beyond growth ratio with healthy volume",
            {
                "observed_error_rate": bucket.error_rate,
                "baseline_error_rate": baseline.error_rate,
                "error_growth_ratio": thresholds.error_growth_ratio,
                "observed_request_count": bucket.request_count,
                "baseline_request_count": baseline.request_count,
            },
        )

    return _Label(
        SegmentType.NORMAL,
        "error rate within normal envelope",
        {
            "observed_error_rate": bucket.error_rate,
            "baseline_error_rate": baseline.error_rate,
            "observed_request_count": bucket.request_count,
            "baseline_request_count": baseline.request_count,
            "observed_sample_rate": bucket.sample_rate,
        },
    )


def _build_segments(
    labels: List[_Label],
    grid: List[datetime],
    interval: timedelta,
    dependency: str,
    baseline: _Baseline,
) -> List[Segment]:
    segments: List[Segment] = []
    run_start = 0
    for index in range(1, len(labels) + 1):
        if index == len(labels) \
                or labels[index].segment_type != labels[run_start].segment_type:
            first = labels[run_start]
            run_labels = labels[run_start:index]
            evidence = _summarize_evidence(
                run_labels, first.segment_type, baseline
            )
            segments.append(
                Segment(
                    segment_type=first.segment_type,
                    start=grid[run_start],
                    end=grid[index - 1] + interval,
                    dependency=dependency,
                    bucket_count=index - run_start,
                    evidence=evidence,
                )
            )
            run_start = index
    return segments


def _summarize_evidence(
    labels: List[_Label],
    segment_type: SegmentType,
    baseline: _Baseline,
) -> Dict[str, Any]:
    reasons = sorted({label.reason for label in labels})
    details: List[Dict[str, Any]] = [label.details for label in labels]

    def numeric(field_name: str) -> List[float]:
        return [
            float(detail[field_name])
            for detail in details
            if isinstance(detail.get(field_name), (int, float))
        ]

    evidence: Dict[str, Any] = {
        "reasons": reasons,
        "baseline": {
            "error_rate": baseline.error_rate,
            "request_count": baseline.request_count,
            "sample_rate": baseline.sample_rate,
            "usable": baseline.usable,
        },
    }

    if segment_type == SegmentType.COLLECTION_MISSING:
        evidence["missing_slots"] = sum(
            1 for detail in details if detail.get("missing_slot")
        )
        evidence["collector_down_slots"] = sum(
            1
            for detail in details
            if detail.get("collector_up") is False
        )
        return evidence

    if segment_type == SegmentType.SAMPLE_DRIFT:
        rates = numeric("observed_sample_rate")
        evidence["sample_rate_min"] = min(rates)
        evidence["sample_rate_median"] = median(rates)
        evidence["baseline_sample_rate"] = baseline.sample_rate
        error_rates = numeric("observed_error_rate")
        if error_rates:
            evidence["error_rate_max"] = max(error_rates)
        signals = {
            signal
            for detail in details
            for signal in detail.get("other_signals", [])
        }
        if signals:
            evidence["other_signals"] = sorted(signals)
        return evidence

    if segment_type == SegmentType.TRAFFIC_DROP:
        volumes = numeric("observed_request_count")
        evidence["request_count_min"] = min(volumes)
        evidence["request_count_median"] = median(volumes)
        evidence["baseline_request_count"] = baseline.request_count
        error_rates = numeric("observed_error_rate")
        evidence["error_rate_max"] = max(error_rates)
        evidence["baseline_error_rate"] = baseline.error_rate
        return evidence

    if segment_type == SegmentType.REAL_ERROR_GROWTH:
        error_rates = numeric("observed_error_rate")
        evidence["error_rate_max"] = max(error_rates)
        evidence["error_rate_median"] = median(error_rates)
        evidence["baseline_error_rate"] = baseline.error_rate
        return evidence

    if segment_type == SegmentType.UNATTRIBUTABLE:
        error_rates = numeric("observed_error_rate")
        if error_rates:
            evidence["error_rate_max"] = max(error_rates)
        return evidence

    error_rates = numeric("observed_error_rate")
    evidence["error_rate_max"] = max(error_rates) if error_rates else 0.0
    return evidence
