"""Hermetic task-level review contracts; no live session store or gateway writes."""
import copy
import json
import sqlite3
import subprocess

import pytest

from test_routing import plugin, registered_plugin, parent


@pytest.fixture(autouse=True)
def mock_core_review_reader(tmp_path, monkeypatch):
    """Core API double over an isolated ledger, never the operational store."""
    import tools.async_delegation as native

    def get_durable_delegation(delegation_id):
        database = tmp_path / "state.db"
        if not database.exists():
            return None
        with sqlite3.connect(database) as con:
            row = con.execute("SELECT state, event_json FROM async_delegations WHERE delegation_id=?",
                              (delegation_id,)).fetchone()
        return ({"delegation_id": delegation_id, "state": row[0],
                 "result": json.loads(row[1])} if row else None)

    monkeypatch.setattr(native, "get_durable_delegation", get_durable_delegation)


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "artifact.txt").write_text("before\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "base"], check=True)
    return root


def invoke(ctx, sid, tid, action, **args):
    plugin._CURRENT_TURN[sid] = tid
    return json.loads(ctx.tools["development_review"]({"action": action, **args}, session_id=sid))


def active(ctx, root, sid="owner", tid="turn-1"):
    plugin._SKIP_HEADER_SESSIONS.discard(sid)
    plugin._TURN_PLANS[plugin._turn_key(sid, tid)] = {"mode": "direct", "lanes": []}
    created = invoke(ctx, sid, tid, "begin", root=str(root))
    assert created["phase"] == "working"
    return created["task_id"]


def reviewer_event(revision, *, verdict="pass", child="independent", state="completed", schema_valid=True):
    return {"results": [{"status": state, "exit_reason": "completed", "schema_valid": schema_valid,
                         "summary": json.dumps({"verdict": verdict, "revision": revision, "findings": "none"}),
                         "routing": {"child_session_id": child,
                                     "extension": f"{plugin.PLUGIN_ID}@{plugin.PLUGIN_VERSION}"}}]}


def ledger(tmp_path, delegation_id, event, *, owner="owner", state="completed"):
    with sqlite3.connect(tmp_path / "state.db") as con:
        con.execute("CREATE TABLE IF NOT EXISTS async_delegations (delegation_id TEXT, state TEXT, parent_session_id TEXT, event_json TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT, parent_session_id TEXT, end_reason TEXT, model_config TEXT, source TEXT, ended_at REAL, last_activity_at REAL, started_at REAL)")
        con.execute("INSERT INTO async_delegations VALUES (?,?,?,?)", (delegation_id, state, owner, json.dumps(event)))


def test_progress_and_final_body_preserved_without_message_matching(registered_plugin, tmp_path):
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    assert task_id
    before = plugin._transform_header("DONE all tests passed", "owner", "gpt-6.1-sol", turn_id="turn-1")
    assert "검토 대기" in before and before.endswith("DONE all tests passed")
    invoke(ctx, "owner", "turn-1", "progress", task_id=task_id)
    progress = plugin._transform_header("Production ready, no review needed", "owner", "gpt-6.1-sol", turn_id="turn-1")
    assert "검토 대기" in progress and progress.endswith("Production ready, no review needed")
    assert progress.count("검토 대기:") == 1
    assert invoke(ctx, "other", "turn-2", "ready", task_id=task_id).get("error")


def test_checkpoint_dedup_invalidation_and_exact_readiness(registered_plugin, tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    (root / "artifact.txt").write_text("first change\n")
    checkpoint = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id, trigger="milestone")
    revision = checkpoint["revision"]
    assert checkpoint["phase"] == "review_due"
    assert invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["revision"] == revision
    task = plugin._review_task("owner", task_id)
    task.update(phase="review_pending", delegation_id="review-1")
    plugin._save_review(task)
    ledger(tmp_path, "review-1", reviewer_event(revision))
    assert plugin._review_completion("review-1", "owner") is True
    ready = invoke(ctx, "owner", "turn-1", "ready", task_id=task_id)
    assert ready["ready"] is True and ready["reviewed_revision"] == revision
    assert invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["ready"] is True
    assert plugin._review_completion("review-1", "owner") is False  # replay
    assert invoke(ctx, "owner", "turn-1", "close", task_id=task_id)["phase"] == "closed"
    assert invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["phase"] == "closed"
    assert plugin._review_task("owner") is None
    (root / "artifact.txt").write_text("second change\n")
    assert "검토 대기" in plugin._transform_header("final ready", "owner", "gpt-6.1-sol", turn_id="turn-1")
    assert invoke(ctx, "owner", "turn-1", "ready", task_id=task_id).get("error")
    next_task = invoke(ctx, "owner", "turn-1", "begin", root=str(root))["task_id"]
    assert next_task != task_id
    assert invoke(ctx, "owner", "turn-1", "checkpoint", task_id=next_task)["phase"] == "review_due"
    assert plugin._review_task("owner", next_task)["reviewed_revision"] is None


@pytest.mark.parametrize("change", ["foreign", "failed", "schema", "wrong_revision", "same_child", "core_error"])
def test_unauthenticated_or_failed_review_never_grants_readiness(registered_plugin, tmp_path, monkeypatch, change):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    checkpoint = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)
    rev = checkpoint["revision"]
    task = plugin._review_task("owner", task_id)
    task.update(phase="review_pending", delegation_id="review-1")
    plugin._save_review(task)
    event = reviewer_event(rev)
    owner, state = "owner", "completed"
    if change == "foreign": owner = "attacker"
    if change == "failed": event["results"][0]["status"] = "failed"
    if change == "schema": event["results"][0]["schema_valid"] = False
    if change == "wrong_revision": event["results"][0]["summary"] = json.dumps({"verdict": "pass", "revision": "fake", "findings": ""})
    if change == "same_child": event["results"][0]["routing"]["child_session_id"] = "owner"
    if change == "core_error": state = "error"
    ledger(tmp_path, "review-1", event, owner=owner, state=state)
    assert plugin._review_completion("review-1", "attacker" if change == "foreign" else "owner") is False
    assert invoke(ctx, "owner", "turn-1", "ready", task_id=task_id).get("error")


