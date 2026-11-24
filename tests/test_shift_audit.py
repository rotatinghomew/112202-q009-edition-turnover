import unittest
from pathlib import Path

import cabinet_audit as ca


ROOT = Path(__file__).resolve().parents[1] / "data"


class AuditInvariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet = ca.load_packet(ROOT)

    def test_current_match_is_unique_after_exclusivity(self):
        snap = ca.world_snapshot(self.packet, 12)
        self.assertEqual(snap["world_count"], 2)
        self.assertEqual(snap["feasible_world_count"], 1)
        assignment = snap["worlds"][0]["assignment"]
        self.assertEqual(assignment["E-U17"], "N-018")
        self.assertEqual(assignment["E-U18"], "N-017")
        self.assertEqual(assignment["E-U19"], "N-018")
        self.assertEqual(assignment["E-U35"], "N-036")

    def test_current_backlog_and_undelivered_are_not_doubled(self):
        snap = ca.world_snapshot(self.packet, 12)
        self.assertEqual(snap["metric_ranges"]["scalars"]["backlog_total"], [13])
        self.assertEqual(snap["metric_ranges"]["scalars"]["undelivered_total"], [1])
        summaries = ca.serial_summaries(self.packet, 12)
        backlog = [s for s, x in summaries.items() if x["backlog_candidate"]]
        self.assertEqual(len(backlog), 13)
        self.assertTrue(all(summaries[s]["origin_channel"] == "south" for s in backlog))

        for dimension in ("edition", "price_band", "origin_channel"):
            groups = snap["worlds"][0]["turnover"][dimension]
            self.assertEqual(sum(x["serial_count"] for x in groups.values()), 66)

    def test_knowledge_arrival_dates_change_commitment_metrics(self):
        day9 = ca.world_snapshot(self.packet, 9)
        self.assertEqual(day9["metric_ranges"]["scalars"]["undelivered_total"], [1, 2])

        day10 = ca.world_snapshot(self.packet, 10)
        self.assertEqual(day10["metric_ranges"]["scalars"]["undelivered_total"], [2])

        day11 = ca.world_snapshot(self.packet, 11)
        self.assertEqual(day11["metric_ranges"]["scalars"]["undelivered_total"], [1])

        n017_at_9 = ca.serial_summaries(self.packet, 9)["N-017"]
        n017_at_12 = ca.serial_summaries(self.packet, 12)["N-017"]
        self.assertNotIn("O1", n017_at_9["fulfilled_orders"])
        self.assertIn("O1", n017_at_12["fulfilled_orders"])

    def test_issued_request_is_visible_before_late_receipt(self):
        statuses = {x["request"]: x for x in ca.pending_replies(self.packet, 7)}
        self.assertEqual(statuses["RQ1"]["status"], "receipt_window_open")
        self.assertEqual(statuses["RQ3"]["status"], "receipt_window_open")

        current = {x["request"]: x for x in ca.pending_replies(self.packet, 12)}
        self.assertEqual(current["RQ1"]["status"], "confirmed")
        self.assertEqual(current["RQ4"]["status"], "confirmed")
        self.assertEqual(current["RQ3"]["status"], "expired_without_confirmation")
        self.assertTrue(current["RQ3"]["waiting_notice"])

    def test_historical_shift_priorities(self):
        mon = ca.voucher_priority(self.packet, 6, "SH1", predecision=True)
        self.assertEqual(mon["ranked"][0]["request"], "RQ1")

        tue = ca.voucher_priority(self.packet, 7, "SH2", predecision=True)
        eligible = [x["request"] for x in tue["ranked"] if x["action"] == "issue_candidate_request"]
        self.assertEqual(eligible, ["RQ3"])

        wed = ca.voucher_priority(self.packet, 8, "SH3", predecision=True)
        self.assertEqual(wed["ranked"][0]["request"], "RQ4")

        thu = ca.voucher_priority(self.packet, 9, "SH4")
        rq5 = next(x for x in thu["all"] if x["request"] == "RQ5")
        self.assertEqual(rq5["action"], "wait_for_other_open_request")
        self.assertEqual(rq5["marginal_unresolved_targets"], [])

    def test_overlapping_vouchers_use_union_not_additive_release(self):
        rq1_rq2 = ca.request_set_value(self.packet, 6, ["RQ1", "RQ2"])
        self.assertEqual(rq1_rq2["union_target_count"], 2)
        self.assertEqual(rq1_rq2["union_saleable_event_units"], 1)
        self.assertEqual(rq1_rq2["commitment_units"], 1)
        self.assertTrue(rq1_rq2["overlaps"])

        rq3_rq5 = ca.request_set_value(self.packet, 9, ["RQ3", "RQ5"])
        self.assertEqual(rq3_rq5["union_targets"], ["E-U17", "E-U19"])
        self.assertEqual(rq3_rq5["union_saleable_event_units"], 1)
        self.assertEqual(rq3_rq5["overlaps"][0]["shared_targets"], ["E-U19"])

    def test_current_backup_voucher_and_scenario_conflict(self):
        priority = ca.voucher_priority(self.packet, 12)
        rq5 = next(x for x in priority["ranked"] if x["request"] == "RQ5")
        self.assertEqual(rq5["action"], "issue_candidate_request")
        self.assertEqual(rq5["marginal_unresolved_targets"], ["E-U19"])

        scenario = ca.request_scenarios(self.packet, 12, "RQ3")
        by_assignment = {
            tuple(o["reply_assignment"].items()): o["direct_reply_creates_hard_conflict"]
            for o in scenario["outcomes"]
        }
        self.assertFalse(by_assignment[(("E-U19", "N-018"),)])
        self.assertTrue(by_assignment[(("E-U19", "N-019"),)])

    def test_review_samples_are_reported_separately(self):
        board = ca.review_board(self.packet, 12)
        self.assertEqual(board["basis_outcomes"]["clue"], {"conflict": 6})
        self.assertEqual(board["basis_outcomes"]["rotation"], {"legit": 19, "conflict": 3})
        self.assertEqual(board["counts"]["reviewed"], 28)
        self.assertEqual(board["counts"]["window_expired_unreviewed"], 38)

    def test_suggestion_sla_and_false_hold_limit(self):
        policy = ca.policy_board(self.packet, 12)
        by_serial = {x["serial"]: x for x in policy["suggestion_sla"]}
        self.assertEqual(by_serial["N-017"]["status"], "sla_expired_without_review")
        self.assertEqual(by_serial["N-017"]["sla_due_day"], 7)
        self.assertEqual(by_serial["N-014"]["status"], "source_closed")
        self.assertFalse(policy["false_hold_rate_computable"])

    def test_master_case_keeps_requests_and_reviews_together(self):
        cases = ca.case_board(self.packet, 12)
        k7 = next(case for case in cases if case["case_id"] == "E-U17")
        self.assertEqual(k7["event_keys"], ["E-U17", "E-U18", "E-U19"])
        self.assertEqual(k7["requests"], ["RQ1", "RQ2", "RQ3", "RQ5"])
        self.assertIn("O1", k7["commitments"])
        self.assertTrue(any(r["outcome"] == "legit" for r in k7["reviews"]))

        direction = {x["serial"]: x for x in ca.direction_board(self.packet, 12)}
        self.assertTrue(direction["N-036"]["backlog_confirmed_across_worlds"])
        self.assertEqual(direction["N-035"]["recommended_action"], "immediate_joint_case_decision")


if __name__ == "__main__":
    unittest.main()
