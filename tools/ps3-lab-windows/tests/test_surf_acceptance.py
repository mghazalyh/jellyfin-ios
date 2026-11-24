"""Structured Surf acceptance evidence, triage and checklist tests.

No server, credentials, media, sockets or physical console are used.  All
records are synthetic fixtures and every physical-output claim stays absent.
"""
import contextlib
import io
import json
import unittest

from ps3lab.surf_acceptance import (
    BLOCKED,
    FAIL,
    NOT_TESTED,
    PASS,
    PHYSICAL_OUTPUT,
    PHYSICAL_TRANSPORT,
    PRESENTATION_PHYSICAL,
    PRESENTATION_SYNTHETIC,
    SCHEMA,
    SYNTHETIC_OUTPUT,
    SYNTHETIC_TRANSPORT,
    TransitionEvidence,
    build_report,
    load_evidence,
    main,
    report_json,
    synthetic_fixture_transitions,
    validate_transition,
)


def record(**overrides):
    data = {
        "transition_id": "T-0001",
        "requested_operation": "select_next",
        "operation_status": "implemented",
        "source_observed": "source-a",
        "epoch": 1,
        "signed_timestamp_offset_ms": -12.5,
        "first_decoded_picture": 100,
        "actual_presentation_confirmed": True,
        "presentation_source": PRESENTATION_SYNTHETIC,
        "audio_release": True,
        "audio_release_reason": "matching_picture_presented",
        "audio_release_epoch": 1,
        "audio_gap_ms": 0.0,
        "timeout": False,
        "timeout_ms": None,
        "interruption": False,
        "interruption_reason": None,
        "underrun": False,
        "underrun_ms": None,
        "black_frame_duration_ms": 0.0,
        "frozen_frame_duration_ms": 0.0,
        "resource_recovery": True,
        "resource_recovery_ms": 0.0,
        "switch_latency_ms": 300.0,
        "command_at_ms": 1000.0,
        "first_decoded_at_ms": 1200.0,
        "presented_at_ms": 1300.0,
        "audio_released_at_ms": 1305.0,
        "transport_source": SYNTHETIC_TRANSPORT,
        "output_source": SYNTHETIC_OUTPUT,
    }
    data.update(overrides)
    return TransitionEvidence.from_mapping(data)


def physical_record(**overrides):
    data = {
        "transition_id": "P-0001",
        "transport_source": PHYSICAL_TRANSPORT,
        "output_source": PHYSICAL_OUTPUT,
        "presentation_source": PRESENTATION_PHYSICAL,
        "actual_presentation_confirmed": True,
    }
    data.update(overrides)
    return record(**data)


def codes(report_or_defects):
    defects = (report_or_defects["defects"]
               if isinstance(report_or_defects, dict)
               and "defects" in report_or_defects
               else report_or_defects)
    return {defect["code"] for defect in defects}


def status(report, item_id):
    for item in report["acceptance_checklist"]:
        if item["id"] == item_id:
            return item["status"]
    for item in report["regression_checklist"]:
        if item["id"] == item_id:
            return item["status"]
    raise AssertionError("checklist item not found: " + item_id)