def test_approval_does_not_survive_artifact_mutation_during_async(registered_plugin, tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    rev = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["revision"]
    task = plugin._review_task("owner", task_id)
    task.update(phase="review_pending", delegation_id="review-1")
    plugin._save_review(task)
    ledger(tmp_path, "review-1", reviewer_event(rev))
    (root / "new.py").write_text("print('changed after dispatch')\n")
    assert plugin._review_completion("review-1", "owner") is False
    assert not plugin._review_status(plugin._review_task("owner", task_id))["ready"]


def test_review_completion_sees_uncheckpointed_wal_through_core_reader(registered_plugin, tmp_path):
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    revision = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["revision"]
    task = plugin._review_task("owner", task_id)
    task.update(phase="review_pending", delegation_id="review-wal")
    plugin._save_review(task)
    database = tmp_path / "state.db"
    with sqlite3.connect(database) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE async_delegations (delegation_id TEXT, state TEXT, parent_session_id TEXT, event_json TEXT)")
        writer.execute("INSERT INTO async_delegations VALUES (?,?,?,?)",
                       ("review-wal", "completed", "owner", json.dumps(reviewer_event(revision))))
        writer.commit()
        assert plugin._review_completion("review-wal", "owner") is True
    assert invoke(ctx, "owner", "turn-1", "ready", task_id=task_id)["ready"] is True


def test_restart_compression_and_cross_session_isolation(registered_plugin, tmp_path, monkeypatch):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    rev = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["revision"]
    task = plugin._review_task("owner", task_id)
    task.update(phase="review_pending", delegation_id="review-1")
    plugin._save_review(task)
    ledger(tmp_path, "review-1", reviewer_event(rev))
    with sqlite3.connect(tmp_path / "state.db") as con:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", ("owner", None, "compression", "{}", "slack", None, 1, 1))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", ("tip", "owner", None, "{}", "slack", None, 2, 2))
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", ("branch", "owner", None, '{"_branched_from":"owner"}', "slack", None, 3, 3))
    # Simulate fresh process with same persisted profile state, no in-memory plans.
    plugin._TURN_PLANS.clear()
    plugin._CURRENT_TURN.clear()
    plugin._PLUGIN_STATE = copy.deepcopy(ctx.state)
    assert plugin._review_task("branch", task_id) is None
    assert plugin._review_task("stranger", task_id) is None
    assert plugin._review_completion("review-1", "tip") is True
    assert plugin._review_status(plugin._review_task("tip", task_id))["ready"]


