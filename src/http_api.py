from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc)})

        @staticmethod
        def _segment_route(path: str):
            """解析 /api/items/{id}/segments[/{sid}[/{action}]] 形式的路径。"""
            parts = [p for p in path.split("/") if p]
            if len(parts) < 4 or parts[0] != "api" or parts[1] != "items" \
                    or parts[3] != "segments":
                return None
            try:
                item_id = int(parts[2])
            except ValueError:
                raise NotFoundError("路径无效")
            if len(parts) == 4:
                return item_id, None, None
            try:
                segment_id = int(parts[4])
            except ValueError:
                raise NotFoundError("路径无效")
            action = parts[5] if len(parts) > 5 else None
            if len(parts) > 6 or action not in (None, "claim", "complete",
                                                "review", "reoil"):
                raise NotFoundError("路径无效")
            return item_id, segment_id, action

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/") and path.endswith("/blockers"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.blockers(item_id, role))
                elif path.startswith("/api/items/") and "/segments" in path:
                    actor, role = self._identity()
                    parsed = self._segment_route(path)
                    if parsed is None:
                        self._json(404, {"error": "not_found"})
                        return
                    item_id, segment_id, action = parsed
                    if action is not None:
                        self._json(404, {"error": "not_found"})
                    elif segment_id is None:
                        self._json(200, {"segments": service.list_segments(item_id, role)})
                    else:
                        self._json(200, service.get_segment(item_id, segment_id, role))
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path.startswith("/api/items/") and "/segments" in path:
                    parsed = self._segment_route(path)
                    if parsed is None:
                        self._json(404, {"error": "not_found"})
                        return
                    item_id, segment_id, action = parsed
                    if segment_id is None:
                        if action is not None:
                            self._json(404, {"error": "not_found"})
                        else:
                            self._json(201, service.register_segment(
                                item_id, body, actor, role))
                    elif action == "claim":
                        self._json(200, service.claim_segment(
                            item_id, segment_id, body, actor, role))
                    elif action == "complete":
                        self._json(200, service.complete_segment(
                            item_id, segment_id, body, actor, role))
                    elif action == "review":
                        self._json(200, service.review_segment(
                            item_id, segment_id, body, actor, role))
                    elif action == "reoil":
                        self._json(200, service.reoil_segment(
                            item_id, segment_id, body, actor, role))
                    else:
                        self._json(404, {"error": "not_found"})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
