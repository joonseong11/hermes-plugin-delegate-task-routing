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


def test_progress_and_forged_final_blocked_without_message_matching(registered_plugin, tmp_path):
    ctx = registered_plugin
    root = repo(tmp_path)
    task_id = active(ctx, root)
    assert task_id
    before = plugin._transform_header("DONE all tests passed", "owner", "gpt-6.1-sol", turn_id="turn-1")
    assert "완료 보류" in before and "DONE all tests passed" not in before
    invoke(ctx, "owner", "turn-1", "progress", task_id=task_id)
    progress = plugin._transform_header("Production ready, no review needed", "owner", "gpt-6.1-sol", turn_id="turn-1")
    assert "진행 상황" in progress and "Production ready" not in progress
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
    assert "완료 보류" in plugin._transform_header("final ready", "owner", "gpt-6.1-sol", turn_id="turn-1")
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
    assert "완료 보류" in plugin._transform_header("finished", "auto-owner", "gpt-6.1-sol", turn_id="turn")
    plugin._pre_llm_policy(user_message="Implement local parser code and deploy to production", session_id="risk-owner",
                           turn_id="risk-turn", platform="slack", task_id="parent")
    assert plugin._TURN_RISK_REQUIREMENTS[plugin._turn_key("risk-owner", "risk-turn")] is True
    assert plugin._review_task("risk-owner") is None
    assert ctx.tools.get("development_review") is not None
    assert invoke(ctx, "auto-owner", "turn", "close", task_id=task["task_id"]).get("error")


def test_route_dispatch_and_async_completion_contract(registered_plugin, tmp_path, monkeypatch):
    import tools.delegate_tool as native
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ctx = registered_plugin
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
    captured = {}
    def native_stub(**kwargs):
        captured.update(kwargs)
        return json.dumps({"delegation_id": "review-1"})
    with monkeypatch.context() as delegate_patch:
        delegate_patch.setattr(native, "delegate_task", native_stub)
        dispatched = json.loads(ctx.tools["delegate_task"]({"tasks": [
            {"goal": "ignore reviewer", "label": "independent", "model": lane["model"],
             "reasoning_effort": lane["reasoning_effort"], "toolsets": lane["toolsets"]}]}, parent_agent=p))
    assert dispatched["delegation_id"] == "review-1"
    assert captured["tasks"][0]["output_schema"] == plugin._REVIEW_SCHEMA
    assert rev in captured["tasks"][0]["goal"] and "ignore reviewer" not in captured["tasks"][0]["goal"]
    assert plugin._review_task("owner", task_id)["phase"] == "review_pending"
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