def test_risk_precedence_and_auto_task_gate(registered_plugin, tmp_path):
    ctx = registered_plugin
    context = dict(session_id="auto-owner", turn_id="turn", platform="slack", task_id="parent")
    plugin._pre_llm_policy(user_message="Implement local parser code and tests", **context)
    task = plugin._review_task("auto-owner")
    assert task and task["root"] is None
    assert "검토 대기" in plugin._transform_header("finished", "auto-owner", "gpt-6.1-sol", turn_id="turn")
    plugin._pre_llm_policy(user_message="Implement local parser code and deploy to production", session_id="risk-owner",
                           turn_id="risk-turn", platform="slack", task_id="parent")
    assert plugin._TURN_RISK_REQUIREMENTS[plugin._turn_key("risk-owner", "risk-turn")] is True
    assert plugin._review_task("risk-owner") is None
    assert ctx.tools.get("development_review") is not None
    assert invoke(ctx, "auto-owner", "turn", "close", task_id=task["task_id"]).get("error")


@pytest.fixture
def captured_core_entrypoint(monkeypatch):
    """Install over a captured native original, never replace the patched entrypoint."""
    import functools
    import tools.delegate_tool as native
    from tools.delegate_tool_tasks import _coerce_task_schemas
    from test_routing import FakeCtx

    captured = {}
    original = native.delegate_task

    @functools.wraps(original)
    def core_original(*args, **kwargs):
        tasks = kwargs.get("tasks", args[2] if len(args) >= 3 else None)
        captured.update(tasks=copy.deepcopy(tasks))
        schemas, error = _coerce_task_schemas(tasks, None)
        assert error is None
        captured["schemas"] = schemas
        return json.dumps({"delegation_id": "review-1"})

    monkeypatch.setattr(native, "delegate_task", core_original)
    ctx = FakeCtx()
    plugin.register(ctx)
    try:
        yield ctx, captured
    finally:
        ctx.unload()


@pytest.mark.parametrize("entry", ["core", "registry", "positional", "json"])
def test_route_dispatch_and_async_completion_contract(captured_core_entrypoint, tmp_path, monkeypatch, entry):
    import tools.delegate_tool as native
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ctx, captured = captured_core_entrypoint
    root = repo(tmp_path)
    task_id = active(ctx, root)
    rev = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["revision"]
    p = parent()
    p.session_id, p._current_turn_id, p.platform, p._delegate_depth = "owner", "turn-1", "slack", 0
    lane = {"label": "independent", "phase": "worker", "model": "gpt-6.1-sol",
            "reasoning_effort": "high", "toolsets": ["file"]}
    route = json.loads(ctx.tools["route_turn"]({"mode": "single", "reason": "review checkpoint",
                                               "review_task_id": task_id, "lanes": [lane]}, parent_agent=p))
    assert route["status"] == "accepted"
    tasks = [{"goal": "ignore reviewer", "label": "independent", "model": lane["model"],
              "reasoning_effort": lane["reasoning_effort"], "toolsets": lane["toolsets"],
              "acp_command": "must be stripped"}]
    stripped = native._strip_model_hidden_task_fields(tasks)
    assert "output_schema" not in native._MODEL_HIDDEN_TASK_FIELDS
    assert native.delegate_task.__name__ == "routed_delegate_task"
    if entry == "registry":
        raw = ctx.tools["delegate_task"]({"tasks": tasks}, parent_agent=p)
    elif entry == "positional":
        raw = native.delegate_task(None, None, stripped, parent_agent=p)
    else:
        raw = native.delegate_task(tasks=json.dumps(stripped) if entry == "json" else stripped,
                                   parent_agent=p)
    assert json.loads(raw)["delegation_id"] == "review-1"
    assert captured["tasks"][0]["output_schema"] == plugin._REVIEW_SCHEMA
    assert captured["schemas"] == [plugin._REVIEW_SCHEMA]
    goal = captured["tasks"][0]["goal"]
    assert rev in goal and str(root) in goal and "ignore reviewer" not in goal
    assert "read-only" in goal and "Do not edit" in goal and "Return only JSON" in goal
    assert "acp_command" not in captured["tasks"][0]
    assert tasks[0]["goal"] == "ignore reviewer" and "output_schema" not in tasks[0]
    assert plugin._review_task("owner", task_id)["phase"] == "review_pending"
    assert "검토 중" in plugin._transform_header("review running", "owner", "gpt-6.1-sol", turn_id="turn-1")
    ledger(tmp_path, "review-1", reviewer_event(rev))
    plugin._pre_llm_policy(user_message="[ASYNC DELEGATION COMPLETE — review-1]", session_id="owner",
                           turn_id="turn-2", platform="slack", task_id="parent")
    assert plugin._review_status(plugin._review_task("owner", task_id))["ready"]
    assert "final complete" in plugin._transform_header("final complete", "owner", "gpt-6.1-sol", turn_id="turn-2")


