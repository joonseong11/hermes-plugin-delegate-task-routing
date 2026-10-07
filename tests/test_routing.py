from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_PATH = Path(
    os.environ.get("PLUGIN_PATH", Path(__file__).resolve().parents[1] / "__init__.py")
)
spec = importlib.util.spec_from_file_location("delegate_task_routing_plugin", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(plugin)


def base_schema():
    return {
        "name": "delegate_task",
        "description": "native",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "goal": {"type": "string"},
                            "context": {"type": "string"},
                            "output_schema": {"type": "object"},
                        },
                    },
                }
            },
        },
    }


@pytest.fixture(autouse=True)
def retained_delegate_request_schema(monkeypatch):
    """Model the schema retained by register() for middleware-only unit tests."""
    monkeypatch.setattr(plugin, "_DELEGATE_TASK_REQUEST_SCHEMA", base_schema())


class FakeChild:
    def __init__(self, *, model, toolsets, parent):
        self.model = model or parent.model
        self.provider = parent.provider
        self.enabled_toolsets = list(toolsets) if toolsets is not None else list(parent.enabled_toolsets)
        self.reasoning_config = dict(parent.reasoning_config)
        self.session_id = f"child-{self.model}-{id(self)}"
        self._delegate_role = "leaf"


def fake_module():
    module = SimpleNamespace()
    module.DELEGATE_TASK_SCHEMA = base_schema()
    module.check_delegate_requirements = lambda: True
    module._build_dynamic_schema_overrides = lambda: {
        "description": "dynamic native",
        "parameters": base_schema()["parameters"],
    }

    def build_child(
        task_index,
        goal,
        context,
        toolsets,
        model,
        max_iterations,
        task_count,
        parent_agent,
        **kwargs,
    ):
        return FakeChild(model=model, toolsets=toolsets, parent=parent_agent)

    def run_single(task_index, goal, child=None, parent_agent=None, **kwargs):
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": goal,
            "model": child.model,
        }

    def delegate_task(
        goal=None,
        context=None,
        tasks=None,
        max_iterations=None,
        role=None,
        background=None,
        output_schema=None,
        action=None,
        subagent_id=None,
        message=None,
        parent_agent=None,
        credentials_cfg=None,
    ):
        if action in {"list", "steer", "stop"}:
            return json.dumps({"action": action})
        task_list = tasks or [{"goal": goal, "context": context}]
        results = []
        for index, task in enumerate(task_list):
            child = module._build_child_preserving_parent_tools(
                task_index=index,
                goal=task["goal"],
                context=task.get("context"),
                toolsets=None,
                model=None,
                max_iterations=10,
                task_count=len(task_list),
                parent_agent=parent_agent,
            )
            results.append(
                module._run_single_child(index, task["goal"], child, parent_agent)
            )
        return json.dumps({"results": results}, sort_keys=True)

    module._build_child_agent = build_child
    module._build_child_preserving_parent_tools = build_child
    module._run_single_child = run_single
    module.delegate_task = delegate_task
    return module


def settings(key, default=None):
    values = {
        "allowed_models": [
            "gpt-5.6-luna-900k",
            "gpt-5.6-terra-900k",
            "gpt-5.6-sol-900k",
            "gpt-6-astra",
            "gpt-6.1-sol",
            "gpt-6-luna",
            "claude-opus-5",
            "claude-fable-5.1",
            "claude-sonnet-5",
        ],
        "allowed_reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
        "allowed_toolsets": ["web", "file", "terminal", "code_execution", "reader_db"],
    }
    return values.get(key, default)


def parent():
    return SimpleNamespace(
        model="gpt-5.6-sol-900k",
        provider="openai-codex",
        enabled_toolsets=["web", "file", "terminal", "code_execution", "reader_db"],
        reasoning_config={"enabled": True, "effort": "medium"},
    )


def routed_task(
    goal="x",
    *,
    label="작업",
    model="gpt-5.6-luna-900k",
    effort="low",
    toolsets=None,
    **extra,
):
    task = {
        "goal": goal,
        "label": label,
        "model": model,
        "reasoning_effort": effort,
        "toolsets": list(toolsets or ["web"]),
    }
    task.update(extra)
    return task


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def test_schema_exposes_exact_allowlisted_routing_fields():
    module = fake_module()
    schema = plugin._make_schema(
        module,
        allowed_models=settings("allowed_models"),
        allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"),
    )
    props = plugin._task_item_properties(schema)
    assert props["model"]["enum"] == settings("allowed_models")
    assert props["reasoning_effort"]["enum"] == settings("allowed_reasoning_efforts")
    assert props["toolsets"]["items"]["enum"] == settings("allowed_toolsets")
    required = schema["parameters"]["properties"]["tasks"]["items"]["required"]
    assert set(required) >= {"goal", "label", "model", "reasoning_effort", "toolsets"}


def test_mixed_batch_routes_model_effort_and_toolsets_and_reports_actuals():
    module = fake_module()
    original_delegate = module.delegate_task
    original_builder = module._build_child_preserving_parent_tools
    original_runner = module._run_single_child
    unload = plugin._install_patches(module, get_config=settings)
    try:
        raw = module.delegate_task(
            tasks=[
                {
                    "goal": "collect simple facts",
                    "label": "자료수집",
                    "model": "gpt-5.6-luna-900k",
                    "reasoning_effort": "low",
                    "toolsets": ["web"],
                },
                {
                    "goal": "analyze operational risk",
                    "label": "위험분석",
                    "model": "gpt-5.6-terra-900k",
                    "reasoning_effort": "high",
                    "toolsets": ["file", "code_execution"],
                },
            ],
            parent_agent=parent(),
            background=True,
        )
        data = json.loads(raw)
        first, second = data["results"]
        assert first["routing"]["requested_model"] == "gpt-5.6-luna-900k"
        assert first["routing"]["actual_model"] == "gpt-5.6-luna-900k"
        assert first["routing"]["actual_reasoning_effort"] == "low"
        assert first["routing"]["effective_toolsets"] == ["web"]
        assert second["routing"]["actual_model"] == "gpt-5.6-terra-900k"
        assert second["routing"]["actual_reasoning_effort"] == "high"
        assert second["routing"]["effective_toolsets"] == ["file", "code_execution"]
        assert second["routing"]["actual_provider"] == "openai-codex"
        assert second["routing"]["child_session_id"].startswith("child-")
    finally:
        unload()
    assert module.delegate_task is original_delegate
    assert module._build_child_preserving_parent_tools is original_builder
    assert module._run_single_child is original_runner


@pytest.mark.parametrize(
    "task,error_text",
    [
        (routed_task(model="unapproved-model"), "allowed_models"),
        (routed_task(effort="ultra"), "not allowed"),
        (routed_task(toolsets=["messaging"]), "unapproved toolsets"),
    ],
)
def test_unapproved_routes_fail_closed(task, error_text):
    module = fake_module()
    unload = plugin._install_patches(module, get_config=settings)
    try:
        data = json.loads(module.delegate_task(tasks=[task], parent_agent=parent()))
        assert "error" in data
        assert error_text in data["error"]
    finally:
        unload()


def test_toolsets_cannot_broaden_parent_permissions():
    module = fake_module()
    unload = plugin._install_patches(module, get_config=settings)
    limited_parent = parent()
    limited_parent.enabled_toolsets = ["web"]
    try:
        data = json.loads(
            module.delegate_task(
                tasks=[routed_task(toolsets=["file"])],
                parent_agent=limited_parent,
            )
        )
        assert "error" in data
        assert "broaden" in data["error"]
    finally:
        unload()


