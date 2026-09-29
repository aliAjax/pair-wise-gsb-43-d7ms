import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, ProcurementService  # noqa: E402


def criteria(cost_weight=60, quality_weight=40):
    return [
        {"name": "报价", "weight": cost_weight, "kind": "cost", "max_value": 1000000},
        {"name": "质量", "weight": quality_weight, "kind": "direct", "max_value": 100},
    ]


class BaselineSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "baseline.db"
        self.service = ProcurementService(self.db_path)
        self.vendor1 = self.service.create_vendor("proc1", "procurement", "V-101", "启明科技", "vendor1")
        self.vendor2 = self.service.create_vendor("proc1", "procurement", "V-102", "远山系统", "vendor2")
        self.tender = self.service.create_tender(
            "proc1", "procurement", "B-001", "数据中心设备",
            (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(), criteria(),
        )
        self.tender = self.service.publish_tender("proc1", "procurement", self.tender["id"], self.tender["version"])

    def tearDown(self):
        self.tmp.cleanup()

    def _open_and_confirm(self):
        time.sleep(2.1)
        opened = self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        confirmed = self.service.confirm_baseline("sup1", "supervisor", self.tender["id"])
        return opened, confirmed

    def _bid(self, vendor, actor, price, quality):
        return self.service.submit_bid(
            actor, "vendor", self.tender["id"], vendor["id"],
            {"报价": price, "质量": quality}, price,
        )

    # 1. 监督员确认后生成不可变快照；无基准不能评分；重复确认被拒
    def test_confirm_gates_and_snapshot_identity(self):
        bid = self._bid(self.vendor1, "vendor1", 800000, 90)
        time.sleep(2.1)
        self.service.open_bids("proc1", "procurement", self.tender["id"], self.tender["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.evaluate_bid("eval1", "evaluator", bid["id"], {"报价": 800000, "质量": 90})
        self.assertEqual(409, ctx.exception.status)
        # 只有监督员能确认
        with self.assertRaises(DomainError) as ctx2:
            self.service.confirm_baseline("proc1", "procurement", self.tender["id"])
        self.assertEqual(403, ctx2.exception.status)
        confirmed = self.service.confirm_baseline("sup1", "supervisor", self.tender["id"])
        self.assertEqual(1, confirmed["baseline"]["version"])
        self.assertEqual("active", confirmed["baseline"]["status"])
        self.assertEqual("sup1", confirmed["baseline"]["confirmed_by"])
        self.assertTrue(confirmed["baseline"]["criteria_hash"])
        with self.assertRaises(DomainError) as ctx3:
            self.service.confirm_baseline("sup2", "supervisor", self.tender["id"])
        self.assertEqual(409, ctx3.exception.status)

    # 2. 调整只形成待生效修订，既有评分和排名不改变；待修订阻断授标并留痕
    def test_revision_stays_pending_and_blocks_award_without_changing_scores(self):
        b1 = self._bid(self.vendor1, "vendor1", 800000, 90)
        b2 = self._bid(self.vendor2, "vendor2", 700000, 80)
        opened, confirmed = self._open_and_confirm()
        self.service.evaluate_bid("eval1", "evaluator", b1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", b2["id"], {"报价": 700000, "质量": 80})
        revision = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"], criteria(50, 50),
            confirmed["baseline"]["version"], "价格竞争充分，提高质量权重",
        )
        self.assertEqual("pending", revision["status"])
        self.assertEqual(1, revision["expected_version"])
        self.assertEqual("proc1", revision["proposed_by"])
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        self.assertEqual(1, detail["active_baseline"]["version"])
        codes = {b["code"] for b in detail["blockers"]}
        self.assertIn("pending_revision", codes)
        with self.assertRaises(DomainError) as ctx:
            self.service.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual(409, ctx.exception.status)
        timeline = self.service.state("sup1", "supervisor")["timeline"]
        blocked = [e for e in timeline if e["action"] == "award.blocked"]
        self.assertTrue(blocked)
        self.assertIn("pending_revision", blocked[0]["details"])
        # 评分仍绑定 v1，分值未被改写
        rounds = detail["evaluation_rounds"]
        self.assertEqual(1, rounds[0]["round"])
        self.assertEqual(1, rounds[0]["groups"][0]["baseline_version"])

    # 3. 晚到修订撞版本冲突：先批准 rev2 到 v2，再批 rev1 报 409 且标记 conflicted
    def test_late_revision_hits_version_conflict(self):
        self._open_and_confirm()
        rev1 = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"], criteria(50, 50), 1, "修订A")
        rev2 = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"], criteria(30, 70), 1, "修订B")
        approved = self.service.review_baseline_revision("sup1", "supervisor", rev2["id"], "approved", "同意")
        self.assertEqual(2, approved["baseline"]["version"])
        # 全项目仍只有一个生效基准
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        actives = [b for b in detail["baselines"] if b["status"] == "active"]
        self.assertEqual(1, len(actives))
        self.assertEqual(2, actives[0]["version"])
        with self.assertRaises(DomainError) as ctx:
            self.service.review_baseline_revision("sup1", "supervisor", rev1["id"], "approved", "补批")
        self.assertEqual(409, ctx.exception.status)
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        stale = next(r for r in detail["revisions"] if r["id"] == rev1["id"])
        self.assertEqual("conflicted", stale["status"])
        self.assertEqual("sup1", stale["reviewed_by"])
        self.assertIn("v1", stale["review_note"] + ctx.exception.args[0])
        # 采购员不能复核
        with self.assertRaises(DomainError) as ctx2:
            self.service.review_baseline_revision("proc1", "procurement", rev1["id"], "approved")
        self.assertEqual(403, ctx2.exception.status)

    # 4. 修订生效进入新一轮：旧评分按旧基准回读，授标只接受生效基准，重评后授标成功
    def test_approval_freezes_old_scores_and_requires_reevaluation(self):
        b1 = self._bid(self.vendor1, "vendor1", 800000, 90)
        b2 = self._bid(self.vendor2, "vendor2", 700000, 80)
        opened, confirmed = self._open_and_confirm()
        self.service.evaluate_bid("eval1", "evaluator", b1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", b2["id"], {"报价": 700000, "质量": 80})
        rev = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"], criteria(50, 50), 1, "权重对半")
        approved = self.service.review_baseline_revision("sup1", "supervisor", rev["id"], "approved", "通过")
        self.assertEqual(2, approved["tender"]["evaluation_round"])
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        # 新一轮未按生效基准评分，授标阻断；旧评分仍以 v1 回读
        self.assertIn("incomplete_evaluation", {b["code"] for b in detail["blockers"]})
        rounds = {r["round"]: r for r in detail["evaluation_rounds"]}
        self.assertEqual(1, rounds[1]["groups"][0]["baseline_version"])
        self.assertEqual("superseded", rounds[1]["groups"][0]["baseline_status"])
        with self.assertRaises(DomainError):
            self.service.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        # 按 v2 重新评分后授标，快照记录生效基准
        self.service.evaluate_bid("eval1", "evaluator", b1["id"], {"报价": 800000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", b2["id"], {"报价": 700000, "质量": 80})
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual(2, award["award"]["baseline_version"])
        self.assertEqual(detail["active_baseline"]["criteria_hash"], award["award"]["criteria_hash"])

    # 5. 口径变化确实改变候选顺序：v1(价格60%) 下低价者胜，v2(质量70%) 下高质量者胜
    def test_ranking_changes_under_new_baseline_after_reevaluation(self):
        # A：高价高质量（130万/90分），B：低价低质量（100万/60分）
        bid_a = self._bid(self.vendor1, "vendor1", 1300000, 90)
        bid_b = self._bid(self.vendor2, "vendor2", 1000000, 60)
        opened, confirmed = self._open_and_confirm()
        self.service.evaluate_bid("eval1", "evaluator", bid_a["id"], {"报价": 1300000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid_b["id"], {"报价": 1000000, "质量": 60})
        with self.service.connect() as conn:
            tender_row = self.service._tender(conn, self.tender["id"])
            baseline_row = self.service._active_baseline(conn, self.tender["id"])
            ranking_v1, blockers_v1 = self.service._compute_ranking(conn, tender_row, baseline_row)
        self.assertEqual([], blockers_v1)
        self.assertEqual(bid_b["id"], ranking_v1[0]["bid_id"])
        rev = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"], criteria(30, 70), 1, "质量优先")
        approved = self.service.review_baseline_revision("sup1", "supervisor", rev["id"], "approved", "通过")
        self.assertEqual(2, approved["baseline"]["version"])
        self.service.evaluate_bid("eval1", "evaluator", bid_a["id"], {"报价": 1300000, "质量": 90})
        self.service.evaluate_bid("eval1", "evaluator", bid_b["id"], {"报价": 1000000, "质量": 60})
        detail = self.service.get_tender("sup1", "supervisor", self.tender["id"])
        award = self.service.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual(2, award["award"]["baseline_version"])
        self.assertEqual(bid_a["id"], award["award"]["winner"]["bid_id"])
        # 授标后不可再修订
        with self.assertRaises(DomainError):
            self.service.propose_baseline_revision(
                "proc1", "procurement", self.tender["id"], criteria(20, 80), 2, "授标后调整")

    # 6. 驳回修订保留复核人和原因；详情页可见修订记录
    def test_rejected_revision_keeps_reviewer_and_reason(self):
        self._open_and_confirm()
        rev = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"], criteria(50, 50), 1, "随意调整")
        rejected = self.service.review_baseline_revision(
            "sup1", "supervisor", rev["id"], "rejected", "开标后无正当理由调整权重")
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("sup1", rejected["reviewed_by"])
        detail = self.service.get_tender("aud1", "auditor", self.tender["id"])
        record = next(r for r in detail["revisions"] if r["id"] == rev["id"])
        self.assertEqual("开标后无正当理由调整权重", record["review_note"])
        # 驳回后无待办，基准仍是 v1
        self.assertEqual([], [b for b in detail["blockers"] if b["code"] == "pending_revision"])
        self.assertEqual(1, detail["active_baseline"]["version"])

    # 7. 服务中断重启：待生效修订持久化，新实例可继续复核
    def test_pending_revision_survives_restart(self):
        b1 = self._bid(self.vendor1, "vendor1", 800000, 90)
        opened, confirmed = self._open_and_confirm()
        self.service.evaluate_bid("eval1", "evaluator", b1["id"], {"报价": 800000, "质量": 90})
        rev = self.service.propose_baseline_revision(
            "proc1", "procurement", self.tender["id"], criteria(50, 50), 1, "重启场景")
        restarted = ProcurementService(self.db_path)
        recovered = restarted.recover_pending()
        self.assertEqual(1, recovered["count"])
        self.assertEqual(rev["id"], recovered["pending"][0]["id"])
        # 用新实例接着办完复核与授标
        approved = restarted.review_baseline_revision("sup1", "supervisor", rev["id"], "approved", "重启后复核")
        self.assertEqual(2, approved["baseline"]["version"])
        self.service.evaluate_bid("eval1", "evaluator", b1["id"], {"报价": 800000, "质量": 90})
        detail = restarted.get_tender("sup1", "supervisor", self.tender["id"])
        award = restarted.award_tender("sup1", "supervisor", self.tender["id"], detail["tender"]["version"])
        self.assertEqual(2, award["award"]["baseline_version"])

    # 8. 修订参数校验：版本号不匹配 409；内容与生效基准一致拒绝；权重不合法拒绝
    def test_revision_validation(self):
        self._open_and_confirm()
        with self.assertRaises(DomainError) as ctx:
            self.service.propose_baseline_revision(
                "proc1", "procurement", self.tender["id"], criteria(50, 50), 2, "基于错误版本")
        self.assertEqual(409, ctx.exception.status)
        with self.assertRaises(DomainError) as ctx2:
            self.service.propose_baseline_revision(
                "proc1", "procurement", self.tender["id"], criteria(60, 40), 1, "没有变化")
        self.assertEqual(400, ctx2.exception.status)
        with self.assertRaises(DomainError):
            self.service.propose_baseline_revision(
                "proc1", "procurement", self.tender["id"], criteria(70, 40), 1, "权重不等于100")


if __name__ == "__main__":
    unittest.main()
