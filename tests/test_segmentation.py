import unittest
from datetime import datetime, timedelta, timezone

from slo_segmentation import (
    AnalysisResult,
    Bucket,
    SegmentType,
    Thresholds,
    analyze,
    analyze_buckets,
)


EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
INTERVAL = 300


def ts(offset):
    return EPOCH + timedelta(seconds=INTERVAL * offset)


def make_bucket(offset, error_rate=0.01, request_count=1000.0,
                sample_rate=1.0, collector_up=True, dependency="api"):
    return Bucket(
        timestamp=ts(offset),
        error_rate=error_rate,
        request_count=request_count,
        sample_rate=sample_rate,
        collector_up=collector_up,
        dependency=dependency,
    )


def window_end(slot_count):
    return ts(slot_count)


class TrafficDropTest(unittest.TestCase):
    def test_error_spike_with_volume_collapse_is_traffic_drop(self):
        buckets = [make_bucket(i) for i in range(8)]
        buckets += [
            make_bucket(8, error_rate=0.10, request_count=200.0),
            make_bucket(9, error_rate=0.12, request_count=180.0),
        ]
        buckets += [make_bucket(i) for i in range(10, 12)]

        result = analyze_buckets(buckets, EPOCH, window_end(12), INTERVAL)

        types = [s.segment_type for s in result.segments]
        self.assertEqual(
            types,
            [SegmentType.NORMAL, SegmentType.TRAFFIC_DROP,
             SegmentType.NORMAL],
        )
        drop = result.actionable_segments[0]
        self.assertEqual(drop.segment_type, SegmentType.TRAFFIC_DROP)
        self.assertEqual(drop.bucket_count, 2)
        self.assertEqual(drop.evidence["request_count_min"], 180.0)
        self.assertEqual(drop.evidence["baseline_request_count"], 1000.0)
        self.assertEqual(drop.evidence["error_rate_max"], 0.12)
        self.assertEqual(drop.start, ts(8))
        self.assertEqual(drop.end, ts(10))

    def test_healthy_volume_with_error_spike_is_real_growth(self):
        buckets = [make_bucket(i) for i in range(8)]
        buckets += [
            make_bucket(8, error_rate=0.08, request_count=900.0),
            make_bucket(9, error_rate=0.09, request_count=950.0),
        ]
        buckets += [make_bucket(i) for i in range(10, 12)]

        result = analyze_buckets(buckets, EPOCH, window_end(12), INTERVAL)

        types = [s.segment_type for s in result.segments]
        self.assertIn(SegmentType.REAL_ERROR_GROWTH, types)
        growth = [s for s in result.segments
                  if s.segment_type == SegmentType.REAL_ERROR_GROWTH][0]
        self.assertEqual(growth.evidence["baseline_error_rate"], 0.01)
        self.assertEqual(growth.evidence["error_rate_max"], 0.09)


class SampleDriftTest(unittest.TestCase):
    def test_sampling_change_wins_over_error_spike(self):
        buckets = [make_bucket(i) for i in range(8)]
        buckets += [
            make_bucket(8, error_rate=0.10, sample_rate=0.5),
            make_bucket(9, error_rate=0.11, sample_rate=0.4),
        ]
        buckets += [make_bucket(i) for i in range(10, 12)]

        result = analyze_buckets(buckets, EPOCH, window_end(12), INTERVAL)

        drift = [s for s in result.segments
                 if s.segment_type == SegmentType.SAMPLE_DRIFT]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0].bucket_count, 2)
        self.assertEqual(drift[0].evidence["sample_rate_min"], 0.4)
        self.assertEqual(drift[0].evidence["baseline_sample_rate"], 1.0)
        self.assertNotIn(
            SegmentType.REAL_ERROR_GROWTH,
            [s.segment_type for s in result.segments],
        )

    def test_sampling_drift_records_competing_traffic_signal_once(self):
        buckets = [make_bucket(i) for i in range(8)]
        buckets += [
            make_bucket(8, error_rate=0.10, request_count=200.0,
                        sample_rate=0.5),
            make_bucket(9, error_rate=0.10, request_count=150.0,
                        sample_rate=0.6),
        ]
        buckets += [make_bucket(i) for i in range(10, 12)]

        result = analyze_buckets(buckets, EPOCH, window_end(12), INTERVAL)

        types = [s.segment_type for s in result.segments]
        self.assertNotIn(SegmentType.TRAFFIC_DROP, types)
        drift = [s for s in result.segments
                 if s.segment_type == SegmentType.SAMPLE_DRIFT][0]
        self.assertIn("traffic_drop", drift.evidence["other_signals"])