def test_native_post_build_toolset_broadening_fails_closed():
    module = fake_module()
    original_builder = module._build_child_preserving_parent_tools

    def broadening_builder(*args, **kwargs):
        child = original_builder(*args, **kwargs)
        child.enabled_toolsets.append("delegation")
        child.close = lambda: None
        return child

    module._build_child_preserving_parent_tools = broadening_builder
    unload = plugin._install_patches(module, get_config=settings)
    try:
        data = json.loads(
            module.delegate_task(
                tasks=[routed_task(toolsets=["web"])],
                parent_agent=parent(),
            )
        )
        assert "error" in data
        assert "broadened requested toolsets" in data["error"]
    finally:
        unload()


def anthropic_parent():
    """Parent running on anthropic (e.g. claude-opus) — the real gateway state
    that makes a gpt-* lane cross-provider."""
    return SimpleNamespace(
        model="claude-opus-5",
        provider="anthropic",
        enabled_toolsets=["web", "file", "terminal", "code_execution", "reader_db"],
        reasoning_config={"enabled": True, "effort": "medium"},
    )


def _install_capturing_creds(monkeypatch):
    """Make the plugin's cross-provider credential resolution hermetic: return a
    fixed codex bundle instead of hitting the live provider system."""
    import types

    fake_bundle = {
        "model": None,
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_key": "codex-key",
        "api_mode": "codex_responses",
        "request_overrides": {},
    }

    def fake_resolve(cfg, parent_agent):
        out = dict(fake_bundle)
        out["model"] = cfg.get("model")
        if cfg.get("provider") == "anthropic":
            out.update(provider="anthropic", base_url="https://api.anthropic.com",
                       api_key="anthropic-key", api_mode="anthropic_messages")
        return out

    fake_mod = types.ModuleType("tools.delegate_tool_config")
    fake_mod._resolve_delegation_credentials = fake_resolve
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_config", fake_mod)


def test_cross_provider_lane_pins_provider_and_fallback(monkeypatch):
    """A gpt-* lane on an anthropic parent must pin openai-codex creds AND get an
    Ordered cross-provider fallback chain, instead of inheriting anthropic and 404ing."""
    _install_capturing_creds(monkeypatch)
    module = fake_module()
    captured = {}
    original_builder = module._build_child_preserving_parent_tools

    def capturing_builder(*args, **kwargs):
        captured.update(kwargs)
        return original_builder(*args, **kwargs)

    module._build_child_preserving_parent_tools = capturing_builder
    unload = plugin._install_patches(module, get_config=settings)
    try:
        module.delegate_task(
            tasks=[routed_task(model="gpt-6.1-sol", effort="high", toolsets=["file"])],
            parent_agent=anthropic_parent(),
        )
        assert captured.get("override_provider") == "openai-codex"
        assert captured.get("override_base_url") == "https://chatgpt.com/backend-api/codex"
        assert captured.get("override_api_key") == "codex-key"
        assert captured.get("override_api_mode") == "codex_responses"
        chain = (captured.get("routing_cfg") or {}).get("fallback_providers")
        assert chain == [
            {"provider": "openai-codex", "model": "gpt-6-sol"},
            {"provider": "openai-codex", "model": "gpt-5.6-sol"},
            {"provider": "openai-codex", "model": "gpt-6-luna"},
            {"provider": "anthropic", "model": "claude-opus-5"},
            {"provider": "anthropic", "model": "claude-sonnet-5"},
        ]
    finally:
        unload()


def test_cross_provider_fallback_chain_matches_order(monkeypatch):
    """Ordered recovery suffixes; Astra intentionally has no implicit fallback."""
    _install_capturing_creds(monkeypatch)
    for lane_model in ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"):
        module = fake_module()
        captured = {}
        original_builder = module._build_child_preserving_parent_tools

        def capturing_builder(*args, __cap=captured, __orig=original_builder, **kwargs):
            __cap.update(kwargs)
            return __orig(*args, **kwargs)

        module._build_child_preserving_parent_tools = capturing_builder
        unload = plugin._install_patches(module, get_config=settings)
        try:
            module.delegate_task(
                tasks=[routed_task(model=lane_model, effort="high", toolsets=["file"])],
                parent_agent=anthropic_parent(),
            )
            chain = (captured.get("routing_cfg") or {}).get("fallback_providers")
            expected_models = {
                "gpt-6.1-sol": ["gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna", "claude-opus-5", "claude-sonnet-5"],
                "gpt-6-luna": ["claude-opus-5", "claude-sonnet-5"],
                "gpt-6-astra": [],
            }[lane_model]
            assert chain == [{"provider": "openai-codex" if m.startswith("gpt-") else "anthropic",
                              "model": m} for m in expected_models]
        finally:
            unload()


def test_same_provider_lane_does_not_override(monkeypatch):
    """When the parent already runs openai-codex, a gpt-* lane must NOT inject an
    override_provider, but approved fallback must still be available."""
    _install_capturing_creds(monkeypatch)
    module = fake_module()
    captured = {}
    original_builder = module._build_child_preserving_parent_tools

    def capturing_builder(*args, **kwargs):
        captured.update(kwargs)
        return original_builder(*args, **kwargs)

    module._build_child_preserving_parent_tools = capturing_builder
    unload = plugin._install_patches(module, get_config=settings)
    try:
        module.delegate_task(
            tasks=[routed_task(model="gpt-6-luna", effort="high", toolsets=["file"])],
            parent_agent=parent(),  # provider="openai-codex"
        )
        assert not captured.get("override_provider")
        assert captured["routing_cfg"]["fallback_providers"] == [
            {"provider": "anthropic", "model": "claude-opus-5"},
            {"provider": "anthropic", "model": "claude-sonnet-5"},
        ]
    finally:
        unload()


def test_routed_orchestrator_role_is_rejected():
    module = fake_module()
    unload = plugin._install_patches(module, get_config=settings)
    try:
        data = json.loads(
            module.delegate_task(
                tasks=[
                    {
                        "goal": "x",
                        "label": "검토",
                        "model": "gpt-5.6-terra-900k",
                        "reasoning_effort": "high",
                        "toolsets": ["file"],
                        "role": "orchestrator",
                    }
                ],
                parent_agent=parent(),
            )
        )
        assert "error" in data
        assert "must remain a leaf" in data["error"]
    finally:
        unload()


