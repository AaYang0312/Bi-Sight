"""Task 4：恢复决策必须按原因码确定，不按文案猜测。"""

import unittest
from datetime import date


class RecoveryDecisionTests(unittest.TestCase):
    def _coverage(self, *, status="partial", suggested=None):
        from bi_agent.metrics import Coverage

        return Coverage(status=status, start=date(2026, 9, 4), end=date(2026, 9, 11),
                        gaps=(["2026-09-09~2026-09-11"] if status != "complete" else []),
                        suggested_window=suggested)

    def _error(self, code):
        from bi_agent.runtime.models import ErrorEnvelope

        return ErrorEnvelope(code=code, stage="execute_fixed_query", retryable=False,
                             recovery="ask_user", public_message="查询暂不可用。")

    # -- 表内每一行都要有自己的断言 -----------------------------------------

    def test_missing_parameters_asks_and_runs_no_sql(self):
        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(self._error("missing_parameters"), None, None, 25.0)

        self.assertEqual((decision.action, decision.max_additional_attempts),
                         ("ask_user", 0))

    def test_invalid_parameters_allows_exactly_one_correction(self):
        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(self._error("invalid_parameters"), None, None, 25.0)

        self.assertEqual(decision.action, "correct_parameters")
        self.assertEqual(decision.max_additional_attempts, 1)

    def test_invalid_parameters_without_budget_terminates(self):
        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(self._error("invalid_parameters"), None, None, 0.5)

        self.assertEqual(decision.action, "terminate")
        self.assertEqual(decision.max_additional_attempts, 0)

    def test_second_correction_is_refused(self):
        """整轮最多一次参数修正：已修正过就不再追加。"""
        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(self._error("invalid_parameters"), None, None, 25.0,
                                   recovery_count=1)

        self.assertEqual((decision.action, decision.max_additional_attempts),
                         ("terminate", 0))

    def test_coverage_gap_never_repeats_the_original_query(self):
        """重复缺覆盖不得耗尽工具次数，也不能给用户换一个窗口。"""
        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(
            None, self._coverage(suggested=("2026-09-04", "2026-09-09")), None, 20.0,
            reason_code="coverage_incomplete")

        self.assertEqual(decision.action, "report_gap")
        self.assertEqual(decision.max_additional_attempts, 0)
        self.assertEqual(decision.suggested_window, ("2026-09-04", "2026-09-09"),
                         "建议只作为建议随决策返回")

    def test_unknown_source_and_failed_quality_report_gap_without_retry(self):
        from bi_agent.business_query.recovery import decide_recovery

        for reason in ("source_not_onboarded", "source_quality_failed",
                       "data_as_of_unknown"):
            with self.subTest(reason):
                decision = decide_recovery(None, self._coverage(), None, 20.0,
                                           reason_code=reason)
                self.assertEqual((decision.action, decision.max_additional_attempts),
                                 ("report_gap", 0))

    def test_forbidden_terminates(self):
        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(self._error("forbidden"), None, None, 25.0)

        self.assertEqual((decision.action, decision.max_additional_attempts),
                         ("terminate", 0))

    def test_deadline_and_timeout_terminate(self):
        from bi_agent.business_query.recovery import decide_recovery

        for code in ("deadline_exceeded", "query_timeout"):
            with self.subTest(code):
                decision = decide_recovery(None, None, None, 0.1, reason_code=code)
                self.assertEqual((decision.action, decision.max_additional_attempts),
                                 ("terminate", 0))

    def test_transient_failure_retries_once_only_with_budget(self):
        from bi_agent.business_query.recovery import decide_recovery

        enough = decide_recovery(None, None, None, 20.0,
                                 reason_code="transient_source_failure")
        tight = decide_recovery(None, None, None, 1.0,
                                reason_code="transient_source_failure")
        second = decide_recovery(None, None, None, 20.0,
                                 reason_code="transient_source_failure",
                                 recovery_count=1)

        self.assertEqual((enough.action, enough.max_additional_attempts),
                         ("retry_transient", 1))
        self.assertEqual(tight.action, "terminate")
        self.assertEqual(second.action, "terminate", "临时故障整轮最多重试一次")

    def test_unavailable_is_never_guessed_into_a_retry(self):
        """未知原因保持终止：猜错误类型再重试会把预算烧在同一个故障上。"""
        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(self._error("unavailable"), None, None, 25.0)

        self.assertEqual((decision.action, decision.max_additional_attempts),
                         ("terminate", 0))

    def test_persistence_failure_fails_closed(self):
        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(self._error("artifact_persistence_failed"),
                                   None, None, 25.0)

        self.assertEqual(decision.action, "fail_closed")
        self.assertFalse(decision.consumes_tool_budget)

    def test_same_fingerprint_same_data_version_reuses_result(self):
        from uuid import uuid4

        from bi_agent.business_query.recovery import decide_recovery

        decision = decide_recovery(None, None, None, 25.0,
                                   reusable_artifact_ref=uuid4())

        self.assertEqual(decision.action, "reuse_result")
        self.assertEqual(decision.max_additional_attempts, 0, "复用不再跑新 SQL")

    def test_decision_carries_no_substitute_request(self):
        """决策不许自带一个被改短的用户窗口。"""
        from dataclasses import fields

        from bi_agent.business_query.recovery import RecoveryDecision

        names = {field.name for field in fields(RecoveryDecision)}

        self.assertEqual(names, {"action", "reason_code", "max_additional_attempts",
                                 "suggested_window"})
        for forbidden in ("request", "rewritten_window", "window", "start", "end"):
            self.assertNotIn(forbidden, names, "决策不得自带替代请求")