class SchemaAndAvailabilityTests(unittest.TestCase):
    def test_complete_synthetic_record_validates_without_defects(self):
        item = validate_transition(record())
        self.assertEqual(item["fields"]["requested_operation"]["value"],
                         "select_next")
        self.assertTrue(item["fields"]["epoch"]["available"])
        self.assertFalse(item["physical"]["measured"])
        self.assertEqual(item["physical"]["presentation_confirmed"], None)
        self.assertEqual(item["physical"]["reason"],
                         "synthetic_output_only_no_physical_measurement")
        report = build_report([record()])
        self.assertEqual(report["defects"], [])
        self.assertEqual(report["overall_verdict"], BLOCKED)

    def test_missing_measurement_is_unavailable_null_not_zero(self):
        report = build_report([record(frozen_frame_duration_ms=None)])
        item = report["summary"]
        self.assertEqual(
            item["latency_ms"]["frozen_frame_duration_ms"]["all"]
            ["sample_count"], 0)
        self.assertEqual(
            item["latency_ms"]["frozen_frame_duration_ms"]
            ["unavailable_transitions"], 1)
        self.assertIn("missing_frozen_frame_duration_ms", codes(report))
        self.assertEqual(
            report["summary"]["required_field_coverage"]
            ["frozen_frame_duration_ms"]["missing"], 1)
        encoded = json.loads(report_json(report))
        self.assertIsNone(
            encoded["summary"]["latency_ms"]["frozen_frame_duration_ms"]
            ["all"]["max"])
        self.assertEqual(status(report, "AC-12"), BLOCKED)

    def test_censored_measurement_is_unavailable_and_excluded(self):
        report = build_report([record(
            frozen_frame_duration_ms=None,
            censored_fields={
                "frozen_frame_duration_ms": "right_censored_window_end"})])
        field = report["summary"]["required_field_coverage"][
            "frozen_frame_duration_ms"]
        self.assertEqual(field["censored"], 1)
        self.assertEqual(
            report["summary"]["latency_ms"]["frozen_frame_duration_ms"]
            ["all"]["sample_count"], 0)
        self.assertIn("censored_frozen_frame_duration_ms", codes(report))
        self.assertNotIn("missing_frozen_frame_duration_ms", codes(report))
        encoded = json.loads(report_json(report))
        self.assertIsNone(
            encoded["summary"]["latency_ms"]["frozen_frame_duration_ms"]
            ["all"]["max"])
        self.assertEqual(status(report, "AC-12"), BLOCKED)

    def test_censored_field_with_value_is_invalid(self):
        item = validate_transition(record(
            frozen_frame_duration_ms=12.0,
            censored_fields={"frozen_frame_duration_ms": "window_end"}))
        evidence = item["fields"]["frozen_frame_duration_ms"]
        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["status"], "invalid")
        self.assertIn("censored_field_has_value",
                      {issue["code"] for issue in item["issues"]})

    def test_unknown_censored_field_is_reported(self):
        item = validate_transition(record(
            censored_fields={"not_a_measurement": "window_end"}))
        self.assertIn("unknown_censored_field",
                      {issue["code"] for issue in item["issues"]})

    def test_duplicate_transition_ids_are_triaged(self):
        report = build_report([
            record(transition_id="SAME"),
            record(transition_id="SAME", epoch=2, audio_release_epoch=2),
        ])
        self.assertIn("duplicate_transition_id", codes(report))

    def test_invalid_field_types_are_not_silently_accepted(self):
        item = validate_transition(record(
            epoch="one", switch_latency_ms=-1.0,
            actual_presentation_confirmed="yes"))
        self.assertEqual(item["fields"]["epoch"]["status"], "invalid")
        self.assertEqual(item["fields"]["switch_latency_ms"]["status"],
                         "invalid")
        self.assertEqual(
            item["fields"]["actual_presentation_confirmed"]["status"],
            "invalid")
        changed = {issue["field"] for issue in item["issues"]}
        self.assertEqual(changed, {
            "epoch", "switch_latency_ms", "actual_presentation_confirmed"})


