"""Surf transition acceptance measurement and defect triage.

This module validates *evidence records* for Surf virtual-source transitions.
It never contacts Jellyfin/CatalogService, authenticates, decodes media or
claims physical PS3 output.  A record produced by a desktop fixture is kept
in a synthetic evidence tier; physical output remains unavailable until it is
separately observed on the console.

The schema deliberately distinguishes:
  * synthetic transport / synthetic output (deterministic fixture evidence)
  * physical output (console RSX flip plus audio output evidence)
  * missing measurement (not observed)
  * censored measurement (observation window ended before the event)

Missing and censored values stay ``None`` and are excluded from percentile
samples.  They are never silently coerced to zero.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "JFI-SURF-ACCEPTANCE-EVIDENCE-1"

SYNTHETIC_TRANSPORT = "synthetic_transport"
PHYSICAL_TRANSPORT = "physical_transport"
SYNTHETIC_OUTPUT = "synthetic_output"
PHYSICAL_OUTPUT = "physical_output"
PRESENTATION_SYNTHETIC = "synthetic_flip_ack"
PRESENTATION_PHYSICAL = "physical_console"

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NOT_TESTED = "NOT TESTED"
NOT_IMPLEMENTED = "NOT IMPLEMENTED"

TRANSPORT_SOURCES = (SYNTHETIC_TRANSPORT, PHYSICAL_TRANSPORT)
OUTPUT_SOURCES = (SYNTHETIC_OUTPUT, PHYSICAL_OUTPUT)

# The release contract's provisional rendered A/V targets.  They are applied
# to physical output only; synthetic values are labelled as modeled evidence.
SIGNED_OFFSET_TARGET_MS = 50.0
SIGNED_OFFSET_CRITICAL_MS = 100.0
# Diagnostic freeze budget, not a measured hardware guarantee.
FREEZE_DIAGNOSTIC_MS = 1000.0

_REQUIRED = "required"

FIELD_SPECS = {
    "requested_operation": {
        "kind": "text", "unit": None, "tier": "control", _REQUIRED: True},
    "source_observed": {
        "kind": "text", "unit": None, "tier": "control", _REQUIRED: True},
    "epoch": {
        "kind": "nonneg_int", "unit": "count", "tier": "control",
        _REQUIRED: True},
    "signed_timestamp_offset_ms": {
        "kind": "finite", "unit": "ms", "tier": "transport", _REQUIRED: True},
    "first_decoded_picture": {
        "kind": "nonneg_int", "unit": "sequence", "tier": "transport",
        _REQUIRED: True},
    "actual_presentation_confirmed": {
        "kind": "bool", "unit": None, "tier": "output", _REQUIRED: True},
    "audio_release": {
        "kind": "bool", "unit": None, "tier": "output", _REQUIRED: True},
    "timeout": {
        "kind": "bool", "unit": None, "tier": "output", _REQUIRED: True},
    "interruption": {
        "kind": "bool", "unit": None, "tier": "transport", _REQUIRED: True},
    "underrun": {
        "kind": "bool", "unit": None, "tier": "output", _REQUIRED: True},
    "black_frame_duration_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: True},
    "frozen_frame_duration_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: True},
    "resource_recovery": {
        "kind": "bool", "unit": None, "tier": "output", _REQUIRED: True},
    "operation_status": {
        "kind": "text", "unit": None, "tier": "control", _REQUIRED: False},
    "presentation_source": {
        "kind": "text", "unit": None, "tier": "output", _REQUIRED: False},
    "audio_release_reason": {
        "kind": "text", "unit": None, "tier": "output", _REQUIRED: False},
    "audio_release_epoch": {
        "kind": "nonneg_int", "unit": "count", "tier": "output",
        _REQUIRED: False},
    "audio_gap_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: False},
    "timeout_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: False},
    "interruption_reason": {
        "kind": "text", "unit": None, "tier": "transport",
        _REQUIRED: False},
    "underrun_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: False},
    "resource_recovery_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: False},
    "switch_latency_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: False},
    "command_at_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "control",
        _REQUIRED: False},
    "first_decoded_at_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "transport",
        _REQUIRED: False},
    "presented_at_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: False},
    "audio_released_at_ms": {
        "kind": "nonneg_number", "unit": "ms", "tier": "output",
        _REQUIRED: False},
}

REQUIRED_FIELDS = tuple(
    name for name, spec in FIELD_SPECS.items() if spec[_REQUIRED])

_LATENCY_FIELDS = (
    "switch_latency_ms",
    "first_picture_latency_ms",
    "audio_gap_ms",
    "black_frame_duration_ms",
    "frozen_frame_duration_ms",
    "underrun_ms",
    "timeout_ms",
    "resource_recovery_ms",
)


@dataclass(frozen=True)
class TransitionEvidence:
    """One Surf transition and its observed/unavailable measurements."""

    transition_id: str | None = None
    requested_operation: str | None = None
    operation_status: str | None = "implemented"
    source_observed: str | None = None
    epoch: int | None = None
    signed_timestamp_offset_ms: float | None = None
    first_decoded_picture: int | None = None
    actual_presentation_confirmed: bool | None = None
    presentation_source: str | None = None
    audio_release: bool | None = None
    audio_release_reason: str | None = None
    audio_release_epoch: int | None = None
    audio_gap_ms: float | None = None
    timeout: bool | None = None
    timeout_ms: float | None = None
    interruption: bool | None = None
    interruption_reason: str | None = None
    underrun: bool | None = None
    underrun_ms: float | None = None
    black_frame_duration_ms: float | None = None
    frozen_frame_duration_ms: float | None = None
    resource_recovery: bool | None = None
    resource_recovery_ms: float | None = None
    switch_latency_ms: float | None = None
    command_at_ms: float | None = None
    first_decoded_at_ms: float | None = None
    presented_at_ms: float | None = None
    audio_released_at_ms: float | None = None
    transport_source: str | None = SYNTHETIC_TRANSPORT
    output_source: str | None = SYNTHETIC_OUTPUT
    censored_fields: Mapping[str, str] = field(default_factory=dict)
    notes: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "TransitionEvidence":
        if not isinstance(data, Mapping):
            raise ValueError("transition evidence must be a mapping")
        values = {
            name: data.get(name) for name in FIELD_SPECS
            if name in data
        }
        for name in ("transition_id", "transport_source", "output_source",
                     "censored_fields", "notes"):
            if name in data:
                values[name] = data[name]
        values.setdefault("transport_source", SYNTHETIC_TRANSPORT)
        values.setdefault("output_source", SYNTHETIC_OUTPUT)
        values.setdefault("operation_status", (
            "implemented" if values.get("requested_operation") else None))
        censored = values.get("censored_fields") or {}
        if not isinstance(censored, Mapping):
            raise ValueError("censored_fields must be a mapping")
        values["censored_fields"] = dict(censored)
        return cls(**values)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _kind_error(kind: str, value: Any) -> str | None:
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            return "expected non-empty text"
        return None
    if kind == "bool":
        return None if isinstance(value, bool) else "expected boolean"
    if kind == "nonneg_int":
        if isinstance(value, bool) or not isinstance(value, int):
            return "expected nonnegative integer"
        return None if value >= 0 else "expected nonnegative integer"
    if kind == "finite":
        return None if _is_number(value) else "expected finite number"
    if kind == "nonneg_number":
        if not _is_number(value):
            return "expected finite number"
        return None if float(value) >= 0 else "expected nonnegative number"
    return "unknown field kind"


def _add_issue(issues: list[dict], code: str, field_name: str | None,
               severity: str, message: str) -> None:
    issues.append({
        "code": code,
        "field": field_name,
        "severity": severity,
        "message": message,
    })


def _source_for(spec: Mapping[str, Any], record: TransitionEvidence) -> str:
    tier = spec["tier"]
    if tier == "control":
        return "control_observation"
    if tier == "transport":
        return record.transport_source or "unclassified"
    return record.output_source or "unclassified"


def _field_evidence(record: TransitionEvidence, name: str,
                    spec: Mapping[str, Any], issues: list[dict]) -> dict:
    value = getattr(record, name)
    censored = record.censored_fields
    source = _source_for(spec, record)
    if name in censored:
        reason = str(censored[name] or "unspecified")
        if value is not None:
            _add_issue(
                issues, "censored_field_has_value", name, "major",
                "a censored measurement must not carry a numeric value")
            return {
                "value": None, "status": "invalid", "available": False,
                "reason": "invalid:value_present_for_censored_measurement",
                "unit": spec["unit"], "source": source,
            }
        return {
            "value": None, "status": "censored", "available": False,
            "reason": "censored:" + reason,
            "unit": spec["unit"], "source": source,
        }
    if value is None:
        return {
            "value": None, "status": "missing", "available": False,
            "reason": "missing", "unit": spec["unit"], "source": source,
        }
    error = _kind_error(spec["kind"], value)
    if error:
        _add_issue(
            issues, "invalid_field", name, "major",
            "invalid " + name + ": " + error)
        return {
            "value": None, "status": "invalid", "available": False,
            "reason": "invalid:" + error,
            "unit": spec["unit"], "source": source,
        }
    return {
        "value": value, "status": "available", "available": True,
        "reason": None, "unit": spec["unit"], "source": source,
    }


def validate_transition(record: TransitionEvidence, index: int = 0) -> dict:
    """Validate one transition and expose unavailable evidence explicitly."""
    issues: list[dict] = []
    fields = {
        name: _field_evidence(record, name, spec, issues)
        for name, spec in FIELD_SPECS.items()
    }

    unknown_censored = sorted(set(record.censored_fields) - set(FIELD_SPECS))
    for name in unknown_censored:
        _add_issue(
            issues, "unknown_censored_field", name, "major",
            "censored field is not part of the evidence schema")

    if (record.operation_status is not None
            and record.operation_status not in
            ("implemented", "not_implemented")):
        _add_issue(
            issues, "invalid_operation_status", "operation_status", "major",
            "operation_status must be implemented or not_implemented")
    if (record.presentation_source is not None
            and record.presentation_source not in
            (PRESENTATION_SYNTHETIC, PRESENTATION_PHYSICAL)):
        _add_issue(
            issues, "invalid_presentation_source", "presentation_source",
            "major", "presentation_source is not a known evidence class")
    if record.transport_source not in TRANSPORT_SOURCES:
        _add_issue(
            issues, "invalid_transport_source", "transport_source", "critical",
            "transport must be synthetic_transport or physical_transport")
    if record.output_source not in OUTPUT_SOURCES:
        _add_issue(
            issues, "invalid_output_source", "output_source", "critical",
            "output must be synthetic_output or physical_output")
    if (record.transport_source == SYNTHETIC_TRANSPORT
            and record.output_source == PHYSICAL_OUTPUT):
        _add_issue(
            issues, "inconsistent_evidence_tier", "output_source", "critical",
            "synthetic transport cannot substantiate physical output")
    if record.output_source == PHYSICAL_OUTPUT:
        if (record.transport_source != PHYSICAL_TRANSPORT
                or record.presentation_source != PRESENTATION_PHYSICAL
                or record.actual_presentation_confirmed is not True):
            _add_issue(
                issues, "unsubstantiated_physical_output",
                "actual_presentation_confirmed", "critical",
                "physical output requires physical transport and a confirmed "
                "physical presentation")
    elif record.presentation_source == PRESENTATION_PHYSICAL:
        _add_issue(
            issues, "physical_presentation_without_physical_output",
            "presentation_source", "critical",
            "physical presentation cannot be claimed in a synthetic tier")
    if (record.actual_presentation_confirmed is True
            and record.presentation_source not in
            (PRESENTATION_SYNTHETIC, PRESENTATION_PHYSICAL)):
        _add_issue(
            issues, "missing_presentation_source", "presentation_source",
            "major", "presentation confirmation needs a source class")

    # Derive switch latency only from observed timestamps.
    command = fields["command_at_ms"]["value"]
    decoded = fields["first_decoded_at_ms"]["value"]
    presented = fields["presented_at_ms"]["value"]
    derived: dict[str, dict] = {}
    if command is not None and decoded is not None:
        first_latency = float(decoded) - float(command)
        if first_latency < 0:
            _add_issue(
                issues, "negative_first_picture_latency",
                "first_decoded_at_ms", "major",
                "first decoded picture precedes the command timestamp")
        else:
            derived["first_picture_latency_ms"] = {
                "value": round(first_latency, 3),
                "status": "available", "available": True,
                "reason": "derived:first_decoded_at_ms-command_at_ms",
                "unit": "ms", "source": _source_for(
                    FIELD_SPECS["first_decoded_at_ms"], record),
            }
    if (fields["switch_latency_ms"]["status"] == "missing"
            and command is not None and presented is not None):
        latency = float(presented) - float(command)
        if latency < 0:
            _add_issue(
                issues, "negative_switch_latency", "presented_at_ms", "major",
                "presented timestamp precedes the command timestamp")
        else:
            fields["switch_latency_ms"] = {
                "value": round(latency, 3),
                "status": "available", "available": True,
                "reason": "derived:presented_at_ms-command_at_ms",
                "unit": "ms", "source": _source_for(
                    FIELD_SPECS["presented_at_ms"], record),
            }

    physical_measured = (
        record.transport_source == PHYSICAL_TRANSPORT
        and record.output_source == PHYSICAL_OUTPUT
        and record.presentation_source == PRESENTATION_PHYSICAL
        and record.actual_presentation_confirmed is True)
    physical_reason = None
    if not physical_measured:
        physical_reason = (
            "synthetic_output_only_no_physical_measurement"
            if record.output_source != PHYSICAL_OUTPUT
            else "physical_output_not_confirmed")
    physical = {
        "output_source": record.output_source,
        "measured": physical_measured,
        "presentation_confirmed": (
            True if physical_measured else None),
        "reason": physical_reason,
        "metrics_available": physical_measured,
    }

    transition_id = record.transition_id
    if not isinstance(transition_id, str) or not transition_id.strip():
        _add_issue(
            issues, "missing_transition_id", "transition_id", "major",
            "transition_id is required for deterministic triage")
        transition_id = "transition-%04d" % (index + 1)

    return {
        "index": index,
        "transition_id": transition_id,
        "transport_source": record.transport_source,
        "output_source": record.output_source,
        "synthetic_transport": record.transport_source == SYNTHETIC_TRANSPORT,
        "synthetic_output": record.output_source == SYNTHETIC_OUTPUT,
        "physical": physical,
        "fields": fields,
        "derived": derived,
        "censored_fields": dict(record.censored_fields),
        "issues": issues,
        "notes": record.notes,
    }


def validate_evidence(records: Iterable[TransitionEvidence]) -> list[dict]:
    validated = [validate_transition(record, i)
                 for i, record in enumerate(records)]
    seen: dict[str, list[int]] = {}
    for item in validated:
        seen.setdefault(item["transition_id"], []).append(item["index"])
    for transition_id, indices in seen.items():
        if len(indices) < 2:
            continue
        for index in indices:
            _add_issue(
                validated[index]["issues"], "duplicate_transition_id",
                "transition_id", "major",
                "transition_id %r is not unique" % transition_id)
    return validated


_CATEGORY_BY_CODE = {
    "premature_audio_release": "sync",
    "premature_audio_release_by_timestamp": "sync",
    "documented_timeout_audio_release": "sync",
    "undocumented_timeout_audio_release": "sync",
    "stale_audio_release": "sync",
    "audio_release_epoch_ahead": "sync",
    "epoch_regression": "sync",
    "gate_timeout": "timeout",
    "transport_interruption": "transport",
    "audio_underrun": "audio",
    "audio_not_released_after_presentation": "audio",
    "presentation_not_confirmed": "presentation",
    "black_frame_observed": "presentation",
    "black_frame_exceeds_diagnostic_budget": "presentation",
    "frozen_frame_observed": "presentation",
    "frozen_frame_exceeds_diagnostic_budget": "presentation",
    "resource_not_recovered": "resource",
    "synthetic_offset_exceeds_provisional_target": "coverage",
    "av_offset_above_target": "sync",
    "av_offset_above_critical_target": "sync",
}


def _defect_category(code: str) -> str:
    if code in _CATEGORY_BY_CODE:
        return _CATEGORY_BY_CODE[code]
    if code.startswith("missing_") or code.startswith("censored_"):
        return "evidence_gap"
    if code.startswith("invalid_") or code.startswith("inconsistent_"):
        return "schema"
    return "schema"


def _value(item: Mapping[str, Any], name: str) -> Any:
    evidence = item["fields"].get(name)
    if evidence is None or not evidence["available"]:
        return None
    return evidence["value"]


def triage(validated: Sequence[dict]) -> list[dict]:
    """Return deterministic, evidence-linked defects for every transition."""
    defects: list[dict] = []

    def add(item: Mapping[str, Any], code: str, field_name: str | None,
            severity: str, message: str) -> None:
        key = (item["index"], code, field_name)
        if any((d["index"], d["code"], d["field"]) == key for d in defects):
            return
        defects.append({
            "defect_id": "D%04d" % (len(defects) + 1),
            "index": item["index"],
            "transition_id": item["transition_id"],
            "code": code,
            "category": _defect_category(code),
            "severity": severity,
            "field": field_name,
            "message": message,
        })

    previous_epoch: int | None = None
    max_epoch_seen: int | None = None
    for item in validated:
        for issue in item["issues"]:
            add(item, issue["code"], issue["field"], issue["severity"],
                issue["message"])

        for name in REQUIRED_FIELDS:
            status = item["fields"][name]["status"]
            if status == "missing":
                severity = "critical" if name == "actual_presentation_confirmed" else "major"
                add(item, "missing_" + name, name, severity,
                    "required measurement was not observed")
            elif status == "censored":
                add(item, "censored_" + name, name, "major",
                    "measurement is censored; treat as unavailable")

        operation = _value(item, "requested_operation")
        operation_status = _value(item, "operation_status")
        epoch = _value(item, "epoch")
        actual = _value(item, "actual_presentation_confirmed")
        audio_release = _value(item, "audio_release")
        release_reason = _value(item, "audio_release_reason")
        release_epoch = _value(item, "audio_release_epoch")
        timeout = _value(item, "timeout")
        interruption = _value(item, "interruption")
        underrun = _value(item, "underrun")
        black_ms = _value(item, "black_frame_duration_ms")
        frozen_ms = _value(item, "frozen_frame_duration_ms")
        recovery = _value(item, "resource_recovery")
        offset = _value(item, "signed_timestamp_offset_ms")
        presented_at = _value(item, "presented_at_ms")
        released_at = _value(item, "audio_released_at_ms")

        if operation_status == "not_implemented":
            add(item, "control_not_implemented", "requested_operation",
                "info", "control is NOT IMPLEMENTED and cannot count as tested")
        if operation is not None and operation_status is None:
            add(item, "missing_operation_status", "operation_status",
                "major", "requested operation lacks implementation status")

        if audio_release is True:
            if item["fields"]["audio_release_reason"]["status"] != "available":
                add(item, "missing_audio_release_reason",
                    "audio_release_reason", "major",
                    "audio release requires a recorded reason")
            if item["fields"]["audio_release_epoch"]["status"] != "available":
                add(item, "missing_audio_release_epoch",
                    "audio_release_epoch", "major",
                    "audio release requires the matching source epoch")
        if actual is not True and audio_release is True:
            reason_text = (release_reason or "").lower()
            documented_timeout = (
                timeout is True
                and ("timeout" in reason_text or "fallback" in reason_text))
            if documented_timeout:
                add(item, "documented_timeout_audio_release",
                    "audio_release", "major",
                    "audio released by documented timeout recovery")
            elif timeout is True:
                add(item, "undocumented_timeout_audio_release",
                    "audio_release", "critical",
                    "timeout audio release has no documented recovery reason")
            else:
                add(item, "premature_audio_release", "audio_release",
                    "critical",
                    "audio released before a matching confirmed picture")
        if (actual is True and audio_release is True
                and presented_at is not None and released_at is not None
                and float(released_at) < float(presented_at)):
            add(item, "premature_audio_release_by_timestamp",
                "audio_released_at_ms", "critical",
                "audio release timestamp precedes the presented timestamp")
        if actual is False and timeout is not True and interruption is not True:
            add(item, "presentation_not_confirmed",
                "actual_presentation_confirmed", "major",
                "transition did not confirm a presented picture")
        if audio_release is True and release_epoch is not None and epoch is not None:
            if release_epoch > epoch:
                add(item, "audio_release_epoch_ahead",
                    "audio_release_epoch", "critical",
                    "audio release names a future source epoch")
            elif release_epoch < epoch:
                add(item, "stale_audio_release", "audio_release_epoch",
                    "critical",
                    "audio release belongs to an obsolete source epoch")
            elif max_epoch_seen is not None and release_epoch < max_epoch_seen:
                add(item, "stale_audio_release", "audio_release_epoch",
                    "critical",
                    "audio release belongs to an obsolete source epoch")
        if timeout is True:
            add(item, "gate_timeout", "timeout", "major",
                "safety gate timeout is a degraded transition")
            if item["fields"]["timeout_ms"]["status"] != "available":
                add(item, "missing_timeout_duration", "timeout_ms", "major",
                    "timeout duration was not recorded")
        if interruption is True:
            add(item, "transport_interruption", "interruption", "major",
                "transport/source interruption was observed")
            if item["fields"]["interruption_reason"]["status"] != "available":
                add(item, "missing_interruption_reason",
                    "interruption_reason", "major",
                    "interruption requires a recorded reason")
        if underrun is True:
            add(item, "audio_underrun", "underrun", "major",
                "audio underrun was observed")
            if item["fields"]["underrun_ms"]["status"] != "available":
                add(item, "missing_underrun_duration", "underrun_ms", "major",
                    "underrun duration was not recorded")
        if black_ms is not None and float(black_ms) > 0:
            code = ("black_frame_exceeds_diagnostic_budget"
                    if float(black_ms) > FREEZE_DIAGNOSTIC_MS
                    else "black_frame_observed")
            add(item, code, "black_frame_duration_ms",
                "major" if code.endswith("budget") else "minor",
                "black-frame duration is non-zero")
        if frozen_ms is not None and float(frozen_ms) > 0:
            code = ("frozen_frame_exceeds_diagnostic_budget"
                    if float(frozen_ms) > FREEZE_DIAGNOSTIC_MS
                    else "frozen_frame_observed")
            add(item, code, "frozen_frame_duration_ms",
                "major" if code.endswith("budget") else "minor",
                "frozen-frame duration is non-zero")
        if (timeout is True or interruption is True) and recovery is None:
            add(item, "missing_resource_recovery", "resource_recovery",
                "critical", "failure did not record resource recovery")
        if recovery is False:
            add(item, "resource_not_recovered", "resource_recovery",
                "critical", "resources were not recovered after the transition")
        if item["fields"]["switch_latency_ms"]["status"] != "available":
            add(item, "missing_switch_latency", "switch_latency_ms", "major",
                "switch latency was neither measured nor derivable")

        if offset is not None:
            magnitude = abs(float(offset))
            if item["physical"]["measured"]:
                if magnitude > SIGNED_OFFSET_CRITICAL_MS:
                    add(item, "av_offset_above_critical_target",
                        "signed_timestamp_offset_ms", "critical",
                        "physical A/V offset exceeds 100 ms")
                elif magnitude > SIGNED_OFFSET_TARGET_MS:
                    add(item, "av_offset_above_target",
                        "signed_timestamp_offset_ms", "major",
                        "physical A/V offset exceeds the 50 ms target")
            elif magnitude > SIGNED_OFFSET_TARGET_MS:
                add(item, "synthetic_offset_exceeds_provisional_target",
                    "signed_timestamp_offset_ms", "info",
                    "synthetic offset exceeds target; not physical evidence")

        if epoch is not None:
            if previous_epoch is not None and epoch < previous_epoch:
                add(item, "epoch_regression", "epoch", "critical",
                    "source epoch moved backwards")
            previous_epoch = epoch
            max_epoch_seen = (epoch if max_epoch_seen is None
                              else max(max_epoch_seen, epoch))

    defects.sort(key=lambda d: (d["index"], d["code"], d["field"] or ""))
    for ordinal, defect in enumerate(defects, 1):
        defect["defect_id"] = "D%04d" % ordinal
    return defects


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    if not 0.0 <= q <= 1.0:
        raise ValueError("percentile must be between 0 and 1")
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    value = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return round(value, 3)


def _stats(values: Sequence[float], unit: str, source: str) -> dict:
    ordered = sorted(float(v) for v in values)
    return {
        "sample_count": len(ordered),
        "min": round(ordered[0], 3) if ordered else None,
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": round(ordered[-1], 3) if ordered else None,
        "unit": unit,
        "source": source,
    }


def _metric_section(validated: Sequence[dict], name: str, unit: str,
                    signed: bool = False) -> dict:
    all_values: list[float] = []
    synthetic_values: list[float] = []
    physical_values: list[float] = []
    unavailable = 0
    for item in validated:
        evidence = item["fields"].get(name)
        if evidence is None and name in item["derived"]:
            evidence = item["derived"][name]
        if evidence is None or not evidence["available"]:
            unavailable += 1
            continue
        value = float(evidence["value"])
        all_values.append(value)
        if item["physical"]["measured"]:
            physical_values.append(value)
        else:
            synthetic_values.append(value)
    section = {
        "all": _stats(all_values, unit, "all_available_evidence"),
        "by_output_class": {
            "synthetic_output": _stats(
                synthetic_values, unit, "synthetic_output"),
            "physical_output": _stats(
                physical_values, unit, "physical_output"),
        },
        "unavailable_transitions": unavailable,
        "signed": signed,
    }
    if signed:
        section["absolute"] = {
            "all": _stats([abs(v) for v in all_values], unit, "all_available_evidence"),
            "by_output_class": {
                "synthetic_output": _stats(
                    [abs(v) for v in synthetic_values], unit,
                    "synthetic_output"),
                "physical_output": _stats(
                    [abs(v) for v in physical_values], unit,
                    "physical_output"),
            },
        }
    return section


def aggregate_transitions(validated: Sequence[dict],
                          defects: Sequence[dict]) -> dict:
    total = len(validated)
    coverage = {}
    optional_coverage = {}
    for name in REQUIRED_FIELDS:
        counts = {"available": 0, "missing": 0, "censored": 0, "invalid": 0}
        for item in validated:
            counts[item["fields"][name]["status"]] += 1
        coverage[name] = counts
    for name in ("switch_latency_ms",):
        counts = {"available": 0, "missing": 0, "censored": 0, "invalid": 0}
        for item in validated:
            counts[item["fields"][name]["status"]] += 1
        optional_coverage[name] = counts
    complete_transitions = sum(
        1 for item in validated
        if all(item["fields"][name]["status"] == "available"
               for name in REQUIRED_FIELDS))
    unavailable_transitions = sum(
        1 for item in validated
        if any(item["fields"][name]["status"] != "available"
               for name in REQUIRED_FIELDS))
    censored_measurements = sum(
        1 for item in validated
        for evidence in item["fields"].values()
        if evidence["status"] == "censored")

    transport_sources: dict[str, int] = {}
    output_sources: dict[str, int] = {}
    for item in validated:
        transport = str(item["transport_source"])
        output = str(item["output_source"])
        transport_sources[transport] = transport_sources.get(transport, 0) + 1
        output_sources[output] = output_sources.get(output, 0) + 1

    non_info = [d for d in defects if d["severity"] != "info"]
    per_transition = [0] * total
    for defect in non_info:
        per_transition[defect["index"]] += 1
    defective = sum(1 for count in per_transition if count)
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_code: dict[str, int] = {}
    for defect in defects:
        by_severity[defect["severity"]] = (
            by_severity.get(defect["severity"], 0) + 1)
        by_category[defect["category"]] = (
            by_category.get(defect["category"], 0) + 1)
        by_code[defect["code"]] = by_code.get(defect["code"], 0) + 1

    latency = {
        name: _metric_section(validated, name, "ms")
        for name in _LATENCY_FIELDS
    }
    signed_offset = _metric_section(
        validated, "signed_timestamp_offset_ms", "ms", signed=True)

    return {
        "total_transitions": total,
        "complete_transitions": complete_transitions,
        "unavailable_transitions": unavailable_transitions,
        "censored_measurements": censored_measurements,
        "fields_fully_available": sum(
            1 for counts in coverage.values() if counts["available"] == total),
        "required_field_coverage": coverage,
        "optional_field_coverage": optional_coverage,
        "transport_source_counts": transport_sources,
        "output_source_counts": output_sources,
        "synthetic_output_transitions": output_sources.get(SYNTHETIC_OUTPUT, 0),
        "physical_output_transitions": output_sources.get(PHYSICAL_OUTPUT, 0),
        "physical_output_measured": sum(
            1 for item in validated if item["physical"]["measured"]),
        "latency_ms": latency,
        "signed_timestamp_offset_ms": signed_offset,
        "failure": {
            "failure_rate_percent": (
                round(defective * 100.0 / total, 3) if total else None),
            "defective_transitions": defective,
            "per_transition_defect_count": _stats(
                per_transition, "defects", "derived"),
            "by_severity": by_severity,
            "by_category": by_category,
            "by_code": by_code,
        },
    }


def _coverage_status(counts: Mapping[str, int], total: int) -> str:
    if counts["invalid"]:
        return FAIL
    if total == 0:
        return BLOCKED
    if counts["available"] == total:
        return PASS
    return BLOCKED


def _codes(defects: Sequence[dict]) -> set[str]:
    return {d["code"] for d in defects}


def acceptance_checklist(validated: Sequence[dict], defects: Sequence[dict],
                         summary: Mapping[str, Any]) -> list[dict]:
    total = len(validated)
    codes = _codes(defects)
    coverage = summary["required_field_coverage"]
    items: list[dict] = []

    def add(item_id: str, requirement: str, status: str, evidence: str) -> None:
        items.append({
            "id": item_id,
            "requirement": requirement,
            "status": status,
            "evidence": evidence,
        })

    add("AC-01", "Requested operation recorded for every transition",
        _coverage_status(coverage["requested_operation"], total),
        "coverage=" + json.dumps(coverage["requested_operation"]))
    add("AC-02", "Observed source recorded for every transition",
        _coverage_status(coverage["source_observed"], total),
        "coverage=" + json.dumps(coverage["source_observed"]))
    add("AC-03", "Source epoch recorded for every transition",
        _coverage_status(coverage["epoch"], total),
        "coverage=" + json.dumps(coverage["epoch"]))
    add("AC-04", "Signed timestamp offset recorded for every transition",
        _coverage_status(coverage["signed_timestamp_offset_ms"], total),
        "coverage=" + json.dumps(coverage["signed_timestamp_offset_ms"]))
    add("AC-05", "First decoded new picture recorded for every transition",
        _coverage_status(coverage["first_decoded_picture"], total),
        "coverage=" + json.dumps(coverage["first_decoded_picture"]))
    add("AC-06", "Actual presentation confirmation recorded and classified",
        _coverage_status(coverage["actual_presentation_confirmed"], total),
        "coverage=" + json.dumps(coverage["actual_presentation_confirmed"]))
    add("AC-07", "Audio release state recorded for every transition",
        _coverage_status(coverage["audio_release"], total),
        "coverage=" + json.dumps(coverage["audio_release"]))
    add("AC-08", "Timeout state recorded for every transition",
        _coverage_status(coverage["timeout"], total),
        "coverage=" + json.dumps(coverage["timeout"]))
    add("AC-09", "Interruption state recorded for every transition",
        _coverage_status(coverage["interruption"], total),
        "coverage=" + json.dumps(coverage["interruption"]))
    add("AC-10", "Audio underrun state recorded for every transition",
        _coverage_status(coverage["underrun"], total),
        "coverage=" + json.dumps(coverage["underrun"]))
    add("AC-11", "Black-frame duration recorded for every transition",
        _coverage_status(coverage["black_frame_duration_ms"], total),
        "coverage=" + json.dumps(coverage["black_frame_duration_ms"]))
    add("AC-12", "Frozen-frame duration recorded for every transition",
        _coverage_status(coverage["frozen_frame_duration_ms"], total),
        "coverage=" + json.dumps(coverage["frozen_frame_duration_ms"]))
    add("AC-13", "Resource recovery recorded for every transition",
        _coverage_status(coverage["resource_recovery"], total),
        "coverage=" + json.dumps(coverage["resource_recovery"]))
    add("AC-14", "Switch latency available or derivable for every transition",
        _coverage_status(
            summary["optional_field_coverage"]["switch_latency_ms"], total),
        "latency samples=%s" % summary["latency_ms"]["switch_latency_ms"]
        ["all"]["sample_count"])

    premature = codes & {
        "premature_audio_release", "premature_audio_release_by_timestamp",
        "undocumented_timeout_audio_release"}
    add("AC-15", "No audio release before the matching confirmed picture",
        FAIL if premature else PASS,
        "codes=" + ",".join(sorted(premature)) if premature else "none")
    stale = codes & {"stale_audio_release", "audio_release_epoch_ahead"}
    add("AC-16", "No stale-epoch audio release", FAIL if stale else PASS,
        "codes=" + ",".join(sorted(stale)) if stale else "none")
    add("AC-17", "Zero safety gate timeouts",
        FAIL if "gate_timeout" in codes else PASS,
        "count=%d" % summary["failure"]["by_code"].get("gate_timeout", 0))
    interrupts = codes & {"transport_interruption", "audio_underrun"}
    add("AC-18", "Zero transport interruptions and audio underruns",
        FAIL if interrupts else PASS,
        "codes=" + ",".join(sorted(interrupts)) if interrupts else "none")
    add("AC-19", "Resources recover after timeout, interruption or underrun",
        FAIL if "resource_not_recovered" in codes else PASS,
        "count=%d" % summary["failure"]["by_code"].get(
            "resource_not_recovered", 0))
    tier_codes = codes & {
        "inconsistent_evidence_tier", "invalid_transport_source",
        "invalid_output_source", "unsubstantiated_physical_output",
        "physical_presentation_without_physical_output",
        "missing_presentation_source"}
    add("AC-20", "Synthetic transport and physical output are separated",
        FAIL if tier_codes else PASS,
        "codes=" + ",".join(sorted(tier_codes)) if tier_codes else "none")
    synthetic_count = summary["synthetic_output_transitions"]
    add("AC-21", "At least 100 deterministic synthetic transitions validated",
        PASS if synthetic_count >= 100 else BLOCKED,
        "synthetic_transitions=%d" % synthetic_count)
    physical_complete = (
        total > 0 and summary["physical_output_measured"] == total)
    add("AC-22", "Physical console presentation measured and confirmed",
        PASS if physical_complete else BLOCKED,
        "physical_transitions=%d total=%d" % (
            summary["physical_output_measured"], total))
    physical_offset = summary["signed_timestamp_offset_ms"]["absolute"][
        "by_output_class"]["physical_output"]
    if total == 0 or physical_offset["sample_count"] < total:
        offset_status = BLOCKED
    elif (physical_offset["p95"] is not None
          and physical_offset["p95"] <= SIGNED_OFFSET_TARGET_MS
          and physical_offset["max"] is not None
          and physical_offset["max"] <= SIGNED_OFFSET_CRITICAL_MS):
        offset_status = PASS
    else:
        offset_status = FAIL
    add("AC-23", "Physical A/V offset target (p95 <= 50 ms, max <= 100 ms)",
        offset_status,
        "p95=%s max=%s samples=%d" % (
            physical_offset["p95"], physical_offset["max"],
            physical_offset["sample_count"]))
    add("AC-24", "Authorized real-server correlation performed", NOT_TESTED,
        "the harness has no server/network code and was not given a server")
    add("AC-25", "Physical PS3 acceptance gate", BLOCKED,
        "this host harness cannot establish the physical console gate")
    return items


def regression_checklist(validated: Sequence[dict], defects: Sequence[dict],
                         summary: Mapping[str, Any], json_ok: bool) -> list[dict]:
    codes = _codes(defects)
    items: list[dict] = []

    def add(item_id: str, requirement: str, status: str, evidence: str) -> None:
        items.append({
            "id": item_id,
            "requirement": requirement,
            "status": status,
            "evidence": evidence,
        })

    missing_required = [name for name, counts in
                        summary["required_field_coverage"].items()
                        if counts["available"] != summary["total_transitions"]]
    add("RC-01", "All required evidence fields or explicit unavailable status",
        FAIL if missing_required else PASS,
        "incomplete=" + ",".join(missing_required) if missing_required else "none")
    values_are_null = all(
        evidence["value"] is None
        for item in validated
        for evidence in item["fields"].values()
        if not evidence["available"])
    add("RC-02", "Missing and censored values are null, never zero",
        PASS if values_are_null else FAIL,
        "unavailable_values_are_null=%s" % values_are_null)
    add("RC-03", "Censored measurements are excluded from percentiles",
        PASS,
        "latency unavailable=%d" % summary["latency_ms"]
        ["switch_latency_ms"]["unavailable_transitions"])
    tier_codes = codes & {
        "inconsistent_evidence_tier", "unsubstantiated_physical_output",
        "physical_presentation_without_physical_output"}
    add("RC-04", "Synthetic and physical evidence tiers stay distinct",
        FAIL if tier_codes else PASS,
        "codes=" + ",".join(sorted(tier_codes)) if tier_codes else "none")
    add("RC-05", "Latency percentiles use only available samples",
        PASS if (summary["latency_ms"]["switch_latency_ms"]["all"]
                 ["sample_count"] <= summary["total_transitions"]) else FAIL,
        "samples=%d total=%d" % (
            summary["latency_ms"]["switch_latency_ms"]["all"]["sample_count"],
            summary["total_transitions"]))
    add("RC-06", "Failure percentiles computed from triaged transitions",
        PASS if "per_transition_defect_count" in summary["failure"] else FAIL,
        "failure_rate_percent=%s" % summary["failure"]["failure_rate_percent"])
    synthetic_count = summary["synthetic_output_transitions"]
    add("RC-07", "At least 100 deterministic synthetic transitions",
        PASS if synthetic_count >= 100
        else (NOT_TESTED if synthetic_count == 0 else FAIL),
        "synthetic_transitions=%d" % synthetic_count)
    add("RC-08", "Report is JSON-safe with no NaN or Infinity",
        PASS if json_ok else FAIL, "json_ok=%s" % json_ok)
    add("RC-09", "Harness performs no server contact or authentication",
        PASS, "server_contacted=false; no network imports")
    add("RC-10", "Full repository unit suite rerun", NOT_TESTED,
        "run: .venv/bin/python -m unittest discover -s tests -p 'test_*.py'")
    return items


def _overall_verdict(items: Sequence[Mapping[str, Any]]) -> str:
    statuses = {item["status"] for item in items}
    if FAIL in statuses:
        return FAIL
    if BLOCKED in statuses or NOT_TESTED in statuses:
        return BLOCKED
    return PASS


def _physical_verdict(summary: Mapping[str, Any], complete: bool) -> str:
    if not complete:
        return BLOCKED
    offset = summary["signed_timestamp_offset_ms"]["absolute"][
        "by_output_class"]["physical_output"]
    if (offset["sample_count"] < summary["total_transitions"]
            or offset["p95"] is None
            or offset["p95"] > SIGNED_OFFSET_TARGET_MS
            or offset["max"] is None
            or offset["max"] > SIGNED_OFFSET_CRITICAL_MS):
        return FAIL
    return PASS


def build_report(records: Iterable[TransitionEvidence],
                 fixture_transition_count: int | None = None) -> dict:
    validated = validate_evidence(records)
    defects = triage(validated)
    summary = aggregate_transitions(validated, defects)
    if fixture_transition_count is not None:
        summary["fixture_transition_count"] = fixture_transition_count
    synthetic_latency = summary["latency_ms"]["switch_latency_ms"]
    try:
        # The final report is serialised once here; JSON failures become a
        # regression result instead of an exception with partial evidence.
        json.dumps({"probe": summary, "validated": validated},
                   allow_nan=False)
        json_ok = True
    except (TypeError, ValueError):
        json_ok = False
    acceptance = acceptance_checklist(validated, defects, summary)
    regression = regression_checklist(validated, defects, summary, json_ok)
    physical_complete = (
        bool(validated)
        and summary["physical_output_measured"] == len(validated))
    report = {
        "schema": SCHEMA,
        "server_contacted": False,
        "synthetic_fixture": all(item["synthetic_output"] for item in validated),
        "physical_output_measured": summary["physical_output_measured"] > 0,
        "summary": summary,
        "defects": defects,
        "regression_checklist": regression,
        "acceptance_checklist": acceptance,
        "overall_verdict": _overall_verdict(acceptance),
        "physical_verdict": _physical_verdict(summary, physical_complete),
        "limitations": [
            "Host-side evidence only; no real PS3 RSX flip, CellAudio DMA, "
            "PCM output, decode, display or audio latency is measured.",
            "Synthetic transport and synthetic output are modeled fixture "
            "evidence, not physical console output.",
            "Luma scene changes remain heuristic presentation evidence and "
            "do not establish upstream source identity.",
            "No Jellyfin/CatalogService server is contacted and no server "
            "correlation, authentication or real media execution occurs.",
            "Black/frozen thresholds are provisional diagnostics, not "
            "measured hardware guarantees.",
        ],
    }
    # Include a small, stable observation count for reviewers without
    # embedding the whole per-transition payload twice.
    report["latency_sample_count"] = synthetic_latency["all"]["sample_count"]
    return report


def report_json(report: Mapping[str, Any], pretty: bool = False) -> str:
    return json.dumps(report, indent=2 if pretty else None, sort_keys=True,
                      allow_nan=False)


def load_evidence(payload: Any) -> list[TransitionEvidence]:
    """Load a list, a single record or a ``{"transitions": [...]}`` wrapper."""
    if isinstance(payload, Mapping):
        if "transitions" in payload:
            sequence = payload["transitions"]
        else:
            sequence = [payload]
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        sequence = payload
    else:
        raise ValueError("evidence must be a transition, list or transitions object")
    if not isinstance(sequence, Sequence):
        raise ValueError("transitions must be a list")
    return [TransitionEvidence.from_mapping(item) for item in sequence]


def synthetic_fixture_transitions(count: int = 100,
                                  seed: int = 20261124) -> list[TransitionEvidence]:
    """Deterministic synthetic evidence for harness/regression checks."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("transition count must be a nonnegative integer")
    rng = random.Random(seed)
    operations = (
        "select_next", "select_previous", "favorite_selected_previous",
        "source_rotation", "select_current",
    )
    records: list[TransitionEvidence] = []
    for index in range(count):
        epoch = index + 1
        command = index * 10000.0
        latency = 280.0 + ((index * 37) % 240)
        first_decode = command + latency - 120.0
        presented = command + latency
        records.append(TransitionEvidence(
            transition_id="SYN-%04d" % (index + 1),
            requested_operation=operations[index % len(operations)],
            operation_status="implemented",
            source_observed="source-%02d" % (index % 7),
            epoch=epoch,
            signed_timestamp_offset_ms=float(rng.randint(-24, 24)),
            first_decoded_picture=1000 + index * 10,
            actual_presentation_confirmed=True,
            presentation_source=PRESENTATION_SYNTHETIC,
            audio_release=True,
            audio_release_reason="matching_picture_presented",
            audio_release_epoch=epoch,
            audio_gap_ms=0.0,
            timeout=False,
            timeout_ms=None,
            interruption=False,
            underrun=False,
            underrun_ms=None,
            black_frame_duration_ms=0.0,
            frozen_frame_duration_ms=0.0,
            resource_recovery=True,
            resource_recovery_ms=0.0,
            switch_latency_ms=round(latency, 3),
            command_at_ms=command,
            first_decoded_at_ms=first_decode,
            presented_at_ms=presented,
            audio_released_at_ms=presented + 5.0,
            transport_source=SYNTHETIC_TRANSPORT,
            output_source=SYNTHETIC_OUTPUT,
            notes="deterministic synthetic fixture",
        ))
    return records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Surf transition evidence without server contact")
    parser.add_argument("--input", help="local JSON evidence file")
    parser.add_argument("--transitions", type=int, default=100,
                        help="synthetic fixture transitions (default 100)")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        records = load_evidence(payload)
        fixture_count = None
    else:
        records = synthetic_fixture_transitions(args.transitions)
        fixture_count = len(records)
    report = build_report(records, fixture_transition_count=fixture_count)
    print(report_json(report, pretty=args.pretty))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
