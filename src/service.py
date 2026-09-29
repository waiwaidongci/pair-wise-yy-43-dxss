from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_segment_sensitivity,
                     normalize_severity, optional_number, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    SENSITIVITY_LABELS, SEGMENT_CLAIM_ROLES, SEGMENT_COMPLETE_ROLES,
                    SEGMENT_ENTITY, SEGMENT_REGISTER_ROLES, SEGMENT_REOIL_ROLES,
                    SEGMENT_REVIEW_ROLES, SEGMENT_STATUS_LABELS, TITLE, VIEW_ROLES,
                    complete_outcome, completion_blockers, escalation_required,
                    film_threshold, priority_score, response_deadline_hours,
                    role_for_transition, segment_closure_blockers,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def _require_open_event(self, item: Dict[str, Any]) -> None:
        if item["status"] == "closed":
            raise ConflictError("事件已关闭，不能再操作岸线段")

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = self.close_blockers(item_id)
        if target in ("closed",) and blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 岸线段台账 ----
    def register_segment(self, item_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SEGMENT_REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        self._require_open_event(item)
        code = require_text(payload.get("code"), "code", 60)
        location = require_text(payload.get("location"), "location", 300)
        sensitivity = normalize_segment_sensitivity(payload.get("sensitivity"))
        threshold = optional_number(payload.get("film_threshold_um"),
                                    "film_threshold_um", 0.0)
        if threshold is None:
            threshold = film_threshold(sensitivity)
        segment = self.repository.create_segment(item_id, code, location,
                                                 sensitivity, threshold, actor)
        self.repository.append_audit("segment_register", SEGMENT_ENTITY,
                                     segment["id"], actor, {
                                         "item_id": item_id, "code": code,
                                         "sensitivity": sensitivity,
                                         "film_threshold_um": threshold,
                                     })
        return self.enrich_segment(segment)

    def claim_segment(self, item_id: int, segment_id: int,
                      payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SEGMENT_CLAIM_ROLES)
        actor = require_text(actor, "actor", 100)
        team = require_text(payload.get("team"), "team", 120)
        expected_version = self._expected_version(payload)
        segment = self.repository.get_segment(segment_id)
        if segment["item_id"] != item_id:
            raise ConflictError("岸线段不属于该事件")
        self._require_open_event(self.repository.get_item(item_id))
        updated = self.repository.claim_segment(segment_id, team, actor,
                                                expected_version)
        self.repository.append_audit("segment_claim", SEGMENT_ENTITY, segment_id,
                                     actor, {"item_id": item_id, "team": team})
        return self.enrich_segment(updated)

    def complete_segment(self, item_id: int, segment_id: int,
                         payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SEGMENT_COMPLETE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._expected_version(payload)
        film = optional_number(payload.get("film_thickness_um"),
                               "film_thickness_um", 0.0)
        cleaned = optional_number(payload.get("cleaned_quantity"),
                                  "cleaned_quantity", 0.0)
        segment = self.repository.get_segment(segment_id)
        if segment["item_id"] != item_id:
            raise ConflictError("岸线段不属于该事件")
        self._require_open_event(self.repository.get_item(item_id))
        outcome = complete_outcome(film, cleaned, segment["sensitivity"])
        updated = self.repository.complete_segment(segment_id, film, cleaned,
                                                   outcome, actor, expected_version)
        self.repository.append_audit("segment_complete", SEGMENT_ENTITY, segment_id,
                                     actor, {
                                         "item_id": item_id,
                                         "film_thickness_um": film,
                                         "cleaned_quantity": cleaned,
                                         "outcome": outcome,
                                         "threshold_um": segment["film_threshold_um"],
                                         "data_complete": film is not None and cleaned is not None,
                                     })
        return self.enrich_segment(updated)

    def review_segment(self, item_id: int, segment_id: int,
                       payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SEGMENT_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        passed = payload.get("passed")
        if not isinstance(passed, bool):
            raise ValueError("passed必须是布尔值")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        expected_version = self._expected_version(payload)
        segment = self.repository.get_segment(segment_id)
        if segment["item_id"] != item_id:
            raise ConflictError("岸线段不属于该事件")
        updated = self.repository.review_segment(segment_id, passed, note, actor,
                                                 expected_version)
        self.repository.append_audit("segment_review", SEGMENT_ENTITY, segment_id,
                                     actor, {"item_id": item_id, "passed": passed,
                                             "note": note})
        return self.enrich_segment(updated)

    def reoil_segment(self, item_id: int, segment_id: int,
                      payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, SEGMENT_REOIL_ROLES)
        actor = require_text(actor, "actor", 100)
        note = require_text(payload.get("note"), "note", 500)
        expected_version = self._expected_version(payload)
        segment = self.repository.get_segment(segment_id)
        if segment["item_id"] != item_id:
            raise ConflictError("岸线段不属于该事件")
        self._require_open_event(self.repository.get_item(item_id))
        updated = self.repository.reoil_segment(segment_id, note, actor,
                                                expected_version)
        self.repository.append_audit("segment_reoil", SEGMENT_ENTITY, segment_id,
                                     actor, {"item_id": item_id, "note": note})
        return self.enrich_segment(updated)

    def list_segments(self, item_id: int, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        self.repository.get_item(item_id)
        return [self.enrich_segment(s)
                for s in self.repository.list_segments(item_id)]

    def get_segment(self, item_id: int, segment_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        segment = self.repository.get_segment(segment_id)
        if segment["item_id"] != item_id:
            from .domain import NotFoundError
            raise NotFoundError("岸线段不属于该事件")
        result = self.enrich_segment(segment)
        result["jobs"] = self.repository.list_segment_jobs(segment_id)
        return result

    def close_blockers(self, item_id: int) -> List[str]:
        blockers = completion_blockers("closed",
                                      self.repository.open_record_count(item_id))
        blockers += segment_closure_blockers(self.repository.list_segments(item_id))
        return blockers

    def blockers(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        self.repository.get_item(item_id)
        reasons = self.close_blockers(item_id)
        return {"item_id": item_id, "can_close": not reasons, "blockers": reasons}

    @staticmethod
    def _expected_version(payload: Dict[str, Any]) -> int:
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_version必须是正整数")
        return expected

    @staticmethod
    def enrich_segment(segment: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(segment)
        result["status_label"] = SEGMENT_STATUS_LABELS.get(segment["status"],
                                                           segment["status"])
        result["sensitivity_label"] = SENSITIVITY_LABELS.get(
            segment["sensitivity"], segment["sensitivity"])
        film = segment.get("film_thickness_um")
        limit = segment.get("film_threshold_um")
        if segment["status"] == "reinspection":
            if film is None or segment.get("cleaned_quantity") is None:
                result["reinspection_reason"] = "完成资料不全（缺实测油膜厚度或清理量）"
            else:
                result["reinspection_reason"] = f"实测油膜{film}μm超过阈值{limit}μm"
        else:
            result["reinspection_reason"] = None
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