def test_contextvars_isolate_concurrent_mixed_batches():
    module = fake_module()
    unload = plugin._install_patches(module, get_config=settings)
    try:
        def run(model, effort, toolset):
            return json.loads(
                module.delegate_task(
                    tasks=[
                        {
                            "goal": f"work with {model}",
                            "label": "작업",
                            "model": model,
                            "reasoning_effort": effort,
                            "toolsets": [toolset],
                        }
                    ],
                    parent_agent=parent(),
                )
            )["results"][0]["routing"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(run, "gpt-5.6-luna-900k", "low", "web")
            b = pool.submit(run, "gpt-5.6-sol-900k", "xhigh", "terminal")
            ra, rb = a.result(), b.result()
        assert (ra["actual_model"], ra["actual_reasoning_effort"], ra["effective_toolsets"]) == (
            "gpt-5.6-luna-900k",
            "low",
            ["web"],
        )
        assert (rb["actual_model"], rb["actual_reasoning_effort"], rb["effective_toolsets"]) == (
            "gpt-5.6-sol-900k",
            "xhigh",
            ["terminal"],
        )
    finally:
        unload()


def test_native_full_support_causes_no_patch():
    module = fake_module()
    props = plugin._task_item_properties(module.DELEGATE_TASK_SCHEMA)
    for field in plugin._ROUTE_FIELDS:
        props[field] = {"type": "string"}
    original = module.delegate_task
    unload = plugin._install_patches(module, get_config=settings)
    assert module.delegate_task is original
    unload()


def test_installed_hermes_contract_is_compatible():
    import tools.delegate_tool as live_delegate

    plugin._assert_compatible(live_delegate)
    assert not plugin._native_has_full_routing(live_delegate)


def test_missing_routing_fields_fail_closed():
    module = fake_module()
    unload = plugin._install_patches(module, get_config=settings)
    try:
        data = json.loads(
            module.delegate_task(tasks=[{"goal": "x"}], parent_agent=parent())
        )
        assert "missing mandatory routing fields" in data["error"]
    finally:
        unload()


def test_route_plan_modes_reject_removed_worker_verifier():
    direct = plugin._validate_plan(
        {"mode": "direct", "reason": "one step", "lanes": []},
        allowed_models=settings("allowed_models"),
        allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"),
    )
    assert direct["mode"] == "direct"
    worker = {
        "label": "작업",
        "work_type": "mechanical",
        "model": "gpt-5.6-terra-900k",
        "reasoning_effort": "high",
        "toolsets": ["file"],
    }
    requested_check = {
        "label": "기술검수",
        "work_type": "verification",
        "model": "gpt-6.1-sol",
        "reasoning_effort": "high",
        "toolsets": ["file"],
    }
    with pytest.raises(ValueError, match="mode must be direct, single, or parallel"):
        plugin._validate_plan(
            {"mode": "worker_verifier", "reason": "consequential change",
             "lanes": [worker, requested_check]},
            allowed_models=settings("allowed_models"),
            allowed_efforts=settings("allowed_reasoning_efforts"),
            allowed_toolsets=settings("allowed_toolsets"),
        )
    # A verification the user asked for is an ordinary lane.
    plan = plugin._validate_plan(
        {"mode": "single", "reason": "user asked for a check", "lanes": [requested_check]},
        allowed_models=settings("allowed_models"),
        allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"),
    )
    assert plan["lanes"] == [requested_check]


@pytest.mark.parametrize("message", [
    "Handle the deployments.",
    "Change production configuration.",
    "Transfer funds.",
    "Change the team permissions.",
    "프로덕션 배포 전 독립 검증을 해줘",
    "로컬 코드 수정 후 버그를 구현해줘",
])
def test_request_wording_never_forces_verification_or_review(message):
    plugin._PLUGIN_STATE = FakeState()
    context = dict(session_id="wording", turn_id="wording-turn", task_id="parent-task",
                   platform="slack", parent_session_id="")
    key = plugin._turn_key("wording", "wording-turn")
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS.pop(key, None)
    assert plugin._pre_llm_policy(user_message=message, **context) is None
    assert key not in plugin._TURN_PLANS
    assert plugin._PLUGIN_STATE.get("development_reviews", None) is None
    direct = plugin._validate_plan(
        {"mode": "direct", "reason": "routine", "lanes": []},
        allowed_models=settings("allowed_models"),
        allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"),
    )
    assert direct["mode"] == "direct"


def test_llm_request_policy_forces_route_then_delegate():
    plugin._ENFORCED_PLATFORMS = {"slack"}
    key = plugin._turn_key("parent-session", "turn-1")
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS.pop(key, None)
    first = plugin._llm_request_policy(
        {"messages": []},
        session_id="parent-session",
        turn_id="turn-1",
        task_id="parent-task",
        platform="slack",
        api_mode="chat_completions",
    )
    assert first["request"]["tool_choice"]["function"]["name"] == "route_turn"
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key] = {
            "mode": "single",
            "lanes": [],
            "dispatched": False,
        }
    second = plugin._llm_request_policy(
        {"messages": []},
        session_id="parent-session",
        turn_id="turn-1",
        task_id="parent-task",
        platform="slack",
        api_mode="chat_completions",
    )
    assert second["request"]["tool_choice"]["function"]["name"] == "delegate_task"
    assert plugin._forced_tool_choice("route_turn", "codex_responses") == {
        "type": "function",
        "name": "route_turn",
    }
    assert plugin._forced_tool_choice("route_turn", "anthropic_messages") == {
        "type": "tool",
        "name": "route_turn",
    }
    assert plugin._forced_tool_choice("route_turn", "bedrock_converse") == {
        "tool": {"name": "route_turn"},
    }


def test_forced_route_response_blocks_parallel_non_route_tool():
    request_id = "forced-request"
    with plugin._POLICY_LOCK:
        plugin._FORCED_ROUTE_REQUESTS.add(request_id)
    blocked = plugin._pre_tool_policy(
        "terminal",
        session_id="parent-session",
        turn_id="turn-race",
        task_id="parent-task",
        api_request_id=request_id,
        platform="slack",
    )
    assert blocked["action"] == "block"
    assert plugin._pre_tool_policy(
        "route_turn",
        session_id="parent-session",
        turn_id="turn-race",
        task_id="parent-task",
        api_request_id=request_id,
        platform="slack",
    ) is None


def test_non_parent_scopes_are_not_gated_or_headered():
    plugin._pre_llm_policy(
        session_id="cron-session",
        turn_id="cron-turn",
        task_id="cron-task",
        platform="cron",
        parent_session_id="",
        user_message="tick",
    )
    assert plugin._pre_tool_policy(
        "terminal",
        session_id="cron-session",
        turn_id="cron-turn",
        task_id="cron-task",
        api_request_id="cron-request",
    ) is None
    assert plugin._transform_header(
        "cron output",
        session_id="cron-session",
        model="gpt-5.6-sol-900k",
    ) is None


def test_duplicate_lane_labels_fail_closed():
    lane = {
        "label": "검수",
        "work_type": "mechanical",
        "model": "gpt-5.6-luna-900k",
        "reasoning_effort": "low",
        "toolsets": ["file"],
    }
    with pytest.raises(ValueError, match="labels must be unique"):
        plugin._validate_plan(
            {
                "mode": "parallel",
                "reason": "two lanes",
                "lanes": [lane, dict(lane)],
            },
            allowed_models=settings("allowed_models"),
            allowed_efforts=settings("allowed_reasoning_efforts"),
            allowed_toolsets=settings("allowed_toolsets"),
        )


def test_declared_plan_must_match_delegate_tasks():
    p = parent()
    p.session_id = "parent-session"
    p._current_turn_id = "turn-match"
    key = plugin._turn_key(p.session_id, p._current_turn_id)
    lane = {
        "label": "자료수집",
        "work_type": "mechanical",
        "model": "gpt-5.6-luna-900k",
        "reasoning_effort": "low",
        "toolsets": ["web"],
    }
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key] = {
            "mode": "single",
            "lanes": [lane],
            "dispatched": False,
        }
    plugin._validate_delegate_against_plan(
        p,
        [routed_task(label="자료수집", toolsets=["web"])],
    )
    with pytest.raises(ValueError, match="model does not match"):
        plugin._validate_delegate_against_plan(
            p,
            [
                routed_task(
                    label="자료수집",
                    model="gpt-5.6-terra-900k",
                    effort="low",
                    toolsets=["web"],
                )
            ],
        )


