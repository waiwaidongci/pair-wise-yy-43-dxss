import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import (OIL_FILM_THRESHOLDS_UM, STATES, TRANSITION_ROLES,
                       complete_outcome, segment_closure_blockers)


class SegmentServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "岸线清污", "description": "段级台账", "severity": "major",
             "quantity": 8, "threshold": 6, "external_ref": "SEG-1"},
            "creator", "observer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _drive_to_monitoring(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "cmd",
                TRANSITION_ROLES[target][0])
        return current

    def test_register_claim_complete_and_review_flow(self):
        seg = self.service.register_segment(
            self.item["id"],
            {"code": "S-01", "location": "北岸礁石区", "sensitivity": "high"},
            "scout", "observer")
        self.assertEqual(seg["status"], "open")
        self.assertEqual(seg["film_threshold_um"], OIL_FILM_THRESHOLDS_UM["high"])

        claimed = self.service.claim_segment(
            self.item["id"], seg["id"], {"team": "甲队", "expected_version": 1},
            "lead-a", "operations")
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["claimed_by"], "甲队")

        # 同一段不能被第二支队伍重复领取
        with self.assertRaises(ConflictError):
            self.service.claim_segment(
                self.item["id"], seg["id"],
                {"team": "乙队", "expected_version": claimed["version"]},
                "lead-b", "operations")

        # 资料不全 -> 待复检
        incomplete = self.service.complete_segment(
            self.item["id"], seg["id"], {"expected_version": 2},
            "lead-a", "operations")
        self.assertEqual(incomplete["status"], "reinspection")
        self.assertIn("资料不全", incomplete["reinspection_reason"])

        # 复核不通过 -> 退回待领取，乙队可重新领取
        reopened = self.service.review_segment(
            self.item["id"], seg["id"],
            {"passed": False, "expected_version": 3}, "cmd", "response_commander")
        self.assertEqual(reopened["status"], "open")
        self.assertIsNone(reopened["claimed_by"])
        claimed2 = self.service.claim_segment(
            self.item["id"], seg["id"], {"team": "乙队", "expected_version": 4},
            "lead-b", "operations")
        self.assertEqual(claimed2["claimed_by"], "乙队")

        # 实测达标 -> 完成
        done = self.service.complete_segment(
            self.item["id"], seg["id"],
            {"film_thickness_um": 10, "cleaned_quantity": 120,
             "expected_version": 5}, "lead-b", "operations")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["film_thickness_um"], 10)

        detail = self.service.get_segment(self.item["id"], seg["id"], "viewer")
        self.assertEqual(len(detail["jobs"]), 2)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_threshold_exceeded_goes_to_reinspection(self):
        seg = self.service.register_segment(
            self.item["id"],
            {"code": "S-02", "location": "滩涂", "sensitivity": "critical"},
            "scout", "observer")
        self.service.claim_segment(
            self.item["id"], seg["id"], {"team": "甲队", "expected_version": 1},
            "lead", "operations")
        over = self.service.complete_segment(
            self.item["id"], seg["id"],
            {"film_thickness_um": 30, "cleaned_quantity": 50, "expected_version": 2},
            "lead", "operations")
        self.assertEqual(over["status"], "reinspection")
        self.assertIn("超过阈值", over["reinspection_reason"])
        # 复核合格
        passed = self.service.review_segment(
            self.item["id"], seg["id"],
            {"passed": True, "expected_version": 3}, "cmd", "response_commander")
        self.assertEqual(passed["status"], "completed")

    def test_reoil_invalidates_completion_and_blocks_close(self):
        seg = self.service.register_segment(
            self.item["id"],
            {"code": "S-03", "location": "河口", "sensitivity": "moderate"},
            "scout", "observer")
        self.service.claim_segment(
            self.item["id"], seg["id"], {"team": "甲队", "expected_version": 1},
            "lead", "operations")
        self.service.complete_segment(
            self.item["id"], seg["id"],
            {"film_thickness_um": 10, "cleaned_quantity": 40, "expected_version": 2},
            "lead", "operations")
        reoiled = self.service.reoil_segment(
            self.item["id"], seg["id"],
            {"note": "涨潮后岸线重新返油", "expected_version": 3},
            "patrol", "observer")
        self.assertEqual(reoiled["status"], "reoiled")
        self.assertIsNone(reoiled["claimed_by"])
        jobs = self.repo.list_segment_jobs(seg["id"])
        self.assertTrue(jobs[0]["completion_invalid"])

        current = self._drive_to_monitoring()
        blockers = self.service.close_blockers(self.item["id"])
        self.assertTrue(any("返油" in b for b in blockers))
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.item["id"], "closed", current["version"], "cmd",
                "response_commander")

        # 重新领取并完成后才允许关闭
        claimed = self.service.claim_segment(
            self.item["id"], seg["id"], {"team": "乙队", "expected_version": 4},
            "lead2", "operations")
        self.service.complete_segment(
            self.item["id"], seg["id"],
            {"film_thickness_um": 8, "cleaned_quantity": 30,
             "expected_version": claimed["version"]},
            "lead2", "operations")
        self.assertEqual(self.service.close_blockers(self.item["id"]), [])
        closed = self.service.transition(
            self.item["id"], "closed", current["version"], "cmd",
            "response_commander")
        self.assertEqual(closed["status"], "closed")

        # 关闭后不能再登记或操作段
        with self.assertRaises(ConflictError):
            self.service.register_segment(
                self.item["id"], {"code": "S-04", "location": "x",
                                  "sensitivity": "low"}, "scout", "observer")

    def test_unfinished_segments_block_close(self):
        current = self._drive_to_monitoring()
        seg = self.service.register_segment(
            self.item["id"],
            {"code": "S-05", "location": "码头", "sensitivity": "low"},
            "scout", "observer")
        blockers = self.service.blockers(self.item["id"], "viewer")
        self.assertFalse(blockers["can_close"])
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.item["id"], "closed", current["version"], "cmd",
                "response_commander")
        self.service.claim_segment(
            self.item["id"], seg["id"], {"team": "甲队", "expected_version": 1},
            "lead", "operations")
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.item["id"], "closed", current["version"], "cmd",
                "response_commander")

    def test_duplicate_code_and_role_guards(self):
        payload = {"code": "S-06", "location": "a", "sensitivity": "low"}
        self.service.register_segment(self.item["id"], payload, "scout", "observer")
        with self.assertRaises(ConflictError):
            self.service.register_segment(self.item["id"], payload, "scout", "observer")
        seg = self.service.list_segments(self.item["id"], "viewer")[0]
        with self.assertRaises(PermissionDenied):
            self.service.claim_segment(
                self.item["id"], seg["id"], {"team": "丙队", "expected_version": 1},
                "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.review_segment(
                self.item["id"], seg["id"], {"passed": True, "expected_version": 1},
                "x", "operations")

    def test_stale_version_conflict(self):
        seg = self.service.register_segment(
            self.item["id"], {"code": "S-07", "location": "a",
                              "sensitivity": "low"}, "scout", "observer")
        self.service.claim_segment(
            self.item["id"], seg["id"], {"team": "甲队", "expected_version": 1},
            "lead", "operations")
        with self.assertRaises(ConflictError):
            self.service.complete_segment(
                self.item["id"], seg["id"],
                {"film_thickness_um": 1, "cleaned_quantity": 1,
                 "expected_version": 1}, "lead", "operations")

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.service.register_segment(
                self.item["id"], {"code": "S-08", "location": "a",
                                  "sensitivity": "unknown"}, "scout", "observer")
        with self.assertRaises(ValueError):
            seg = self.service.register_segment(
                self.item["id"], {"code": "S-09", "location": "a",
                                  "sensitivity": "low"}, "scout", "observer")
            self.service.claim_segment(
                self.item["id"], seg["id"], {"team": "甲队"}, "lead", "operations")

    def test_pure_rules(self):
        self.assertEqual(complete_outcome(None, 10, "low"), "reinspection")
        self.assertEqual(complete_outcome(5, None, "low"), "reinspection")
        self.assertEqual(complete_outcome(50, 10, "high"), "reinspection")
        self.assertEqual(complete_outcome(10, 10, "high"), "completed")
        blockers = segment_closure_blockers([
            {"id": 1, "code": "A", "status": "reinspection"},
            {"id": 2, "code": "B", "status": "reoiled"},
            {"id": 3, "code": "C", "status": "completed"},
        ])
        self.assertEqual(len(blockers), 2)


if __name__ == "__main__":
    unittest.main()