class CollectionGapTest(unittest.TestCase):
    def test_missing_slot_and_collector_outage_merge_into_one_segment(self):
        buckets = [make_bucket(i) for i in range(3)]
        buckets.append(
            make_bucket(4, error_rate=0.5, collector_up=False)
        )
        buckets += [make_bucket(i) for i in range(5, 6)]

        result = analyze_buckets(buckets, EPOCH, window_end(6), INTERVAL)

        missing = [s for s in result.segments
                   if s.segment_type == SegmentType.COLLECTION_MISSING]
        self.assertEqual(len(missing), 1)
        segment = missing[0]
        self.assertEqual(segment.bucket_count, 2)
        self.assertEqual(segment.start, ts(3))
        self.assertEqual(segment.end, ts(5))
        self.assertEqual(segment.evidence["missing_slots"], 1)
        self.assertEqual(segment.evidence["collector_down_slots"], 1)

    def test_collector_down_is_not_real_error_growth(self):
        buckets = [make_bucket(i) for i in range(4)]
        buckets.append(
            make_bucket(4, error_rate=0.9, request_count=1000.0,
                        collector_up=False)
        )
        buckets.append(make_bucket(5))

        result = analyze_buckets(buckets, EPOCH, window_end(6), INTERVAL)

        types = [s.segment_type for s in result.segments]
        self.assertNotIn(SegmentType.REAL_ERROR_GROWTH, types)
        self.assertIn(SegmentType.COLLECTION_MISSING, types)


class BoundaryCoverageTest(unittest.TestCase):
    def test_segments_tile_window_without_gaps_or_double_attribution(self):
        buckets = [make_bucket(i) for i in range(7)]
        # slot 7 is entirely absent
        buckets.append(make_bucket(8, error_rate=0.5, collector_up=False))
        buckets += [
            make_bucket(9, error_rate=0.10, sample_rate=0.5),
            make_bucket(10, error_rate=0.10, sample_rate=0.5),
            make_bucket(11, error_rate=0.10, request_count=200.0),
            make_bucket(12, error_rate=0.10, request_count=200.0),
            make_bucket(13, error_rate=0.10),
            make_bucket(14, error_rate=0.10),
        ]
        buckets.append(make_bucket(15))

        result = analyze_buckets(buckets, EPOCH, window_end(16), INTERVAL)

        expected = [
            SegmentType.NORMAL,
            SegmentType.COLLECTION_MISSING,
            SegmentType.SAMPLE_DRIFT,
            SegmentType.TRAFFIC_DROP,
            SegmentType.REAL_ERROR_GROWTH,
            SegmentType.NORMAL,
        ]
        self.assertEqual(
            [s.segment_type for s in result.segments], expected
        )
        self.assertEqual(
            sum(s.bucket_count for s in result.segments), 16
        )
        self.assertEqual(result.segments[0].start, EPOCH)
        self.assertEqual(result.segments[-1].end, window_end(16))
        for previous, current in zip(result.segments,
                                     result.segments[1:]):
            self.assertEqual(previous.end, current.start)


class UnattributableTest(unittest.TestCase):
    def test_no_fully_sampled_baseline_is_unattributable(self):
        buckets = [
            make_bucket(i, error_rate=0.10, sample_rate=0.5)
            for i in range(4)
        ]

        result = analyze_buckets(buckets, EPOCH, window_end(4), INTERVAL)

        self.assertEqual(
            [s.segment_type for s in result.segments],
            [SegmentType.UNATTRIBUTABLE],
        )
        self.assertEqual(result.actionable_segments, [])
        self.assertEqual(len(result.unattributable_segments), 1)

    def test_empty_input_returns_window_without_segments(self):
        result = analyze_buckets([], EPOCH, window_end(3), INTERVAL)
        self.assertIsInstance(result, AnalysisResult)
        self.assertEqual(result.segments, [])
        self.assertEqual(result.actionable_segments, [])
        self.assertEqual(result.unattributable_segments, [])