def test_header_is_deterministic_and_discloses_fallback():
    session_id = "header-session"
    turn_id = "header-turn"
    with plugin._POLICY_LOCK:
        plugin._CURRENT_TURN[session_id] = turn_id
        plugin._SKIP_HEADER_SESSIONS.discard(session_id)
        plugin._TURN_PLANS[plugin._turn_key(session_id, turn_id)] = {
            "mode": "completion",
            "actual_routes": [
                {
                    "label": "자료수집",
                    "requested_model": "gpt-5.6-luna-900k",
                    "requested_reasoning_effort": "low",
                    "actual_model": "gpt-5.6-terra-900k",
                    "actual_reasoning_effort": None,
                    "status": "completed",
                    "completed": True,
                }
            ],
        }
    result = plugin._transform_header(
        "_Alex: wrong_\n\n본문",
        session_id=session_id,
        model="gpt-5.6-sol-900k",
    )
    assert result.startswith(
        "_Alex: Sol-medium · 자료수집: Luna-low → Terra-unknown_\n\n본문"
    )


def _completed_route(label="구현"):
    return {
        "label": label,
        "requested_model": "claude-opus-5",
        "requested_reasoning_effort": "high",
        "actual_model": "claude-opus-5",
        "actual_reasoning_effort": "high",
        "status": "completed",
        "completed": True,
    }


def test_worker_completion_is_final_and_replay_fails_closed(monkeypatch):
    plugin._PLUGIN_STATE = FakeState()
    worker = {
        "label": "구현",
        "work_type": "implementation",
        "model": "claude-opus-5",
        "reasoning_effort": "high",
        "toolsets": ["file"],
    }
    plan = plugin._validate_plan(
        {"mode": "single", "reason": "write", "lanes": [worker]},
        allowed_models=settings("allowed_models"),
        allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"),
    )
    plan["parent_session_id"] = "parent"
    plugin._mark_delegation_dispatch(plan, {"delegation_id": "deleg-workers"})
    assert plan["dispatched"] is True
    assert plugin._PLUGIN_STATE.get("delegations", None) is None

    monkeypatch.setattr(
        plugin,
        "_verified_completion_routes",
        lambda delegation_id, session_id: [_completed_route()] if session_id == "parent" else [],
    )
    context = dict(session_id="parent", task_id="parent-task", platform="slack")
    plugin._pre_llm_policy(
        turn_id="final-turn", parent_session_id="",
        user_message="[ASYNC DELEGATION BATCH COMPLETE — deleg-workers]", **context,
    )
    final_plan = plugin._TURN_PLANS[plugin._turn_key("parent", "final-turn")]
    assert final_plan["mode"] == "completion"
    assert [x["label"] for x in final_plan["actual_routes"]] == ["구현"]
    assert plugin.delegation_lifecycle_for_turn("parent", "final-turn") == {
        "mode": "completion", "delegation_ids": ["deleg-workers"], "dispatched": True,
    }
    # No verifier dispatch is forced after the workers return.
    assert plugin._llm_request_policy(
        {"input": []}, turn_id="final-turn", api_mode="codex_responses", **context,
    ) is None
    assert plugin._pre_tool_policy("terminal", turn_id="final-turn", **context) is None
    plugin._pre_llm_policy(
        turn_id="replayed", parent_session_id="",
        user_message="[ASYNC DELEGATION BATCH COMPLETE — deleg-workers]", **context,
    )
    assert plugin._TURN_PLANS[plugin._turn_key("parent", "replayed")]["mode"] == "policy_error"


@pytest.mark.parametrize("stage", ["workers_dispatched", "verifiers_dispatched"])
def test_legacy_dispatched_chain_is_claimed_once_as_ordinary_completion(monkeypatch, stage):
    plugin._PLUGIN_STATE = FakeState()
    plugin._PLUGIN_STATE.set("delegations", {"deleg-legacy": {
        "stage": stage, "parent_session_id": "parent", "created_at": 1,
        "lanes": [{"label": "기술검수", "phase": "verifier"}],
        "prior_routes": [_completed_route("구현")], "source_delegation_id": "deleg-source",
    }})
    monkeypatch.setattr(plugin, "_verified_completion_routes", lambda *_: [_completed_route("기술검수")])
    context = dict(task_id="parent-task", platform="slack", parent_session_id="",
                   user_message="[ASYNC DELEGATION BATCH COMPLETE — deleg-legacy]")
    plugin._pre_llm_policy(session_id="other-parent", turn_id="foreign", **context)
    assert plugin._TURN_PLANS[plugin._turn_key("other-parent", "foreign")]["mode"] == "policy_error"
    plugin._pre_llm_policy(session_id="parent", turn_id="legacy-turn", **context)
    plan = plugin._TURN_PLANS[plugin._turn_key("parent", "legacy-turn")]
    assert plan["mode"] == "completion" and plan["dispatched"] is True
    # Only a legacy verifier completion carries the workers that ran before it.
    chained = stage == "verifiers_dispatched"
    assert [x["label"] for x in plan["actual_routes"]] == (["구현", "기술검수"] if chained else ["기술검수"])
    assert plan["owned_delegation_ids"] == (["deleg-source", "deleg-legacy"] if chained else ["deleg-legacy"])
    saved = plugin._PLUGIN_STATE.get("delegations")["deleg-legacy"]
    assert saved["stage"] == "completion_consumed" and "claimed_from_stage" not in saved
    plugin._pre_llm_policy(session_id="parent", turn_id="legacy-replay", **context)
    assert plugin._TURN_PLANS[plugin._turn_key("parent", "legacy-replay")]["mode"] == "policy_error"


def test_lifecycle_contract_does_not_expose_route_payloads():
    platform = type("PlatformValue", (), {"value": "slack"})()
    assert plugin._is_enforced_parent(platform=platform, task_id="parent", parent_session_id="")
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[plugin._turn_key("observer", "turn")] = {
            "mode": "parallel", "delegation_id": "deleg-safe", "dispatched": True,
            "reason": "secret reason", "lanes": [{"goal": "secret goal", "model": "secret model"}],
        }
    assert plugin.delegation_lifecycle_for_turn("observer", "turn") == {
        "mode": "parallel", "delegation_ids": ["deleg-safe"], "dispatched": True,
    }


def test_retention_prunes_oldest_records_including_legacy_chains():
    plugin._PLUGIN_STATE = FakeState()
    legacy = {
        f"live-{index}": {"stage": "workers_dispatched", "parent_session_id": "parent", "created_at": index}
        for index in range(101)
    }
    plugin._PLUGIN_STATE.set("delegations", legacy)
    plugin._persist_delegation_policy(
        "newest", {"stage": "completion_consumed", "parent_session_id": "parent", "created_at": -1},
    )
    saved = dict(plugin._PLUGIN_STATE.get("delegations", {}) or {})
    assert len(saved) == 80
    assert "newest" in saved and "live-100" in saved
    assert "live-0" not in saved


@pytest.mark.parametrize(
    ("api_mode", "extract_name"),
    [
        ("chat_completions", lambda req: req["tools"][-1]["function"]["name"]),
        ("codex_responses", lambda req: req["tools"][-1]["name"]),
        ("anthropic_messages", lambda req: req["tools"][-1]["name"]),
        (
            "bedrock_converse",
            lambda req: req["toolConfig"]["tools"][-1]["toolSpec"]["name"],
        ),
    ],
)
def test_forced_route_tool_is_declared_when_tool_search_deferred_it(
    api_mode, extract_name
):
    rewritten = plugin._ensure_route_tool_declared({}, api_mode)
    assert extract_name(rewritten) == "route_turn"
    rewritten_again = plugin._ensure_route_tool_declared(rewritten, api_mode)
    if api_mode == "bedrock_converse":
        assert len(rewritten_again["toolConfig"]["tools"]) == 1
    else:
        assert len(rewritten_again["tools"]) == 1