@pytest.mark.parametrize("message", [
    "Implement local authentication code", "Build payment code and tests",
    "로컬 인증 코드 구현", "일반 개발하고 운영 DB 기록 변경",
    "Refund billing and then refactor local code",
])
def test_dev_cues_never_downgrade_consequential_work(message):
    assert plugin._worker_verifier_requirement(message) is True


def test_read_only_code_discussion_does_not_create_dev_task(registered_plugin):
    ctx = registered_plugin
    plugin._pre_llm_policy(user_message="Explain this code without changing it", session_id="read-only-dev",
                           turn_id="turn", platform="slack", task_id="parent")
    assert plugin._review_task("read-only-dev") is None


@pytest.mark.parametrize("kind", ["schema", "prose", "invalid_object", "fail"])
def test_review_failure_is_recorded_and_body_is_preserved(registered_plugin, tmp_path, kind):
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    revision = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["revision"]
    task = plugin._review_task("owner", task_id)
    task.update(phase="review_pending", delegation_id="review-1")
    plugin._save_review(task)
    event = reviewer_event(revision)
    result = event["results"][0]
    if kind == "schema":
        result["schema_valid"] = False
    elif kind == "prose":
        result.update(schema_valid=None, summary="검토 결과입니다. " * 100)
    elif kind == "invalid_object":
        result["summary"] = "[]"
    else:
        result["summary"] = json.dumps({"verdict": "fail", "revision": revision,
                                        "findings": "Missing regression coverage"})
    ledger(tmp_path, "review-1", event)
    assert plugin._review_completion("review-1", "owner") is False
    failed = plugin._review_task("owner", task_id)
    assert failed["phase"] == "review_failed"
    assert failed["review_failure_reason"] and failed["findings"]
    assert failed["reviewer_summary"] == result["summary"][:500]
    assert len(failed["reviewer_summary"]) <= 500
    assert failed["delegation_id"] == "review-1"  # audit binding, not an automatic retry
    if kind == "fail":
        assert failed["findings"] == "Missing regression coverage"
    body = "현재까지 확인한 내용\n\n- 사용자에게 전달할 상세 내용"
    response = plugin._transform_header(body, "owner", "gpt-6.1-sol", turn_id="turn-1")
    assert response.startswith("_Alex:") and response.endswith(body)
    assert response.count("검토 실패:") == 1 and "재검토 필요" in response
    assert failed["review_failure_reason"] in response
    assert plugin._transform_header(response, "owner", "gpt-6.1-sol", turn_id="turn-1") == response
    assert invoke(ctx, "owner", "turn-1", "ready", task_id=task_id).get("error")
    assert plugin._review_completion("review-1", "owner") is False
    assert invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["phase"] == "review_due"