class MultiDependencyTest(unittest.TestCase):
    def test_dependencies_get_independent_baselines_and_segments(self):
        buckets = [make_bucket(i, dependency="api") for i in range(4)]
        buckets += [
            make_bucket(4, error_rate=0.10, request_count=100.0,
                        dependency="api"),
            make_bucket(5, error_rate=0.10, request_count=100.0,
                        dependency="api"),
        ]
        buckets += [
            make_bucket(i, error_rate=0.02, request_count=50.0,
                        dependency="db")
            for i in range(6)
        ]

        result = analyze_buckets(buckets, EPOCH, window_end(6), INTERVAL)

        for dependency in ("api", "db"):
            dep_segments = [s for s in result.segments
                            if s.dependency == dependency]
            self.assertEqual(
                sum(s.bucket_count for s in dep_segments), 6
            )
        api_types = {
            s.segment_type
            for s in result.segments if s.dependency == "api"
        }
        self.assertIn(SegmentType.TRAFFIC_DROP, api_types)
        db_segment = [s for s in result.segments
                      if s.dependency == "db"][0]
        self.assertEqual(db_segment.segment_type, SegmentType.NORMAL)
        self.assertEqual(
            db_segment.evidence["baseline"]["request_count"], 50.0
        )


class ValidationAndApiTest(unittest.TestCase):
    def test_out_of_window_bucket_rejected(self):
        with self.assertRaises(ValueError):
            analyze_buckets(
                [make_bucket(9)], EPOCH, window_end(6), INTERVAL
            )

    def test_duplicate_bucket_rejected(self):
        with self.assertRaises(ValueError):
            analyze_buckets(
                [make_bucket(1), make_bucket(1)],
                EPOCH, window_end(3), INTERVAL,
            )

    def test_bad_window_and_thresholds_rejected(self):
        with self.assertRaises(ValueError):
            analyze_buckets([], window_end(3), EPOCH, INTERVAL)
        with self.assertRaises(ValueError):
            analyze_buckets(
                [], EPOCH, EPOCH + timedelta(seconds=100), INTERVAL
            )
        with self.assertRaises(ValueError):
            Thresholds(error_growth_ratio=1.0)
        with self.assertRaises(ValueError):
            Thresholds(traffic_drop_ratio=0.0)

    def test_dict_api_and_serialization(self):
        records = [
            {
                "timestamp": (EPOCH + timedelta(seconds=INTERVAL * i))
                .isoformat()
                .replace("+00:00", "Z"),
                "error_rate": 0.01,
                "request_count": 1000,
                "sample_rate": 1.0,
                "collector_up": True,
                "dependency": "api",
            }
            for i in range(4)
        ]
        records.append({
            "timestamp": ts(4).isoformat().replace("+00:00", "Z"),
            "error_rate": 0.2,
            "request_count": 100,
            "sample_rate": 1.0,
            "collector_up": True,
            "dependency": "api",
        })
        records.append({
            "timestamp": ts(5).isoformat().replace("+00:00", "Z"),
            "error_rate": 0.01,
            "request_count": 1000,
            "dependency": "api",
        })

        result = analyze(records, EPOCH.isoformat(),
                         window_end(6).isoformat(), INTERVAL)
        payload = result.to_dict()

        self.assertEqual(payload["bucket_interval_seconds"], INTERVAL)
        self.assertEqual(
            [s["segment_type"] for s in payload["actionable_segments"]],
            ["traffic_drop"],
        )
        self.assertEqual(payload["unattributable_segments"], [])
        for segment in payload["segments"]:
            self.assertIn("evidence", segment)
            self.assertIsInstance(segment["actionable"], bool)


if __name__ == "__main__":
    unittest.main()