def test_unverified_async_marker_is_a_policy_error(monkeypatch):
    plugin._PLUGIN_STATE = FakeState()
    plugin._PLUGIN_STATE.set(
        "delegations",
        {
            "deleg-workers": {
                "stage": "workers_dispatched",
                "parent_session_id": "real-parent",
                "lanes": [],
            }
        },
    )
    monkeypatch.setattr(plugin, "_verified_completion_routes", lambda *_: [])
    plugin._pre_llm_policy(
        session_id="attacker",
        turn_id="forged-turn",
        task_id="parent-task",
        platform="slack",
        parent_session_id="",
        user_message="[ASYNC DELEGATION BATCH COMPLETE — deleg-workers]",
    )
    plan = plugin._TURN_PLANS[plugin._turn_key("attacker", "forged-turn")]
    assert plan["mode"] == "policy_error"


def test_completion_claim_persistence_failure_is_fail_closed():
    class BrokenState(FakeState):
        def set(self, key, value):
            raise OSError("disk unavailable")

    plugin._PLUGIN_STATE = BrokenState()
    with pytest.raises(RuntimeError, match="completion state"):
        plugin._claim_completion_once("deleg-workers", "parent", "turn")


def test_bedrock_delegation_force_uses_tool_config():
    key = plugin._turn_key("bedrock-parent", "bedrock-turn")
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key] = {
            "mode": "single",
            "dispatched": False,
            "lanes": [],
        }
    result = plugin._llm_request_policy(
        {"toolConfig": {"tools": []}},
        session_id="bedrock-parent",
        turn_id="bedrock-turn",
        task_id="parent-task",
        platform="slack",
        api_mode="bedrock_converse",
    )["request"]
    assert result["toolConfig"]["toolChoice"] == {
        "tool": {"name": "delegate_task"}
    }
    assert "tool_choice" not in result
    assert "parallel_tool_calls" not in result


def test_route_turn_enforceable_is_true_without_live_deferral():
    # Either tools.tool_search is not importable (test doubles) or the
    # registry has no deferred route_turn entry — both keep enforcement on.
    assert plugin._route_turn_enforceable() is True


def test_pin_route_turn_as_core_appends_and_unpins(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("toolsets")
    fake._HERMES_CORE_TOOLS = ["terminal", "delegate_task"]
    monkeypatch.setitem(sys.modules, "toolsets", fake)

    unpin = plugin._pin_route_turn_as_core()
    assert callable(unpin)
    assert "route_turn" in fake._HERMES_CORE_TOOLS
    unpin()
    assert "route_turn" not in fake._HERMES_CORE_TOOLS

    # Already pinned by someone else: returns a no-op that keeps the pin.
    fake._HERMES_CORE_TOOLS.append("route_turn")
    unpin_noop = plugin._pin_route_turn_as_core()
    assert callable(unpin_noop)
    unpin_noop()
    assert "route_turn" in fake._HERMES_CORE_TOOLS

    # Unexpected layout: fail soft with None so the request-time
    # bypass path takes over.
    fake._HERMES_CORE_TOOLS = frozenset({"terminal"})
    assert plugin._pin_route_turn_as_core() is None


def test_route_forcing_bypasses_turn_when_route_turn_not_enforceable(monkeypatch):
    monkeypatch.setattr(plugin, "_route_turn_enforceable", lambda: False)
    session_id = "bypass-session"
    turn_id = "bypass-turn"
    key = plugin._turn_key(session_id, turn_id)
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS.pop(key, None)
        plugin._SKIP_HEADER_SESSIONS.discard(session_id)
        plugin._CURRENT_TURN[session_id] = turn_id

    # 1) The request is NOT rewritten: no forced tool_choice that the
    #    executor would reject.
    result = plugin._llm_request_policy(
        {"messages": []},
        session_id=session_id,
        turn_id=turn_id,
        task_id="parent-task",
        platform="slack",
        api_mode="chat_completions",
    )
    assert result is None
    with plugin._POLICY_LOCK:
        assert plugin._TURN_PLANS[key]["mode"] == "policy_bypass"

    # 2) The pre-tool gate must not block ordinary tools in a bypassed turn.
    assert (
        plugin._pre_tool_policy(
            "terminal",
            session_id=session_id,
            turn_id=turn_id,
            task_id="parent-task",
            platform="slack",
        )
        is None
    )

    # 3) delegate_task skips strict plan matching but still runs routed.
    p = parent()
    p.session_id = session_id
    p._current_turn_id = turn_id
    assert (
        plugin._validate_delegate_against_plan(
            p, [routed_task(label="자료수집", toolsets=["web"])]
        )
        is None
    )

    # 4) The header honestly discloses that the policy was skipped.
    header = plugin._transform_header(
        "본문",
        session_id=session_id,
        model="gpt-5.6-sol-900k",
    )
    assert header.startswith("_Alex: Sol-medium · 라우팅 정책 미적용_")


def test_resolve_turn_context_falls_back_to_current_turn_map():
    # registry.dispatch passes neither parent_agent nor turn_id to plain
    # tool handlers; the handler must recover the turn id from the map the
    # pre-LLM hook maintains, or the plan lands under a session key the
    # middleware never reads.
    with plugin._POLICY_LOCK:
        plugin._CURRENT_TURN["ctx-session"] = "ctx-session:task:abcd1234"
    sid, tid = plugin._resolve_turn_context(None, {"session_id": "ctx-session"})
    assert sid == "ctx-session"
    assert tid == "ctx-session:task:abcd1234"
    assert plugin._turn_key(sid, tid) == ("turn", "ctx-session:task:abcd1234")

    # Explicit parent_agent context still wins over the fallback map.
    p = parent()
    p.session_id = "ctx-session"
    p._current_turn_id = "ctx-session:task:ffff0000"
    sid2, tid2 = plugin._resolve_turn_context(p, {})
    assert (sid2, tid2) == ("ctx-session", "ctx-session:task:ffff0000")

    # No context anywhere: empty turn id degrades to the session key.
    with plugin._POLICY_LOCK:
        plugin._CURRENT_TURN.pop("ctx-orphan", None)
    sid3, tid3 = plugin._resolve_turn_context(None, {"session_id": "ctx-orphan"})
    assert (sid3, tid3) == ("ctx-orphan", "")


def test_forced_route_loop_breaker_bypasses_after_max_attempts():
    session_id = "loop-session"
    turn_id = "loop-turn"
    key = plugin._turn_key(session_id, turn_id)
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS.pop(key, None)
        plugin._FORCED_ROUTE_COUNTS.pop(key, None)

    def call():
        return plugin._llm_request_policy(
            {"messages": []},
            session_id=session_id,
            turn_id=turn_id,
            task_id="parent-task",
            platform="slack",
            api_mode="chat_completions",
        )

    # First _MAX_FORCED_ROUTE_ATTEMPTS calls keep forcing route_turn
    # (the plan is never stored, simulating the key-mismatch loop).
    for attempt in range(plugin._MAX_FORCED_ROUTE_ATTEMPTS):
        forced = call()
        assert forced is not None, f"attempt {attempt} should force route_turn"
        assert (
            forced["request"]["tool_choice"]["function"]["name"] == "route_turn"
        )

    # The next call gives up instead of forcing forever.
    assert call() is None
    with plugin._POLICY_LOCK:
        assert plugin._TURN_PLANS[key]["mode"] == "policy_bypass"
        assert key not in plugin._FORCED_ROUTE_COUNTS


def _pending_delegate_request(session_id, turn_id):
    return plugin._llm_request_policy(
        {"messages": []},
        session_id=session_id,
        turn_id=turn_id,
        task_id="parent-task",
        platform="slack",
        api_mode="chat_completions",
    )


def test_forced_delegate_loop_breaker_releases_after_max_attempts():
    plugin._ENFORCED_PLATFORMS = {"slack"}
    session_id, turn_id = "delegate-loop-session", "delegate-loop-turn"
    key = plugin._turn_key(session_id, turn_id)
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key] = {"mode": "single", "lanes": [], "dispatched": False}

    # The plan never dispatches (rejected tasks, or list/steer/stop calls).
    for attempt in range(plugin._MAX_FORCED_DELEGATE_ATTEMPTS):
        forced = _pending_delegate_request(session_id, turn_id)
        assert forced is not None, f"attempt {attempt} should force delegate_task"
        assert forced["request"]["tool_choice"]["function"]["name"] == "delegate_task"

    # Later requests are left alone so the parent can answer in text.
    assert _pending_delegate_request(session_id, turn_id) is None
    assert _pending_delegate_request(session_id, turn_id) is None
    with plugin._POLICY_LOCK:
        plan = plugin._TURN_PLANS[key]
        # The declared plan stays in force; only the forced tool choice is dropped.
        assert plan["mode"] == "single"
        assert plan["delegate_forcing_released"] is True
        assert plan["dispatched"] is False

    agent = SimpleNamespace(session_id=session_id, _current_turn_id=turn_id)
    assert plugin._delegate_forcing_exhausted(agent) is True
    blocked = plugin._pre_tool_policy(
        "terminal",
        session_id=session_id,
        turn_id=turn_id,
        task_id="parent-task",
        platform="slack",
    )
    assert blocked == {
        "action": "block",
        "message": plugin._DELEGATE_FORCING_RELEASED_NOTE,
    }
    for allowed in ("route_turn", "delegate_task"):
        assert plugin._pre_tool_policy(
            allowed,
            session_id=session_id,
            turn_id=turn_id,
            task_id="parent-task",
            platform="slack",
        ) is None