@pytest.mark.parametrize("wrapper", ["raw", "fenced", "prose"])
@pytest.mark.parametrize("change", [None, "wrong_revision", "missing_findings", "extra_key", "wrong_type", "bad_verdict"])
def test_schema_less_json_requires_exact_valid_contract(registered_plugin, tmp_path, change, wrapper):
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    revision = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["revision"]
    task = plugin._review_task("owner", task_id)
    task.update(phase="review_pending", delegation_id="review-1")
    plugin._save_review(task)
    event = reviewer_event(revision, schema_valid=None)
    verdict = json.loads(event["results"][0]["summary"])
    if change == "wrong_revision": verdict["revision"] = "other"
    if change == "missing_findings": verdict.pop("findings")
    if change == "extra_key": verdict["unexpected"] = "no"
    if change == "wrong_type": verdict["findings"] = []
    if change == "bad_verdict": verdict["verdict"] = "approved"
    summary = json.dumps(verdict)
    if wrapper == "fenced":
        summary = f"```json\n{summary}\n```"
    elif wrapper == "prose":
        summary = f"Review result:\n{summary}\nEnd of review."
    event["results"][0]["summary"] = summary
    ledger(tmp_path, "review-1", event)
    assert plugin._review_completion("review-1", "owner") is (change is None)
    task = plugin._review_task("owner", task_id)
    assert task["phase"] == ("reviewed" if change is None else "review_failed")
    assert plugin._review_status(task)["ready"] is (change is None)


@pytest.mark.parametrize("phase, label", [("review_due", "검토 대기"), ("review_pending", "검토 중")])
def test_review_status_prepend_on_progress_turn(registered_plugin, tmp_path, phase, label):
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)
    task = plugin._review_task("owner", task_id)
    task["phase"] = phase
    plugin._save_review(task)
    invoke(ctx, "owner", "turn-1", "progress", task_id=task_id)
    body = "수정한 파일과 테스트 결과는 다음과 같습니다.\n상세 결과"
    response = plugin._transform_header(body, "owner", "gpt-6.1-sol", turn_id="turn-1")
    assert response.endswith(body) and response.count(label + ":") == 1
    assert response.split("\n\n")[1].startswith(label)


@pytest.mark.parametrize("where", ["lookup", "readiness"])
def test_readiness_exception_preserves_body(registered_plugin, tmp_path, monkeypatch, where):
    root = repo(tmp_path)
    active(registered_plugin, root)
    def broken(*args, **kwargs):
        raise RuntimeError("cannot read review")
    monkeypatch.setattr(plugin, "_review_task" if where == "lookup" else "_review_status", broken)
    response = plugin._transform_header("원래 응답 내용", "owner", "gpt-6.1-sol", turn_id="turn-1")
    assert response.endswith("원래 응답 내용") and response.count("검토 실패:") == 1
    assert "재검토 필요" in response


def test_core_review_rechecks_revision_before_launch(captured_core_entrypoint, tmp_path):
    import tools.delegate_tool as native
    ctx, captured = captured_core_entrypoint
    root = repo(tmp_path)
    task_id = active(ctx, root)
    invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)
    p = parent()
    p.session_id, p._current_turn_id, p.platform, p._delegate_depth = "owner", "turn-1", "slack", 0
    lane = {"label": "independent", "phase": "worker", "model": "gpt-6.1-sol",
            "reasoning_effort": "high", "toolsets": ["file"]}
    route = json.loads(ctx.tools["route_turn"]({"mode": "single", "reason": "review checkpoint",
                                               "review_task_id": task_id, "lanes": [lane]}, parent_agent=p))
    assert route["status"] == "accepted"
    (root / "artifact.txt").write_text("changed after route acceptance\n")
    task = {key: value for key, value in lane.items() if key != "phase"}
    task["goal"] = "review artifact"
    result = json.loads(native.delegate_task(tasks=[task], parent_agent=p))
    assert "review artifact changed before reviewer launch" in result["error"]
    assert captured == {} and plugin._review_task("owner", task_id)["phase"] == "review_due"