class PercentileTests(unittest.TestCase):
    def test_known_latency_percentiles(self):
        records = [
            record(transition_id="T1", epoch=1, audio_release_epoch=1,
                   switch_latency_ms=10.0),
            record(transition_id="T2", epoch=2, audio_release_epoch=2,
                   switch_latency_ms=20.0),
            record(transition_id="T3", epoch=3, audio_release_epoch=3,
                   switch_latency_ms=30.0),
            record(transition_id="T4", epoch=4, audio_release_epoch=4,
                   switch_latency_ms=40.0),
        ]
        report = build_report(records)
        stats = report["summary"]["latency_ms"]["switch_latency_ms"]["all"]
        self.assertEqual(stats["sample_count"], 4)
        self.assertEqual(stats["min"], 10.0)
        self.assertEqual(stats["p50"], 25.0)
        self.assertEqual(stats["p95"], 38.5)
        self.assertEqual(stats["p99"], 39.7)
        self.assertEqual(stats["max"], 40.0)
        self.assertEqual(stats["unit"], "ms")

    def test_failure_rate_and_percentiles(self):
        records = [
            record(transition_id="T1", epoch=1, audio_release_epoch=1),
            record(transition_id="T2", epoch=2, audio_release_epoch=2),
            record(transition_id="T3", epoch=3, audio_release_epoch=3,
                   underrun=True, underrun_ms=100.0),
            record(transition_id="T4", epoch=4, audio_release_epoch=4,
                   underrun=True, underrun_ms=50.0,
                   frozen_frame_duration_ms=1500.0),
        ]
        report = build_report(records)
        failure = report["summary"]["failure"]
        self.assertEqual(failure["defective_transitions"], 2)
        self.assertEqual(failure["failure_rate_percent"], 50.0)
        percentile = failure["per_transition_defect_count"]
        self.assertEqual(percentile["sample_count"], 4)
        self.assertEqual(percentile["min"], 0.0)
        self.assertEqual(percentile["max"], 2.0)
        self.assertAlmostEqual(percentile["p50"], 0.5, places=3)

    def test_censored_samples_do_not_change_percentiles(self):
        records = [
            record(transition_id="T1", epoch=1, audio_release_epoch=1,
                   switch_latency_ms=100.0),
            record(transition_id="T2", epoch=2, audio_release_epoch=2,
                   switch_latency_ms=None,
                   censored_fields={"switch_latency_ms": "window_end"}),
        ]
        report = build_report(records)
        section = report["summary"]["latency_ms"]["switch_latency_ms"]
        self.assertEqual(section["all"]["sample_count"], 1)
        self.assertEqual(section["all"]["p95"], 100.0)
        self.assertEqual(section["unavailable_transitions"], 1)


class TriageTests(unittest.TestCase):
    def test_premature_audio_release_is_critical_failure(self):
        report = build_report([record(
            actual_presentation_confirmed=False,
            audio_release=True,
            audio_release_reason="gate bug")])
        defects = [d for d in report["defects"]
                   if d["code"] == "premature_audio_release"]
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["severity"], "critical")
        self.assertEqual(defects[0]["category"], "sync")
        self.assertEqual(status(report, "AC-15"), FAIL)
        self.assertEqual(report["overall_verdict"], FAIL)

    def test_audio_release_timestamp_before_presentation_is_critical(self):
        report = build_report([record(
            audio_released_at_ms=1290.0,
            presented_at_ms=1300.0)])
        defect = [d for d in report["defects"]
                  if d["code"] == "premature_audio_release_by_timestamp"]
        self.assertEqual(len(defect), 1)
        self.assertEqual(defect[0]["severity"], "critical")
        self.assertEqual(status(report, "AC-15"), FAIL)

    def test_documented_timeout_release_is_degraded_not_premature(self):
        report = build_report([record(
            actual_presentation_confirmed=False,
            audio_release=True,
            audio_release_reason="gate_timeout_recovery",
            timeout=True, timeout_ms=3000.0,
            resource_recovery=True)])
        found = codes(report)
        self.assertIn("documented_timeout_audio_release", found)
        self.assertIn("gate_timeout", found)
        self.assertNotIn("premature_audio_release", found)
        self.assertEqual(status(report, "AC-15"), PASS)
        self.assertEqual(status(report, "AC-17"), FAIL)

    def test_undocumented_timeout_release_is_premature_failure(self):
        report = build_report([record(
            actual_presentation_confirmed=False,
            audio_release=True,
            audio_release_reason="released anyway",
            timeout=True, timeout_ms=3000.0,
            resource_recovery=True)])
        self.assertIn("undocumented_timeout_audio_release", codes(report))
        self.assertEqual(status(report, "AC-15"), FAIL)

    def test_stale_audio_release_is_critical(self):
        report = build_report([record(
            epoch=5, audio_release_epoch=4)])
        defects = [d for d in report["defects"]
                   if d["code"] == "stale_audio_release"]
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["severity"], "critical")
        self.assertEqual(status(report, "AC-16"), FAIL)

    def test_epoch_regression_is_critical(self):
        report = build_report([
            record(transition_id="T1", epoch=3, audio_release_epoch=3),
            record(transition_id="T2", epoch=2, audio_release_epoch=2),
        ])
        self.assertIn("epoch_regression", codes(report))
        critical = [d for d in report["defects"]
                    if d["code"] == "epoch_regression"]
        self.assertEqual(critical[0]["severity"], "critical")

    def test_timeout_interruption_underrun_and_freeze_are_triaged(self):
        report = build_report([
            record(transition_id="T1", epoch=1, audio_release_epoch=1,
                   timeout=True, timeout_ms=3000.0,
                   actual_presentation_confirmed=False,
                   audio_release=False,
                   resource_recovery=True),
            record(transition_id="T2", epoch=2, audio_release_epoch=2,
                   interruption=True, interruption_reason="disconnect",
                   resource_recovery=False),
            record(transition_id="T3", epoch=3, audio_release_epoch=3,
                   underrun=True, underrun_ms=120.0,
                   black_frame_duration_ms=200.0,
                   frozen_frame_duration_ms=1500.0),
        ])
        found = codes(report)
        self.assertIn("gate_timeout", found)
        self.assertIn("transport_interruption", found)
        self.assertIn("audio_underrun", found)
        self.assertIn("black_frame_observed", found)
        self.assertIn("frozen_frame_exceeds_diagnostic_budget", found)
        self.assertIn("resource_not_recovered", found)
        self.assertEqual(status(report, "AC-17"), FAIL)
        self.assertEqual(status(report, "AC-18"), FAIL)
        self.assertEqual(status(report, "AC-19"), FAIL)

    def test_missing_underrun_duration_is_evidence_gap(self):
        report = build_report([record(
            underrun=True, underrun_ms=None)])
        self.assertIn("missing_underrun_duration", codes(report))

    def test_unimplemented_control_is_reported_not_counted_as_tested(self):
        report = build_report([record(
            requested_operation="favorite_current",
            operation_status="not_implemented")])
        defect = [d for d in report["defects"]
                  if d["code"] == "control_not_implemented"]
        self.assertEqual(len(defect), 1)
        self.assertEqual(defect[0]["severity"], "info")
        self.assertEqual(report["summary"]["failure"]["defective_transitions"], 0)