def test_released_undispatched_plan_header_does_not_claim_running_lanes():
    plugin._ENFORCED_PLATFORMS = {"slack"}
    session_id, turn_id = "delegate-header-session", "delegate-header-turn"
    key = plugin._turn_key(session_id, turn_id)
    lane = {"label": "환불액 계산", "model": "claude-opus-5", "reasoning_effort": "high"}
    with plugin._POLICY_LOCK:
        plugin._SKIP_HEADER_SESSIONS.discard(session_id)
        plugin._TURN_PLANS[key] = {"mode": "single", "lanes": [lane], "dispatched": False}

    def header():
        return plugin._transform_header(
            "위임이 거부되어 실행하지 못했습니다.",
            session_id=session_id,
            model="claude-opus-5",
            turn_id=turn_id,
        ).splitlines()[0]

    # While the dispatch is still pending the declared lane reads as running.
    assert "환불액 계산" in header() and "실행 중" in header()
    for _ in range(plugin._MAX_FORCED_DELEGATE_ATTEMPTS + 1):
        _pending_delegate_request(session_id, turn_id)
    released = header()
    assert released.endswith("· 위임 미실행_")
    assert "실행 중" not in released and "환불액 계산" not in released

    # A dispatch after the release is reported as the lane again.
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key]["dispatched"] = True
    assert "환불액 계산" in header() and "위임 미실행" not in header()


def test_forced_delegate_counter_restarts_with_a_new_plan_and_stops_on_dispatch():
    plugin._ENFORCED_PLATFORMS = {"slack"}
    session_id, turn_id = "delegate-reset-session", "delegate-reset-turn"
    key = plugin._turn_key(session_id, turn_id)
    agent = SimpleNamespace(session_id=session_id, _current_turn_id=turn_id)
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key] = {"mode": "parallel", "lanes": [], "dispatched": False}
    for _ in range(plugin._MAX_FORCED_DELEGATE_ATTEMPTS):
        assert _pending_delegate_request(session_id, turn_id) is not None
    assert _pending_delegate_request(session_id, turn_id) is None

    # route_turn stores a fresh plan dict, which is forced again from zero.
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key] = {"mode": "single", "lanes": [], "dispatched": False}
    assert plugin._delegate_forcing_exhausted(agent) is False
    forced = _pending_delegate_request(session_id, turn_id)
    assert forced["request"]["tool_choice"]["function"]["name"] == "delegate_task"

    # A dispatched plan is neither forced nor counted as exhausted.
    with plugin._POLICY_LOCK:
        plan = plugin._TURN_PLANS[key]
        plan["forced_delegate_attempts"] = plugin._MAX_FORCED_DELEGATE_ATTEMPTS
        plan["dispatched"] = True
    assert _pending_delegate_request(session_id, turn_id) is None
    assert plugin._delegate_forcing_exhausted(agent) is False
    with plugin._POLICY_LOCK:
        assert "delegate_forcing_released" not in plugin._TURN_PLANS[key]


def test_accepted_plan_clears_forced_route_counter():
    key = plugin._turn_key("clear-session", "clear-turn")
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS.pop(key, None)
        plugin._FORCED_ROUTE_COUNTS[key] = 2
        # Simulate what route_handler does on plan acceptance.
        plugin._TURN_PLANS[key] = {
            "mode": "direct",
            "lanes": [],
            "dispatched": False,
        }
        plugin._FORCED_ROUTE_COUNTS.pop(key, None)
    result = plugin._llm_request_policy(
        {"messages": []},
        session_id="clear-session",
        turn_id="clear-turn",
        task_id="parent-task",
        platform="slack",
        api_mode="chat_completions",
    )
    # Direct plan present: no forcing, no bypass, model answers freely.
    assert result is None
    with plugin._POLICY_LOCK:
        assert plugin._TURN_PLANS[key]["mode"] == "direct"


class FakeCtx:
    """Minimal plugin context capturing registered tools and hooks."""

    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.middleware = {}
        self.unload = None
        self.state = FakeState()

    def get_config(self, key, default=None):
        return settings(key, default)

    def register_tool(self, *, name, handler, **kw):
        self.tools[name] = handler
        return object()

    def register_hook(self, kind, fn):
        self.hooks[kind] = fn

    def register_middleware(self, kind, fn):
        self.middleware[kind] = fn

    def on_unload(self, fn):
        self.unload = fn


@pytest.fixture()
def registered_plugin():
    ctx = FakeCtx()
    try:
        plugin.register(ctx)
    except ModuleNotFoundError as exc:
        pytest.skip(f"installed Hermes dependencies unavailable here: {exc}")
    try:
        yield ctx
    finally:
        if ctx.unload is not None:
            ctx.unload()


def test_redundant_route_turn_cannot_clobber_completion_plan(registered_plugin):
    ctx = registered_plugin
    route_handler = ctx.tools["route_turn"]
    session_id = "completion-session"
    turn_id = "completion-turn"
    key = plugin._turn_key(session_id, turn_id)
    completion_plan = {
        "mode": "completion",
        "reason": "background delegation completion",
        "lanes": [],
        "actual_routes": [
            {
                "label": "글자 수 계산",
                "requested_model": "gpt-5.6-luna-900k",
                "requested_reasoning_effort": "low",
                "actual_model": "gpt-5.6-luna-900k",
                "actual_reasoning_effort": "low",
                "status": "completed",
                "completed": True,
            }
        ],
        "dispatched": True,
        "created_at": 0.0,
    }
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key] = dict(completion_plan)
        plugin._CURRENT_TURN[session_id] = turn_id
        plugin._SKIP_HEADER_SESSIONS.discard(session_id)

    # Redundant route_turn (as the model habitually emits) must not replace
    # the completion plan — regardless of the args it sends.
    result = json.loads(
        route_handler(
            {"mode": "direct", "reason": "결과 전달", "lanes": []},
            session_id=session_id,
        )
    )
    assert result["status"] == "already_routed"
    assert result["mode"] == "completion"
    with plugin._POLICY_LOCK:
        assert plugin._TURN_PLANS[key]["mode"] == "completion"

    # The final header therefore still attributes the actual delegated lane.
    header = plugin._transform_header(
        "본문",
        session_id=session_id,
        model="gpt-5.6-sol-900k",
    )
    assert "글자 수 계산: Luna-low" in header
    assert "직접 처리" not in header


