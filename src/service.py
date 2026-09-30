from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ValidationError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    evaluate_segment_completion, normalize_sensitivity,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

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
        blockers = completion_blockers(
            target,
            self.repository.open_record_count(item_id),
            self.repository.count_unfinished_segments(item_id),
            self.repository.count_reoiled_segments(item_id),
        )
        if blockers:
            from .domain import ConflictError
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

    def add_segment(self, item_id: int, payload: Dict[str, Any], actor: str,
                    role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        sensitivity = normalize_sensitivity(payload.get("sensitivity"))
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        segment = self.repository.add_segment(item_id, name, sensitivity,
                                              external_ref, actor)
        self.repository.append_audit("segment_register", "segment", segment["id"], actor, {
            "item_id": item_id, "name": name, "sensitivity": sensitivity,
        })
        return segment

    def list_segments(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_segments(item_id)

    def claim_segment(self, item_id: int, seg_id: int, actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        segment = self.repository.claim_segment(item_id, seg_id, actor)
        self.repository.append_audit("segment_claim", "segment", segment["id"], actor, {
            "item_id": item_id, "name": segment["name"],
        })
        return segment

    def complete_segment(self, item_id: int, seg_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        thickness = self._optional_number(payload.get("oil_film_thickness"),
                                          "oil_film_thickness")
        cleanup_amount = self._optional_number(payload.get("cleanup_amount"),
                                               "cleanup_amount")
        target_status = evaluate_segment_completion(thickness, cleanup_amount)
        segment = self.repository.complete_segment(item_id, seg_id, target_status,
                                                   thickness, cleanup_amount, actor)
        self.repository.append_audit("segment_complete", "segment", segment["id"], actor, {
            "item_id": item_id, "oil_film_thickness": thickness,
            "cleanup_amount": cleanup_amount, "result": target_status,
        })
        return segment

    def reinspect_segment(self, item_id: int, seg_id: int, payload: Dict[str, Any],
                           actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = require_text(payload.get("decision"), "decision", 20)
        if decision not in ("pass", "fail"):
            raise ValidationError("decision必须是pass或fail")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        segment = self.repository.reinspect_segment(item_id, seg_id, decision, actor)
        self.repository.append_audit("segment_reinspect", "segment", segment["id"], actor, {
            "item_id": item_id, "decision": decision, "note": note,
        })
        return segment

    def reoil_segment(self, item_id: int, seg_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        segment = self.repository.reoil_segment(item_id, seg_id, actor)
        self.repository.append_audit("segment_reoil", "segment", segment["id"], actor, {
            "item_id": item_id, "note": note,
        })
        return segment

    @staticmethod
    def _optional_number(value: Any, field: str) -> Optional[float]:
        if value is None:
            return None
        return require_number(value, field)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["segment_summary"] = self.repository.segment_summary(item["id"])
        result["blockers"] = completion_blockers(
            "closed",
            self.repository.open_record_count(item["id"]),
            self.repository.count_unfinished_segments(item["id"]),
            self.repository.count_reoiled_segments(item["id"]),
        )
        return result