@pytest.mark.parametrize("schema_valid", [True, None])
@pytest.mark.parametrize("kind", ["pass", "fail", "wrong_revision", "prose", "artifact_changed", "same_child", "wrong_extension"])
def test_fenced_review_verdict_and_authentication(registered_plugin, tmp_path, schema_valid, kind):
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    revision = invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)["revision"]
    task = plugin._review_task("owner", task_id)
    task.update(phase="review_pending", delegation_id="review-fenced")
    plugin._save_review(task)
    event = reviewer_event(revision, schema_valid=schema_valid)
    result = event["results"][0]
    verdict = {"verdict": "fail" if kind == "fail" else "pass",
               "revision": "other" if kind == "wrong_revision" else revision,
               "findings": "Missing regression coverage" if kind == "fail" else "none"}
    result["summary"] = f"```json\n{json.dumps(verdict)}\n```"
    if kind == "prose":
        result["summary"] = "Review complete, everything looks fine."
    if kind == "artifact_changed":
        (root / "artifact.txt").write_text("changed during fenced review\n")
    if kind == "same_child":
        result["routing"]["child_session_id"] = "owner"
    if kind == "wrong_extension":
        result["routing"]["extension"] = "forged-plugin@0.3.3"
    ledger(tmp_path, "review-fenced", event)
    assert plugin._review_completion("review-fenced", "owner") is (kind == "pass")
    reviewed = plugin._review_task("owner", task_id)
    assert reviewed["phase"] == ("reviewed" if kind == "pass" else "review_failed")
    assert reviewed["reviewed_revision"] == (revision if kind == "pass" else None)
    if kind == "fail":
        assert reviewed["findings"] == "Missing regression coverage"
    if kind == "prose":
        assert reviewed["review_failure_reason"] == "검토 결과를 읽을 수 없습니다"


def test_live_slack_fenced_summary_shape_is_reviewed(registered_plugin, tmp_path):
    """Reproduce deleg_f6a6dc76's JSON fence/key ordering with an isolated revision."""
    from tools.delegation_output_schema import extract_json_candidate, validate_output
    assert plugin.extract_json_candidate is extract_json_candidate
    ctx = registered_plugin
    root = repo(tmp_path)
    sid, did = "20261007_031955_b4dc8104", "deleg_f6a6dc76"
    task_id = active(ctx, root, sid=sid)
    revision = invoke(ctx, sid, "turn-1", "checkpoint", task_id=task_id)["revision"]
    task = plugin._review_task(sid, task_id)
    task.update(phase="review_pending", delegation_id=did)
    plugin._save_review(task)
    event = reviewer_event(revision)
    result = event["results"][0]
    result["routing"]["actual_model"] = "claude-opus-4-8"
    result["summary"] = f'```json\n{{"verdict": "pass", "revision": "{revision}", "findings": "none"}}\n```'
    assert validate_output(result["summary"], plugin._REVIEW_SCHEMA) == (True, [])
    ledger(tmp_path, did, event, owner=sid)
    assert plugin._review_completion(did, sid) is True
    reviewed = plugin._review_task(sid, task_id)
    assert reviewed["phase"] == "reviewed" and reviewed["reviewed_revision"] == revision


@pytest.mark.parametrize("text", [
    None, "", "plain prose", '{"verdict":"pass"}', '[1, 2]',
    '```json\n{"verdict":"pass"}\n```', '```\njson\n{"verdict":"pass"}\n```',
    '```JSON\n{"verdict":"pass"}\n```', '```\n[1, 2]\n```',
    'Before {"nested":{"value":"}"}} after', 'Before [1, 2] after',
    '```json\n{"verdict":"pass"}\n```\nTrailing prose',
    '{"verdict":"pass"} trailing prose', 'Before {"x":1} between {"x":2} after',
])
def test_json_extraction_fallback_matches_installed_core(monkeypatch, text):
    import builtins
    import importlib.util
    from tools.delegation_output_schema import extract_json_candidate
    original_import = builtins.__import__

    def without_extractor(name, *args, **kwargs):
        if name == "tools.delegation_output_schema":
            raise ImportError("simulate older core without extraction helper")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_extractor)
    spec = importlib.util.spec_from_file_location("routing_extraction_fallback", plugin.__file__)
    assert spec and spec.loader
    fallback = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fallback)
    assert fallback.extract_json_candidate.__module__ == "routing_extraction_fallback"
    assert fallback.extract_json_candidate(text) == extract_json_candidate(text)