def test_main_effort_is_observed_from_request_payload(registered_plugin):
    ctx = registered_plugin
    route_handler = ctx.tools["route_turn"]
    session_id = "effort-session"
    turn_id = "effort-turn"
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS.pop(plugin._turn_key(session_id, turn_id), None)
        plugin._CURRENT_TURN[session_id] = turn_id
        plugin._SKIP_HEADER_SESSIONS.discard(session_id)
        plugin._MAIN_EFFORTS.pop(session_id, None)

    # The parent's own LLM request payload carries the effort (codex shape).
    plugin._llm_request_policy(
        {"input": [], "reasoning": {"effort": "medium"}},
        session_id=session_id,
        turn_id=turn_id,
        task_id="parent-task",
        platform="slack",
        api_mode="codex_responses",
    )
    with plugin._POLICY_LOCK:
        assert plugin._MAIN_EFFORTS[session_id] == "medium"

    # route_turn without a parent_agent picks the observed effort up.
    result = json.loads(
        route_handler(
            {"mode": "direct", "reason": "간단 응답", "lanes": []},
            session_id=session_id,
        )
    )
    assert result["status"] == "accepted"
    header = plugin._transform_header(
        "본문",
        session_id=session_id,
        model="gpt-5.6-sol-900k",
    )
    assert header.startswith("_Alex: Sol-medium · 직접 처리_")


def test_extract_request_effort_shapes():
    assert plugin._extract_request_effort({"reasoning_effort": "High"}) == "high"
    assert (
        plugin._extract_request_effort({"reasoning": {"effort": "low"}}) == "low"
    )
    assert (
        plugin._extract_request_effort(
            {"extra_body": {"reasoning": {"effort": "xhigh"}}}
        )
        == "xhigh"
    )
    assert plugin._extract_request_effort({"input": []}) == ""


def test_header_is_not_prepended_to_silence_markers():
    session_id = "silent-session"
    with plugin._POLICY_LOCK:
        plugin._SKIP_HEADER_SESSIONS.discard(session_id)
        plugin._CURRENT_TURN[session_id] = "silent-turn"
        plugin._TURN_PLANS[plugin._turn_key(session_id, "silent-turn")] = {
            "mode": "completion",
            "actual_routes": [],
            "dispatched": True,
        }
    for marker in ("NO_REPLY", "[SILENT]", "  NO_REPLY  "):
        assert plugin._transform_header(
            marker, session_id=session_id, model="gpt-5.6-sol-900k"
        ) is None
    # Substantive prose that merely mentions the marker still gets a header.
    result = plugin._transform_header(
        "NO_REPLY 처리 방식을 설명하면 다음과 같습니다.",
        session_id=session_id,
        model="gpt-5.6-sol-900k",
    )
    assert result is not None and result.startswith("_Alex:")


def test_delegated_acceptance_carries_interim_status_note(registered_plugin):
    ctx = registered_plugin
    route_handler = ctx.tools["route_turn"]
    session_id = "note-session"
    with plugin._POLICY_LOCK:
        plugin._CURRENT_TURN[session_id] = "note-turn"
        plugin._TURN_PLANS.pop(plugin._turn_key(session_id, "note-turn"), None)
    accepted = json.loads(
        route_handler(
            {
                "mode": "single",
                "reason": "독립 실행 필요",
                "lanes": [
                    {
                        "label": "자료수집",
                        "work_type": "mechanical",
                        "model": "gpt-5.6-luna-900k",
                        "reasoning_effort": "low",
                        "toolsets": ["web"],
                    }
                ],
            },
            session_id=session_id,
        )
    )
    assert accepted["status"] == "accepted"
    assert "one-line interim status" in accepted["note"]

    # Direct acceptance carries no interim-status note.
    with plugin._POLICY_LOCK:
        plugin._CURRENT_TURN[session_id] = "note-turn-2"
        plugin._TURN_PLANS.pop(plugin._turn_key(session_id, "note-turn-2"), None)
    direct = json.loads(
        route_handler(
            {"mode": "direct", "reason": "즉답 가능", "lanes": []},
            session_id=session_id,
        )
    )
    assert direct["status"] == "accepted"
    assert "note" not in direct


def test_schema_prefers_direct_over_trivial_delegation_and_limits_astra():
    desc = plugin.ROUTE_TURN_SCHEMA["description"]
    assert "never delegate a question you can answer immediately" in desc
    assert "When uncertain whether delegation is worth it, choose direct" in desc
    assert "exceptional architecture" in desc
    assert "mechanical delegated work" not in desc or "work_type" in desc
    assert "Verification MUST use gpt-6.1-sol" in desc
    assert "any allowlisted model" in desc


def test_delegation_phase_contract_is_always_worker():
    key = plugin._turn_key("session", "legacy-verification-turn")
    with plugin._POLICY_LOCK:
        plugin._TURN_PLANS[key] = {"mode": "verification", "lanes": []}
    try:
        assert plugin.delegation_phase_for_turn("session", "legacy-verification-turn") == "worker"
        assert plugin.delegation_phase_for_turn("missing", "turn") == "worker"
    finally:
        with plugin._POLICY_LOCK:
            plugin._TURN_PLANS.pop(key, None)


def _completion_db(tmp_path, state="completed", owner="parent", delivery="parent", compressed=False):
    db=tmp_path / "state.db"; con=sqlite3.connect(db)
    con.execute("CREATE TABLE async_delegations (delegation_id TEXT, state TEXT, parent_session_id TEXT, event_json TEXT)")
    con.execute("CREATE TABLE sessions (id TEXT, parent_session_id TEXT, end_reason TEXT, model_config TEXT, source TEXT, ended_at REAL, last_activity_at REAL, started_at REAL)")
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",(owner,None,"compression" if compressed else None,"{}","slack",None,1,1))
    if compressed:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",(delivery,owner,None,"{}","slack",None,2,2))
    event={"results":[{"status":"timeout" if state=="error" else state,"routing":{"label":"조사","requested_model":"gpt-5.6-terra-900k","requested_reasoning_effort":"high"}}]}
    con.execute("INSERT INTO async_delegations VALUES (?,?,?,?)",("deleg-test",state,owner,json.dumps(event)))
    con.commit();con.close();return db


@pytest.mark.parametrize("state",["completed","error","unknown"])
def test_terminal_completion_states_preserve_routes(monkeypatch,tmp_path,state):
    _completion_db(tmp_path,state=state)
    import hermes_constants
    monkeypatch.setattr(hermes_constants,"get_hermes_home",lambda:tmp_path)
    routes=plugin._verified_completion_routes("deleg-test","parent")
    assert routes and routes[0]["label"]=="조사"
    assert routes[0]["status"] in {state,"timeout"}


def test_compression_tip_is_owned_but_unrelated_session_is_rejected(monkeypatch,tmp_path):
    _completion_db(tmp_path,owner="parent",delivery="compressed-tip",compressed=True)
    import hermes_constants
    monkeypatch.setattr(hermes_constants,"get_hermes_home",lambda:tmp_path)
    assert plugin._verified_completion_routes("deleg-test","compressed-tip")
    assert plugin._verified_completion_routes("deleg-test","attacker")==[]