class EvidenceTierTests(unittest.TestCase):
    def test_physical_record_is_measured_and_separate(self):
        report = build_report([physical_record(
            signed_timestamp_offset_ms=10.0)])
        item = report["summary"]
        self.assertEqual(item["physical_output_measured"], 1)
        self.assertEqual(item["physical_output_transitions"], 1)
        self.assertEqual(item["synthetic_output_transitions"], 0)
        self.assertTrue(report["physical_output_measured"])
        self.assertEqual(report["physical_verdict"], PASS)
        self.assertEqual(status(report, "AC-22"), PASS)
        self.assertEqual(status(report, "AC-23"), PASS)
        self.assertEqual(status(report, "AC-24"), NOT_TESTED)
        self.assertEqual(report["overall_verdict"], BLOCKED)

    def test_synthetic_physical_claim_is_rejected(self):
        report = build_report([record(
            output_source=PHYSICAL_OUTPUT,
            presentation_source=PRESENTATION_PHYSICAL,
            actual_presentation_confirmed=True)])
        found = codes(report)
        self.assertIn("inconsistent_evidence_tier", found)
        self.assertIn("unsubstantiated_physical_output", found)
        self.assertEqual(status(report, "AC-20"), FAIL)
        self.assertEqual(report["overall_verdict"], FAIL)

    def test_physical_presentation_with_synthetic_output_is_rejected(self):
        report = build_report([record(
            presentation_source=PRESENTATION_PHYSICAL)])
        self.assertIn("physical_presentation_without_physical_output",
                      codes(report))
        self.assertEqual(status(report, "AC-20"), FAIL)

    def test_physical_offset_target_is_checked(self):
        report = build_report([physical_record(
            signed_timestamp_offset_ms=120.0)])
        self.assertIn("av_offset_above_critical_target", codes(report))
        self.assertEqual(status(report, "AC-23"), FAIL)
        self.assertEqual(report["physical_verdict"], FAIL)
        self.assertEqual(report["overall_verdict"], FAIL)

    def test_synthetic_offset_target_is_informational_only(self):
        report = build_report([record(
            signed_timestamp_offset_ms=120.0)])
        defects = [d for d in report["defects"]
                   if d["code"] ==
                   "synthetic_offset_exceeds_provisional_target"]
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["severity"], "info")
        self.assertEqual(report["summary"]["failure"]["by_severity"]
                         .get("critical", 0), 0)


