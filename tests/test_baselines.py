import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


class ScoringBaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "baseline.db"
        self.service = ProcurementService(self.db_path)
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-101", "启明科技", "v1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-102", "远山系统", "v2")
        self.criteria = [
            {"name": "报价", "weight": 60, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100},
        ]
        self.tender = self.service.create_tender(
            "proc1", "procurement", "T-B1", "评分基准项目",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), self.criteria,
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])
        self.bid1 = self.service.submit_bid("v1", "vendor", self.tender["id"], self.vendor1["id"],
                                            {"报价": 800000, "质量": 90}, 800000)
        self.bid2 = self.service.submit_bid("v2", "vendor", self.tender["id"], self.vendor2["id"],
                                            {"报价": 700000, "质量": 80}, 700000)
        time.sleep(2.1)
        self.opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def confirm(self):
        return self.service.confirm_scoring_baseline(
            "sup1", "supervisor", self.tender["id"], self.opened["tender"]["version"]
        )["baseline"]

    def score_all(self, evaluator):
        self.service.evaluate_bid(evaluator, "evaluator", self.bid1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid(evaluator, "evaluator", self.bid2["id"], {"报价": 700000, "质量": 80})

    def test_confirm_only_by_supervisor_after_open_and_single_active(self):
        with self.assertRaises(DomainError) as ctx:
            self.service.confirm_scoring_baseline("proc1", "procurement", self.tender["id"],
                                                  self.opened["tender"]["version"])
        self.assertEqual(403, ctx.exception.status)
        baseline = self.confirm()
        self.assertEqual("active", baseline["status"])
        self.assertEqual(self.criteria, baseline["criteria"])
        # 同一项目只能有一个生效基准
        with self.assertRaises(DomainError) as ctx2:
            self.service.confirm_scoring_baseline("sup1", "supervisor", self.tender["id"],
                                                  baseline["version"] + 1)
        self.assertEqual(409, ctx2.exception.status)
        # 未确认基准前不能评分（此处已确认，换项目阶段语义由其它用例覆盖），确认后可以
        result = self.service.evaluate_bid("eval1", "evaluator", self.bid1["id"],
                                           {"报价": 800000, "质量": 90})
        self.assertEqual(baseline["id"], result["baseline_id"])

    def _pending_revision(self):
        """共享前置：确认基准、打完一轮旧分、提交一条待生效修订。"""
        baseline = self.confirm()
        self.score_all("eval1")
        new_criteria = [
            {"name": "报价", "weight": 30, "kind": "cost", "max_value": 1000000},
            {"name": "质量", "weight": 70, "kind": "direct", "max_value": 100},
        ]
        rev = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"], new_criteria, "强化质量权重"
        )["revision"]
        return baseline, rev, new_criteria

    def test_revision_is_pending_until_review_and_does_not_mutate_active(self):
        baseline, rev, _new_criteria = self._pending_revision()
        self.assertEqual("pending", rev["status"])
        self.assertEqual(baseline["id"], rev["baseline_id"])
        # 生效基准与既有排名不变，仍按旧口径
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual(baseline["id"], detail["baselines"]["active"]["id"])
        self.assertAlmostEqual(detail["current_ranking"][0]["score"], 96.0, places=1)
        # 只允许一条待生效修订
        with self.assertRaises(DomainError):
            self.service.propose_baseline_revision(
                "proc1", "procurement", self.tender["id"],
                [{"name": "报价", "weight": 50, "kind": "cost", "max_value": 1000000},
                 {"name": "质量", "weight": 50, "kind": "direct", "max_value": 100}],
                "又一次调整",
            )
        # 授标被待生效修订阻断
        codes = {b["code"] for b in detail["award_blockers"]}
        self.assertIn("pending_revision", codes)

    def test_approve_revision_switches_baseline_and_old_scores_need_rescoring(self):
        baseline, rev, _new_criteria = self._pending_revision()
        current_version = self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]["version"]
        applied = self.service.review_baseline_revision(
            "sup1", "supervisor", rev["id"], "approved", baseline["version"], note="同意调整"
        )
        self.assertEqual(
            current_version + 1,
            self.service.get_tender("sup1", "supervisor", self.tender["id"])["tender"]["version"],
        )
        new_baseline = applied["baseline"]
        self.assertEqual("active", new_baseline["status"])
        self.assertEqual(rev["id"], applied["revision"]["id"])
        self.assertEqual("applied", applied["revision"]["status"])
        # 旧基准保留为 superseded
        old = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        statuses = {b["id"]: b["status"] for b in old["baselines"]["history"]}
        self.assertEqual("superseded", statuses[baseline["id"]])
        self.assertEqual("active", statuses[new_baseline["id"]])
        # 旧评分按当时基准回读
        grouped = old["scores_by_baseline"]
        self.assertTrue({row["baseline_id"] for row in grouped} == {baseline["id"]})
        # 新基准下尚无评分，授标阻断并给出重评原因
        self.score_all("eval2")  # 另一评审人按新生效基准重评
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        baseline_rows = {b["id"]: b for b in detail["baselines"]["history"]}
        new_id = [i for i, b in baseline_rows.items() if b["status"] == "active"][0]
        codes = {b["code"] for b in detail["award_blockers"]}
        self.assertNotIn("stale_baseline_scores", codes)
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"],
                                          detail["tender"]["version"])
        # 质量权重提高后，高分质量的 vendor1 仍应第一
        self.assertEqual(award["award"]["baseline_id"], new_id)
        self.assertEqual(self.bid1["id"], award["award"]["winner"]["bid_id"])

    def test_late_revision_hits_version_conflict_with_block_reason(self):
        baseline, rev, _ = self._pending_revision()
        with self.assertRaises(DomainError) as ctx:
            self.service.review_baseline_revision(
                "sup1", "supervisor", rev["id"], "approved", baseline["version"] + 99
            )
        self.assertEqual(409, ctx.exception.status)
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        blocked = next(r for r in detail["baselines"]["revisions"] if r["id"] == rev["id"])
        self.assertEqual("blocked", blocked["status"])
        self.assertTrue(blocked["block_reason"])
        # 已阻断的修订不能再次处理
        with self.assertRaises(DomainError) as ctx2:
            self.service.review_baseline_revision("sup1", "supervisor", rev["id"], "approved",
                                                  baseline["version"])
        self.assertEqual(409, ctx2.exception.status)

    def test_rejected_revision_keeps_reviewer_and_note(self):
        baseline = self.confirm()
        rev = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"],
            [{"name": "报价", "weight": 30, "kind": "cost", "max_value": 1000000},
             {"name": "质量", "weight": 70, "kind": "direct", "max_value": 100}],
            "临时改口径",
        )["revision"]
        with self.assertRaises(DomainError):
            self.service.review_baseline_revision("sup1", "supervisor", rev["id"], "rejected",
                                                  baseline["version"])
        result = self.service.review_baseline_revision(
            "sup1", "supervisor", rev["id"], "rejected", baseline["version"], note="开标后不得随意调整"
        )
        self.assertEqual("rejected", result["revision"]["status"])
        detail = self.service.get_tender("auditor1", "auditor", self.tender["id"])
        record = next(r for r in detail["baselines"]["revisions"] if r["id"] == rev["id"])
        self.assertEqual("sup1", record["reviewed_by"])
        self.assertEqual("开标后不得随意调整", record["review_note"])
        self.assertTrue(record["reviewed_at"])

    def test_restart_resumes_pending_and_blocks_stale_revisions(self):
        baseline = self.confirm()
        rev = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"],
            [{"name": "报价", "weight": 30, "kind": "cost", "max_value": 1000000},
             {"name": "质量", "weight": 70, "kind": "direct", "max_value": 100}],
            "中断前提交",
        )["revision"]
        # 服务重启：新实例连同一数据库，未完成修订被接续
        restarted = ProcurementService(self.db_path)
        recovery = restarted.recover_pending_revisions()
        self.assertEqual([rev["id"]], [r["revision_id"] for r in recovery["pending_revisions"]])
        # 恢复后仍可完成复核
        restarted.review_baseline_revision("sup1", "supervisor", rev["id"], "approved", baseline["version"])
        # 构造一条指向已失效基准的悬挂修订，再次重启应自动阻断并保留原因
        with restarted.connect() as conn:
            cur = conn.execute(
                """INSERT INTO baseline_revisions(tender_id,baseline_id,revision_no,criteria,criteria_hash,reason,
                                                  expected_version,proposed_by,proposed_at,status)
                   VALUES(?,?,?,?,?,?,?,?,?,'pending')""",
                (self.tender["id"], baseline["id"], 99,
                 '[{"name":"报价","weight":30.0,"kind":"cost","max_value":1000000.0},'
                 '{"name":"质量","weight":70.0,"kind":"direct","max_value":100.0}]',
                 "x", "晚到悬挂修订", baseline["version"], "proc1",
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            dangling_id = cur.lastrowid
        again = ProcurementService(self.db_path)
        recovery2 = again.recover_pending_revisions()
        self.assertIn(dangling_id, recovery2["blocked_on_recovery"])
        detail = again.get_tender("sup1", "supervisor", self.tender["id"])
        dangling = next(r for r in detail["baselines"]["revisions"] if r["id"] == dangling_id)
        self.assertEqual("blocked", dangling["status"])
        self.assertTrue(dangling["block_reason"])

    def test_award_requires_confirmed_baseline(self):
        # 开标后、基准确认前：评分和授标都被阻断
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", self.bid1["id"],
                                      {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx.exception.status)
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        codes = {b["code"] for b in detail["award_blockers"]}
        self.assertIn("no_baseline", codes)


if __name__ == "__main__":
    unittest.main()
