import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class SegmentLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "岸线溢油", "description": "段级台账", "severity": "major",
             "quantity": 12, "threshold": 6, "external_ref": "SEG-1"},
            "creator", "observer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _advance_to(self, target):
        current = self.service.get_item(self.item["id"], "viewer")
        for t in STATES[1:]:
            if current["status"] == target:
                break
            role = TRANSITION_ROLES[t][0]
            current = self.service.transition(
                current["id"], t, current["version"], "reviewer", role)
        return current

    def test_register_and_claim_segment(self):
        seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        self.assertEqual(seg["status"], "open")
        claimed = self.service.claim_segment(self.item["id"], seg["id"], "team1", "operations")
        self.assertEqual(claimed["status"], "occupied")
        self.assertEqual(claimed["claimed_by"], "team1")

    def test_duplicate_claim_conflict(self):
        seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg["id"], "team1", "operations")
        with self.assertRaises(ConflictError):
            self.service.claim_segment(self.item["id"], seg["id"], "team2", "operations")

    def test_complete_threshold_and_incomplete_data(self):
        seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg["id"], "team1", "operations")
        # 油膜厚度超过验收阈值 -> 待复检
        over = self.service.complete_segment(
            self.item["id"], seg["id"],
            {"oil_film_thickness": 1.5, "cleanup_amount": 3.0},
            "team1", "operations")
        self.assertEqual(over["status"], "pending_recheck")

        seg2 = self.service.add_segment(
            self.item["id"], {"name": "B段", "sensitivity": "low"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg2["id"], "team2", "operations")
        # 资料不全（缺清理量）-> 待复检
        incomplete = self.service.complete_segment(
            self.item["id"], seg2["id"], {"oil_film_thickness": 0.5},
            "team2", "operations")
        self.assertEqual(incomplete["status"], "pending_recheck")

    def test_complete_good_data(self):
        seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg["id"], "team1", "operations")
        done = self.service.complete_segment(
            self.item["id"], seg["id"],
            {"oil_film_thickness": 0.4, "cleanup_amount": 2.0},
            "team1", "operations")
        self.assertEqual(done["status"], "completed")

    def test_reinspect_pass_and_fail(self):
        seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg["id"], "team1", "operations")
        self.service.complete_segment(
            self.item["id"], seg["id"],
            {"oil_film_thickness": 1.5, "cleanup_amount": 3.0},
            "team1", "operations")
        passed = self.service.reinspect_segment(
            self.item["id"], seg["id"], {"decision": "pass"},
            "commander", "response_commander")
        self.assertEqual(passed["status"], "completed")

        seg2 = self.service.add_segment(
            self.item["id"], {"name": "B段", "sensitivity": "low"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg2["id"], "team2", "operations")
        self.service.complete_segment(
            self.item["id"], seg2["id"],
            {"oil_film_thickness": 2.0, "cleanup_amount": 1.0},
            "team2", "operations")
        failed = self.service.reinspect_segment(
            self.item["id"], seg2["id"], {"decision": "fail", "note": "仍有油膜"},
            "commander", "response_commander")
        self.assertEqual(failed["status"], "open")

    def test_reoil_invalidates_completion(self):
        seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg["id"], "team1", "operations")
        self.service.complete_segment(
            self.item["id"], seg["id"],
            {"oil_film_thickness": 0.4, "cleanup_amount": 2.0},
            "team1", "operations")
        reoiled = self.service.reoil_segment(
            self.item["id"], seg["id"], {"note": "涨潮返油"},
            "team1", "operations")
        self.assertEqual(reoiled["status"], "reopened")
        # 返油后可重新领段
        reclaimed = self.service.claim_segment(
            self.item["id"], seg["id"], "team1", "operations")
        self.assertEqual(reclaimed["status"], "occupied")

    def test_close_blocked_by_unfinished_and_reoiled(self):
        open_seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        reoil_seg = self.service.add_segment(
            self.item["id"], {"name": "B段", "sensitivity": "low"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], reoil_seg["id"], "team2", "operations")
        self.service.complete_segment(
            self.item["id"], reoil_seg["id"],
            {"oil_film_thickness": 0.3, "cleanup_amount": 1.0},
            "team2", "operations")
        self.service.reoil_segment(
            self.item["id"], reoil_seg["id"], {"note": "涨潮"},
            "team2", "operations")

        current = self._advance_to(STATES[-2])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                current["id"], STATES[-1], current["version"],
                "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        message = str(ctx.exception)
        self.assertIn("未完成岸线段", message)
        self.assertIn("复油岸线段", message)

    def test_close_succeeds_after_all_completed(self):
        seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg["id"], "team1", "operations")
        self.service.complete_segment(
            self.item["id"], seg["id"],
            {"oil_film_thickness": 0.4, "cleanup_amount": 2.0},
            "team1", "operations")
        current = self._advance_to(STATES[-2])
        closed = self.service.transition(
            current["id"], STATES[-1], current["version"],
            "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"], STATES[-1])
        self.assertEqual(closed["blockers"], [])

    def test_segment_actions_leave_audit_trail(self):
        seg = self.service.add_segment(
            self.item["id"], {"name": "A段", "sensitivity": "high"},
            "recorder", "response_commander")
        self.service.claim_segment(self.item["id"], seg["id"], "team1", "operations")
        self.service.complete_segment(
            self.item["id"], seg["id"],
            {"oil_film_thickness": 0.4, "cleanup_amount": 2.0},
            "team1", "operations")
        events = self.service.audit("viewer", self.item["id"])
        actions = [e["action"] for e in events if e["entity_type"] == "segment"]
        self.assertIn("segment_register", actions)
        self.assertIn("segment_claim", actions)
        self.assertIn("segment_complete", actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