class FixtureAndChecklistTests(unittest.TestCase):
    def test_hundred_transition_fixture_is_complete_and_deterministic(self):
        first = synthetic_fixture_transitions(100)
        second = synthetic_fixture_transitions(100)
        self.assertEqual(report_json(build_report(first, 100)),
                         report_json(build_report(second, 100)))
        report = build_report(first, fixture_transition_count=100)
        self.assertEqual(report["schema"], SCHEMA)
        self.assertEqual(report["summary"]["total_transitions"], 100)
        self.assertEqual(report["summary"]["complete_transitions"], 100)
        self.assertEqual(report["summary"]["physical_output_measured"], 0)
        self.assertEqual(report["defects"], [])
        self.assertEqual(status(report, "AC-21"), PASS)
        self.assertEqual(status(report, "AC-22"), BLOCKED)
        self.assertEqual(status(report, "AC-24"), NOT_TESTED)
        self.assertEqual(status(report, "AC-25"), BLOCKED)
        self.assertEqual(report["overall_verdict"], BLOCKED)
        self.assertFalse(report["server_contacted"])
        self.assertTrue(report["synthetic_fixture"])
        self.assertEqual(
            report["summary"]["latency_ms"]["switch_latency_ms"]["all"]
            ["sample_count"], 100)

    def test_checklists_cover_regression_and_acceptance(self):
        report = build_report(synthetic_fixture_transitions(100), 100)
        regression = {item["id"]: item for item in report[
            "regression_checklist"]}
        acceptance = {item["id"]: item for item in report[
            "acceptance_checklist"]}
        for item_id in ("RC-01", "RC-02", "RC-03", "RC-04", "RC-05",
                        "RC-06", "RC-07", "RC-08", "RC-09"):
            self.assertEqual(regression[item_id]["status"], PASS, item_id)
        self.assertEqual(regression["RC-10"]["status"], NOT_TESTED)
        self.assertEqual(len(acceptance), 25)
        for item in report["acceptance_checklist"]:
            self.assertIn(item["status"],
                          {PASS, FAIL, BLOCKED, NOT_TESTED})

    def test_report_json_is_strict_and_serializable(self):
        report = build_report([record(frozen_frame_duration_ms=None)])
        encoded = report_json(report)
        json.loads(encoded, parse_constant=lambda token: self.fail(
            "non-finite JSON token: " + token))
        self.assertNotIn(": NaN", encoded)
        self.assertNotIn(": Infinity", encoded)
        self.assertNotIn(": -Infinity", encoded)

    def test_client_can_load_single_or_wrapped_evidence(self):
        single = {
            "transition_id": "S1",
            "requested_operation": "select_next",
            "source_observed": "source-a",
            "epoch": 1,
            "signed_timestamp_offset_ms": 0.0,
            "first_decoded_picture": 1,
            "actual_presentation_confirmed": True,
            "presentation_source": PRESENTATION_SYNTHETIC,
            "audio_release": False,
            "timeout": False,
            "interruption": False,
            "underrun": False,
            "black_frame_duration_ms": 0.0,
            "frozen_frame_duration_ms": 0.0,
            "resource_recovery": True,
            "switch_latency_ms": 10.0,
        }
        self.assertEqual(len(load_evidence(single)), 1)
        wrapped = {"transitions": [{
            "transition_id": "W1",
            "requested_operation": "select_next",
            "source_observed": "source-a",
            "epoch": 1,
            "signed_timestamp_offset_ms": 0.0,
            "first_decoded_picture": 1,
            "actual_presentation_confirmed": True,
            "presentation_source": PRESENTATION_SYNTHETIC,
            "audio_release": False,
            "timeout": False,
            "interruption": False,
            "underrun": False,
            "black_frame_duration_ms": 0.0,
            "frozen_frame_duration_ms": 0.0,
            "resource_recovery": True,
            "switch_latency_ms": 10.0,
        }]}
        self.assertEqual(len(load_evidence(wrapped)), 1)

    def test_cli_emits_deterministic_report_without_network(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--transitions", "100"])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["schema"], SCHEMA)
        self.assertEqual(report["summary"]["total_transitions"], 100)
        self.assertFalse(report["server_contacted"])
        self.assertEqual(report["overall_verdict"], BLOCKED)


if __name__ == "__main__":
    unittest.main()