class ResponseSummaryTests(unittest.TestCase):
    """兜底摘要只能复述已存结果，不能重新算钱。"""

    def _result(self, **overrides):
        from uuid import uuid4

        from bi_agent.metrics import METRIC_DEFINITIONS, Coverage
        from bi_agent.runtime.models import DomainResult

        base = dict(
            run_id=uuid4(), status="success",
            model_payload={
                "status": "ok",
                "metric_definition": {"paid_amount": METRIC_DEFINITIONS["paid_amount"]},
                "coverage": {"status": "complete", "start": "2026-09-01",
                             "end": "2026-09-10", "gaps": []},
                "limitations": ["来源质量未核验（尚无对账记录）"],
                "data_as_of": "2026-09-09T19:39:35+08:00",
                "filters": {"start": "2026-09-01", "end": "2026-09-10",
                            "shop_refs": ["ent-689af83c"], "metrics": ["paid_amount"]},
                "data": [{"line_kind": "sale", "paid_amount": "44010.160000",
                          "shop_ref": "ent-689af83c"}],
            },
            coverage=Coverage(status="complete", start=date(2026, 9, 1),
                              end=date(2026, 9, 10), gaps=[]),
        )
        base.update(overrides)
        return DomainResult(**base)

    def test_summary_states_metrics_window_cutoff_and_limits(self):
        from bi_agent.response_summary import render_result_summary

        text = render_result_summary(self._result())

        self.assertIn("paid_amount", text)
        self.assertIn("2026-09-01", text)
        self.assertIn("2026-09-10", text)
        self.assertIn("44010.16", text)
        self.assertIn("未核验", text)

    def test_summary_does_not_recompute_or_aggregate(self):
        """摘要里的金额必须原样来自已存结果，不做二次汇总。"""
        import re

        from bi_agent.response_summary import render_result_summary

        text = render_result_summary(self._result())

        self.assertNotIn("合计", text)
        self.assertEqual(len(re.findall(r"\d+\.\d{2}", text)), 1,
                         "不得出现重新算出来的第二个金额")

    def test_agent_falls_back_to_summary_when_model_gives_no_text(self):
        """兜底链：模型补答失败时必须用已存 Artifact 复述，而不是放弃回答。"""
        from bi_agent.agent import _deterministic_summary

        outcome = type("Outcome", (), {"domain_result": self._result()})()

        text = _deterministic_summary([outcome])

        self.assertIsNotNone(text)
        self.assertIn("paid_amount", text)
        self.assertIn("44010.16", text)
        self.assertIsNone(_deterministic_summary([]), "没有结果时不许编一段话")

    def test_missing_result_says_so_instead_of_inventing_text(self):
        from bi_agent.response_summary import render_result_summary

        text = render_result_summary(None)

        self.assertTrue(text.strip())
        self.assertIn("未能生成", text)


if __name__ == "__main__":
    unittest.main()
