"""Bounded risk-gate regressions; no providers or operational store access."""
import pytest

from test_routing import plugin, settings


LOW_RISK = [
    "운영 관련 질문이야. fast 모드가 뭔지 설명해줘",
    "운영 관점에서 fast 모드와 일반 모드의 차이를 설명해줘",
    "운영 로그를 읽고 현재 상태만 알려줘",
    "Read production logs and summarize the current status.",
    "Check prod health status (read-only).",
    "Explain the production monitoring dashboard.",
    "Explain the production application status.",
    "Read production startup logs.",
    "Look up the installed version.",
    "지원하는 작업 목록만 조회해줘",
    "Summarize these local notes and update a draft.",
    "로컬 코드 수정 후 테스트를 실행해줘",
    "Implement a local parser and run its unit tests.",
    "Research two libraries and compare them in a local draft.",
]
HIGH_RISK = [
    "Change production configuration.",
    "Update prod records after reading the logs.",
    "Summarize production logs, then restart the service.",
    "운영 DB의 예약 데이터를 수정해줘",
    "운영 환경 설정을 바꿔줘",
    "운영 서버를 재시작해줘",
    "운영 로그를 확인하고 설정을 변경해줘",
    "프로덕션에 적용해줘",
    "Handle the deployments.",
    "Publish this local draft.",
    "Post the response to Slack.",
    "Send this brief email.",
    "Write the approved update to the external system.",
    "외부 시스템에 변경 사항을 기록해줘.",
    "고객에게 메시지를 전송해줘",
    "Transfer funds.",
    "Complete the accounting close.",
    "회계 정산 결과를 검토해줘",
    "Review this legal decision.",
    "Summarize the legal liability of this contract.",
    "법률 검토를 해줘",
    "Perform a local security change and run unit tests.",
    "보안 정책을 수정해줘",
    "Change the team permissions.",
    "Read the access control policy and advise on permissions.",
    "Perform this irreversible action.",
    "This ambiguous task has high error cost.",
    "Narrow the routing policy and verifier triggers in the local plugin.",
    "위임·검증 기준을 좁히는 것을 반영해줘",
    # Operational requests without a clearly read-only scope stay conservative.
    "Take care of production.",
    "운영 작업을 처리해줘",
    "Local analysis of a production intervention.",
    "Read production logs.\nThen delete stale records.",
    "운영 로그 조회 후 DB 레코드를 삭제해줘",
    "Explain the production status and enable the feature.",
    "Inspect production and rotate the credentials.",
    # Negation/quotation is deliberately not treated as a safety bypass.
    "Explain production changes; do not deploy anything.",
    "Scale production replicas after checking status.",
    "Read production status and roll back the service.",
    "운영 로그를 읽고 기능을 켜줘",
]


def plan(mode):
    worker = {"label": "work", "phase": "worker", "work_type": "implementation", "model": "claude-opus-5",
              "reasoning_effort": "high", "toolsets": ["file"]}
    verifier = {**worker, "label": "review", "phase": "verifier",
                "work_type": "verification", "model": "gpt-6.1-sol"}
    lanes = {"direct": [], "single": [worker],
             "parallel": [worker, {**worker, "label": "independent"}],
             "worker_verifier": [worker, verifier]}[mode]
    return {"mode": mode, "reason": "test boundary", "lanes": lanes}


def validate(message, mode):
    return plugin._validate_plan(
        plan(mode), allowed_models=settings("allowed_models"),
        allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"),
        require_worker_verifier=plugin._worker_verifier_requirement(message),
    )


@pytest.mark.parametrize("message", LOW_RISK)
def test_routine_requests_do_not_require_or_allow_a_second_verifier(message):
    assert plugin._worker_verifier_requirement(message) is False
    for mode in ("direct", "single", "parallel"):
        assert validate(message, mode)["mode"] == mode
    with pytest.raises(ValueError, match="reserved for high-risk"):
        validate(message, "worker_verifier")


@pytest.mark.parametrize("message", HIGH_RISK)
def test_risky_requests_cannot_hide_behind_local_read_or_test_words(message):
    assert plugin._worker_verifier_requirement(message) is True
    for mode in ("direct", "single", "parallel"):
        with pytest.raises(ValueError, match="required for this high-risk"):
            validate(message, mode)
    assert validate(message, "worker_verifier")["stage"] == "pending_workers"


@pytest.mark.parametrize("message", [
    "Handle this unusual sensitive change carefully.",
    "Review this unfamiliar sensitive matter carefully.",
    "Inspect the records, then modify them.",
])
def test_ambiguous_operational_scope_does_not_forbid_conservative_verification(message):
    assert plugin._worker_verifier_requirement(message) is None
    assert validate(message, "worker_verifier")["mode"] == "worker_verifier"


@pytest.mark.parametrize("message,expected", [
    (LOW_RISK[0], False), (HIGH_RISK[3], True),
])
def test_parent_turn_gate_uses_current_request_classification(monkeypatch, message, expected):
    monkeypatch.setattr(plugin, "_ENFORCED_PLATFORMS", {"slack"})
    monkeypatch.setattr(plugin, "_TURN_RISK_REQUIREMENTS", {})
    context = dict(session_id="risk-test", turn_id="turn", task_id="parent-task", platform="slack")
    plugin._pre_llm_policy(user_message=message, **context)
    key = plugin._turn_key("risk-test", "turn")
    assert plugin._TURN_RISK_REQUIREMENTS[key] is expected


def test_model_guidance_keeps_tests_in_same_lane_and_risk_distinct_from_complexity():
    desc = plugin.ROUTE_TURN_SCHEMA["description"]
    assert "tests in the same worker lane" in desc
    assert "not implementation plus its tests" in desc
    assert "read-only production status" in desc
    assert "Mere mentions of operations" in desc
    assert "routing/verification policy changes" in desc