def test_transform_prefers_context_local_turn_over_global_interleaving():
    sid="same-session";local_key=plugin._turn_key(sid,"local-turn")
    other_key=plugin._turn_key(sid,"other-turn")
    with plugin._POLICY_LOCK:
        plugin._SKIP_HEADER_SESSIONS.discard(sid)
        plugin._TURN_PLANS[local_key]={"mode":"direct","main_reasoning_effort":"high"}
        plugin._TURN_PLANS[other_key]={"mode":"policy_error","main_reasoning_effort":"medium"}
        plugin._CURRENT_TURN[sid]="other-turn"
    token=plugin._ACTIVE_TURN_ID.set((sid,"local-turn"))
    try:
        out=plugin._transform_header("본문",sid,"gpt-5.6-sol-900k")
        assert "직접 처리" in out and "실행경로 미검증" not in out
    finally:
        plugin._ACTIVE_TURN_ID.reset(token)


def test_policy_error_uses_precise_unverified_label():
    sid="unverified";key=plugin._turn_key(sid,"turn")
    with plugin._POLICY_LOCK:
        plugin._SKIP_HEADER_SESSIONS.discard(sid)
        plugin._TURN_PLANS[key]={"mode":"policy_error","actual_routes":[]}
        plugin._CURRENT_TURN[sid]="turn"
    out=plugin._transform_header("본문",sid,"gpt-5.6-sol-900k",turn_id="turn")
    assert "위임 기록 만료 · 실행경로 미검증" in out
    assert "실행정보 확인 실패" not in out


def test_route_turn_schema_has_no_automatic_verification():
    description = plugin.ROUTE_TURN_SCHEMA["description"]
    assert "No verification stage runs automatically" in description
    assert "tests in the same worker lane" in description
    assert "not implementation plus its tests" in description
    assert "only when the user explicitly asks" in description
    # A dependent review is never planned alongside the work it reviews.
    assert "never put a review in the same parallel plan as that work" in description
    assert "delegate the work only" in description
    assert "requested in a follow-up message" in description
    for removed in ("worker_verifier", "development_review", "review_task_id", "phase=verifier"):
        assert removed not in description
    properties = plugin.ROUTE_TURN_SCHEMA["parameters"]["properties"]
    assert properties["mode"]["enum"] == ["direct", "single", "parallel"]
    assert "review_task_id" not in properties
    lane_items = properties["lanes"]["items"]
    assert "phase" not in lane_items["properties"] and "phase" not in lane_items["required"]
    assert not hasattr(plugin, "_review_action")
    assert not hasattr(plugin, "_worker_verifier_requirement")


def test_skill_refresh_uses_direct_before_reclassifying_work():
    description = plugin.ROUTE_TURN_SCHEMA["description"]
    assert "skill refresh" in description
    assert "route_turn again before any write" in description


def test_localized_direct_route_allows_skill_read_and_final_verification(monkeypatch):
    monkeypatch.setattr(plugin, "_ENFORCED_PLATFORMS", {"slack"})
    key = plugin._turn_key("localized-figma-test", "turn-local")
    monkeypatch.setitem(plugin._TURN_PLANS, key, {"mode": "direct", "lanes": [], "dispatched": False})
    context = dict(session_id="localized-figma-test", turn_id="turn-local",
                   task_id="parent-task", platform="slack")
    for tool in ("skill_view", "mcp__figma__use_figma", "mcp__figma__get_screenshot"):
        assert plugin._pre_tool_policy(tool, **context) is None
    assert plugin._llm_request_policy({"messages": []}, api_mode="chat_completions", **context) is None


@pytest.fixture
def captured_core_entrypoint(monkeypatch):
    """Install over a captured native original, never replace the patched entrypoint."""
    import copy
    import functools
    import tools.delegate_tool as native

    captured = {}
    original = native.delegate_task

    @functools.wraps(original)
    def core_original(*args, **kwargs):
        tasks = kwargs.get("tasks", args[2] if len(args) >= 3 else None)
        if isinstance(tasks, str):
            tasks = json.loads(tasks)
        captured.update(tasks=copy.deepcopy(tasks))
        return json.dumps({"delegation_id": "deleg-1"})

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
    sid, turn, done = f"owner-{entry}", f"dispatch-{entry}", f"complete-{entry}"
    plugin._SKIP_HEADER_SESSIONS.discard(sid)
    p = parent()
    p.session_id, p._current_turn_id, p.platform, p._delegate_depth = sid, turn, "slack", 0
    lane = {"label": "구현", "work_type": "implementation", "model": "claude-opus-5",
            "reasoning_effort": "high", "toolsets": ["file"]}
    route = json.loads(ctx.tools["route_turn"]({"mode": "single", "reason": "bounded work",
                                               "lanes": [lane]}, parent_agent=p))
    assert route["status"] == "accepted" and route["dispatch_lanes"] == [lane]
    assert "deferred_verifier_lanes" not in route
    tasks = [{"goal": "do the work", "label": "구현", "model": lane["model"],
              "reasoning_effort": lane["reasoning_effort"], "toolsets": lane["toolsets"],
              "acp_command": "must be stripped"}]
    stripped = native._strip_model_hidden_task_fields(tasks)
    assert native.delegate_task.__name__ == "routed_delegate_task"
    if entry == "registry":
        raw = ctx.tools["delegate_task"]({"tasks": tasks}, parent_agent=p)
    elif entry == "positional":
        raw = native.delegate_task(None, None, stripped, parent_agent=p)
    else:
        raw = native.delegate_task(tasks=json.dumps(stripped) if entry == "json" else stripped,
                                   parent_agent=p)
    assert json.loads(raw)["delegation_id"] == "deleg-1"
    # The worker goal reaches core unchanged: no reviewer goal or schema is injected.
    assert captured["tasks"][0]["goal"] == "do the work"
    assert "output_schema" not in captured["tasks"][0]
    assert "acp_command" not in captured["tasks"][0]
    plan = plugin._TURN_PLANS[plugin._turn_key(sid, turn)]
    assert plan["dispatched"] is True and plan["delegation_id"] == "deleg-1"
    context = dict(session_id=sid, platform="slack", task_id="parent")
    assert plugin._llm_request_policy({"input": []}, turn_id=turn,
                                      api_mode="codex_responses", **context) is None
    event = {"results": [{"status": "completed", "routing": {
        "label": "구현", "requested_model": "claude-opus-5", "requested_reasoning_effort": "high",
        "actual_model": "claude-opus-5", "actual_reasoning_effort": "high"}}]}
    with sqlite3.connect(tmp_path / "state.db") as con:
        con.execute("CREATE TABLE async_delegations (delegation_id TEXT, state TEXT, parent_session_id TEXT, event_json TEXT)")
        con.execute("INSERT INTO async_delegations VALUES (?,?,?,?)",
                    ("deleg-1", "completed", sid, json.dumps(event)))
    plugin._pre_llm_policy(user_message="[ASYNC DELEGATION COMPLETE — deleg-1]", turn_id=done, **context)
    final = plugin._TURN_PLANS[plugin._turn_key(sid, done)]
    assert final["mode"] == "completion" and final["owned_delegation_ids"] == ["deleg-1"]
    assert plugin._llm_request_policy({"input": []}, turn_id=done,
                                      api_mode="codex_responses", **context) is None
    out = plugin._transform_header("final complete", sid, "gpt-6.1-sol", turn_id=done)
    assert "구현: claude-opus-5-high" in out.split("\n\n", 1)[0]
    assert out.endswith("\n\nfinal complete") and "검토" not in out