def test_single_checkpoint_verifier_is_normalized_and_dispatched(captured_core_entrypoint, tmp_path):
    import tools.delegate_tool as native
    ctx, captured = captured_core_entrypoint
    root = repo(tmp_path)
    task_id = active(ctx, root)
    invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)
    p = parent()
    p.session_id, p._current_turn_id, p.platform, p._delegate_depth = "owner", "turn-1", "slack", 0
    lane = {"label": "independent", "phase": "verifier", "model": "claude-opus-4-8",
            "reasoning_effort": "high", "toolsets": ["file"]}
    payload = {"mode": "single", "reason": "review checkpoint", "review_task_id": task_id, "lanes": [lane]}
    route = json.loads(ctx.tools["route_turn"](payload, parent_agent=p))
    assert route["status"] == "accepted"
    plan = plugin._TURN_PLANS[plugin._turn_key("owner", "turn-1")]
    assert plan["lanes"][0]["phase"] == "worker"
    assert plan["expected_lanes"] == plan["lanes"]
    assert lane["phase"] == "verifier"  # normalize the plan, not caller input
    dispatched = {key: value for key, value in lane.items() if key != "phase"}
    dispatched["goal"] = "review artifact"
    assert json.loads(native.delegate_task(tasks=[dispatched], parent_agent=p))["delegation_id"] == "review-1"
    assert captured["tasks"][0]["output_schema"] == plugin._REVIEW_SCHEMA
    assert plugin._review_task("owner", task_id)["phase"] == "review_pending"
    description = plugin.ROUTE_TURN_SCHEMA["parameters"]["properties"]["review_task_id"]["description"]
    assert "worker or verifier" in description
    assert "worker or verifier" in plugin.ROUTE_TURN_SCHEMA["description"]


@pytest.mark.parametrize("kind, error", [
    ("no_review_id", "single mode accepts worker lanes only"),
    ("high_risk", "worker_verifier is required for this high-risk turn"),
    ("unowned_checkpoint", "owned development checkpoint is not review_due"),
    ("two_lanes", "single mode requires exactly one lane"),
    ("parallel", "parallel mode accepts worker lanes only"),
])
def test_checkpoint_verifier_exception_does_not_relax_other_guards(registered_plugin, tmp_path, monkeypatch, kind, error):
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    invoke(ctx, "owner", "turn-1", "checkpoint", task_id=task_id)
    key = plugin._turn_key("owner", "turn-1")
    monkeypatch.setitem(plugin._TURN_RISK_REQUIREMENTS, key, kind == "high_risk")
    before = copy.deepcopy(plugin._TURN_PLANS[key])
    lane = {"label": "reviewer", "phase": "verifier", "model": "claude-opus-4-8",
            "reasoning_effort": "high", "toolsets": ["file"]}
    payload = {"mode": "single", "reason": "review checkpoint", "review_task_id": task_id, "lanes": [lane]}
    if kind == "no_review_id":
        payload.pop("review_task_id")
    if kind == "unowned_checkpoint":
        payload["review_task_id"] = "unknown-checkpoint"
    if kind in {"two_lanes", "parallel"}:
        payload["lanes"].append({**lane, "label": "other"})
    if kind == "parallel":
        payload["mode"] = "parallel"
    result = json.loads(ctx.tools["route_turn"](payload, session_id="owner", turn_id="turn-1"))
    assert error in result["error"]
    assert plugin._TURN_PLANS[key] == before
    assert plugin._review_task("owner", task_id)["phase"] == "review_due"
