"""v0.4.0 fixed work types and ordered recovery; no live providers/store access."""
from __future__ import annotations

import copy
import json

import pytest

from test_routing import (
    FakeCtx, _install_capturing_creds, anthropic_parent, fake_module, parent,
    plugin, routed_task, settings,
)

MODELS = [
    "claude-opus-5", "claude-fable-5.1", "gpt-6-astra", "gpt-6.1-sol",
    "gpt-6-luna", "claude-sonnet-5",
]
CHAIN = ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna",
         "claude-opus-5", "claude-sonnet-5"]


def six_settings(key, default=None):
    return MODELS if key == "allowed_models" else settings(key, default)


def suffix_entries(model):
    suffix = CHAIN[CHAIN.index(model) + 1:] if model in CHAIN else []
    return [{"provider": "openai-codex" if m.startswith("gpt-") else "anthropic", "model": m}
            for m in suffix]


def build_captured(monkeypatch, model, parent_agent, *, allowed=MODELS, **overrides):
    _install_capturing_creds(monkeypatch)
    module = fake_module()
    captured = {}
    original = module._build_child_preserving_parent_tools

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    module._build_child_preserving_parent_tools = capture
    unload = plugin._install_patches(
        module, get_config=lambda key, default=None: (
            allowed if key == "allowed_models" else settings(key, default)
        ),
    )
    token = plugin._ACTIVE_ROUTES.set({0: {"model": model}})
    try:
        module._build_child_preserving_parent_tools(
            task_index=0, goal="test", context=None, toolsets=["file"],
            model=None, max_iterations=1, task_count=1,
            parent_agent=parent_agent, **overrides,
        )
    finally:
        plugin._ACTIVE_ROUTES.reset(token)
        unload()
    return captured


def lane(work_type="implementation", model="claude-opus-5", label="work"):
    return {"label": label, "work_type": work_type, "model": model,
            "reasoning_effort": "high", "toolsets": ["file"]}


def validate(lanes, *, mode="single", **options):
    payload = {"mode": mode, "reason": "bounded work", "lanes": lanes}
    return plugin._validate_plan(
        payload, allowed_models=MODELS, allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"), **options,
    )


def test_roster_schema_and_policy():
    assert list(plugin.DEFAULT_ALLOWED_MODELS) == MODELS
    assert list(plugin.DEFAULT_RECOVERY_CHAIN) == CHAIN
    assert plugin.DEFAULT_FALLBACK_MODELS == frozenset(CHAIN)
    schema = plugin._make_schema(fake_module(), allowed_models=MODELS,
                                allowed_efforts=settings("allowed_reasoning_efforts"),
                                allowed_toolsets=settings("allowed_toolsets"))
    assert plugin._task_item_properties(schema)["model"]["enum"] == MODELS
    assert not {"gpt-6-sol", "gpt-5.6-sol"} & set(MODELS)
    items = plugin.ROUTE_TURN_SCHEMA["parameters"]["properties"]["lanes"]["items"]
    assert "work_type" in items["required"]
    assert items["properties"]["work_type"]["enum"] == [
        "implementation", "research", "verification", "mechanical", "architecture"]
    desc = plugin.ROUTE_TURN_SCHEMA["description"]
    assert all(model in desc for model in MODELS + CHAIN)
    assert "MUST use claude-opus-5" in desc and "Verification MUST use gpt-6.1-sol" in desc
    assert "Sol remains the delegation default" not in desc


@pytest.mark.parametrize("work_type", ["implementation", "research"])
@pytest.mark.parametrize("model", MODELS)
def test_execution_and_research_fixed_model(work_type, model):
    additional = plugin.DEFAULT_ADDITIONAL_MODELS.get(work_type, ())
    if model == "claude-opus-5" or model in additional:
        assert validate([lane(work_type, model)])["lanes"][0]["model"] == model
    else:
        with pytest.raises(ValueError, match=f"work_type={work_type} requires model=claude-opus-5; use claude-opus-5"):
            validate([lane(work_type, model)])


@pytest.mark.parametrize("model", MODELS)
def test_requested_verification_lane_uses_fixed_model(model):
    if model == "gpt-6.1-sol":
        assert validate([lane("verification", model)])["lanes"][0]["model"] == model
    else:
        with pytest.raises(ValueError, match="requires model=gpt-6.1-sol; use gpt-6.1-sol"):
            validate([lane("verification", model)])


def test_requested_verification_lane_uses_configured_model():
    item = lane("verification", "claude-sonnet-5")
    plan = validate([item], fixed_models={"verification": "claude-sonnet-5"})
    assert plan["lanes"] == [item]


def test_worker_verifier_mode_is_removed():
    lanes = [lane(), lane("verification", "gpt-6.1-sol", "verify")]
    with pytest.raises(ValueError, match="mode must be direct, single, or parallel"):
        validate(lanes, mode="worker_verifier")
    assert validate(lanes, mode="parallel")["lanes"] == lanes


def test_legacy_phase_field_is_ignored():
    item = {**lane("verification", "gpt-6.1-sol"), "phase": "verifier"}
    plan = validate([item])
    assert plan["lanes"] == [lane("verification", "gpt-6.1-sol")]
    assert "expected_lanes" not in plan and "stage" not in plan


@pytest.mark.parametrize("work_type", ["mechanical", "architecture"])
@pytest.mark.parametrize("model", MODELS)
def test_free_choice_categories(work_type, model):
    assert validate([lane(work_type, model)])["lanes"][0]["model"] == model


@pytest.mark.parametrize("work_type", [None, "", "unknown", "Implementation", [], 1])
def test_work_type_required_and_exact(work_type):
    item = lane()
    if work_type is None:
        item.pop("work_type")
    else:
        item["work_type"] = work_type
    with pytest.raises(ValueError, match="work_type"):
        validate([item])


@pytest.mark.parametrize("work_type", ["implementation", "research", "verification"])
def test_fixed_model_override_and_partial_defaults(work_type):
    overrides = {work_type: "claude-sonnet-5"}
    assert validate([lane(work_type, "claude-sonnet-5")], fixed_models=overrides)["lanes"][0]["model"] == "claude-sonnet-5"
    assert plugin._fixed_model_policy(overrides) == {**plugin.DEFAULT_FIXED_MODELS, **overrides}
    with pytest.raises(ValueError, match="use claude-sonnet-5"):
        validate([lane(work_type, plugin.DEFAULT_FIXED_MODELS[work_type])], fixed_models=overrides)


@pytest.mark.parametrize("value", [[], "opus", {"mechanical": "gpt-6-luna"},
                                   {"implementation": ""}, {"research": None}, {"verification": " gpt-6.1-sol"}])
def test_malformed_fixed_model_config_fails_closed(value):
    with pytest.raises(ValueError, match="fixed_models"):
        plugin._fixed_model_policy(value)


@pytest.mark.parametrize("override", [None, {"implementation": "claude-sonnet-5", "verification": "gpt-6-luna"}])
def test_registered_route_uses_configuration_in_validator_and_guidance(monkeypatch, override):
    ctx = FakeCtx()
    ctx.get_config = lambda key, default=None: override if key == "fixed_models" else six_settings(key, default)
    registrations = {}
    original = ctx.register_tool

    def capture(**kwargs):
        registrations[kwargs["name"]] = kwargs
        return original(**kwargs)

    ctx.register_tool = capture
    plugin.register(ctx)
    try:
        fixed = plugin._fixed_model_policy(override)
        route = registrations["route_turn"]["schema"]
        props = route["parameters"]["properties"]["lanes"]["items"]["properties"]
        assert props["model"]["enum"] == MODELS
        assert f"implementation: {fixed['implementation']}" in route["description"]
        assert f"verification: {fixed['verification']}" in props["work_type"]["description"]
        for work_type, model in fixed.items():
            sid, tid = f"fixed-{work_type}", "fixed-turn"
            key = plugin._turn_key(sid, tid)
            monkeypatch.delitem(plugin._TURN_PLANS, key, raising=False)
            payload = {"mode": "single", "reason": "policy test", "lanes": [lane(work_type, model)]}
            result = json.loads(ctx.tools["route_turn"](payload, session_id=sid, turn_id=tid))
            assert result["status"] == "accepted"
            # Neither a fixed nor an additional model under either parametrization.
            payload["lanes"][0]["model"] = "claude-fable-5.1"
            rejected = json.loads(ctx.tools["route_turn"](payload, session_id=sid, turn_id="rejected-turn"))
            assert f"use {model}" in rejected["error"]
            assert plugin._turn_key(sid, "rejected-turn") not in plugin._TURN_PLANS
            p = parent()
            p.session_id, p._current_turn_id = sid, tid
            task = {k: v for k, v in lane(work_type, model).items() if k != "work_type"}
            task["goal"] = "test"
            assert plugin._validate_delegate_against_plan(p, [task])["lanes"][0]["work_type"] == work_type
            task["model"] = "claude-fable-5.1"
            with pytest.raises(ValueError, match="model does not match"):
                plugin._validate_delegate_against_plan(p, [task])
            plugin._TURN_PLANS.pop(key, None)
    finally:
        ctx.unload()


def test_default_additional_model_is_implementation_only():
    assert plugin.DEFAULT_ADDITIONAL_MODELS == {"implementation": ("gpt-6-astra",)}
    assert validate([lane("implementation", "gpt-6-astra")])["lanes"][0]["model"] == "gpt-6-astra"
    assert validate([lane("implementation", "claude-opus-5")])["lanes"][0]["model"] == "claude-opus-5"
    for work_type in ("research", "verification"):
        with pytest.raises(ValueError, match=f"work_type={work_type} requires model="):
            validate([lane(work_type, "gpt-6-astra")])


def test_additional_model_override_merges_and_empty_list_disables():
    with pytest.raises(ValueError, match="requires model=claude-opus-5"):
        validate([lane("implementation", "gpt-6-astra")], additional_models={"implementation": []})
    extra = {"research": ["claude-fable-5.1"]}
    assert validate([lane("research", "claude-fable-5.1")], additional_models=extra)["lanes"][0]["model"] == "claude-fable-5.1"
    # A partial override keeps the built-in implementation entry.
    assert validate([lane("implementation", "gpt-6-astra")], additional_models=extra)["lanes"][0]["model"] == "gpt-6-astra"
    with pytest.raises(ValueError, match="requires model=claude-opus-5"):
        validate([lane("research", "gpt-6-luna")], additional_models=extra)


def test_additional_model_still_needs_the_allowlist():
    payload = {"mode": "single", "reason": "bounded work", "lanes": [lane("implementation", "gpt-6-astra")]}
    with pytest.raises(ValueError, match="model is not allowed"):
        plugin._validate_plan(
            payload, allowed_models=["claude-opus-5"],
            allowed_efforts=settings("allowed_reasoning_efforts"),
            allowed_toolsets=settings("allowed_toolsets"),
        )


@pytest.mark.parametrize("value", [[], "astra", {"mechanical": ["gpt-6-luna"]},
                                   {"implementation": "gpt-6-astra"}, {"implementation": [""]},
                                   {"research": [None]}, {"verification": [" gpt-6-luna"]}])
def test_malformed_additional_model_config_fails_closed(value):
    with pytest.raises(ValueError, match="additional_models"):
        plugin._additional_model_policy(value)


def _register_with(config):
    ctx = FakeCtx()
    ctx.get_config = lambda key, default=None: config[key] if key in config else six_settings(key, default)
    registrations = {}
    original = ctx.register_tool

    def capture(**kwargs):
        registrations[kwargs["name"]] = kwargs
        return original(**kwargs)

    ctx.register_tool = capture
    plugin.register(ctx)
    return ctx, registrations["route_turn"]["schema"]


def test_registered_route_accepts_and_describes_additional_model(monkeypatch):
    ctx, route = _register_with({})
    try:
        guidance = "implementation: claude-opus-5 (gpt-6-astra only when the user names it)"
        assert guidance in route["description"]
        assert guidance in route["parameters"]["properties"]["lanes"]["items"]["properties"]["work_type"]["description"]
        assert "research: claude-opus-5;" in route["description"]
        sid, tid = "additional-implementation", "additional-turn"
        monkeypatch.delitem(plugin._TURN_PLANS, plugin._turn_key(sid, tid), raising=False)
        payload = {"mode": "single", "reason": "user named the model",
                   "lanes": [lane("implementation", "gpt-6-astra")]}
        result = json.loads(ctx.tools["route_turn"](payload, session_id=sid, turn_id=tid))
        assert result["status"] == "accepted"
        p = parent()
        p.session_id, p._current_turn_id = sid, tid
        task = {k: v for k, v in lane("implementation", "gpt-6-astra").items() if k != "work_type"}
        task["goal"] = "test"
        assert plugin._validate_delegate_against_plan(p, [task])["lanes"][0]["model"] == "gpt-6-astra"
        payload["lanes"][0]["work_type"] = "research"
        rejected = json.loads(ctx.tools["route_turn"](payload, session_id=sid, turn_id="rejected-turn"))
        assert "use claude-opus-5" in rejected["error"]
        plugin._TURN_PLANS.pop(plugin._turn_key(sid, tid), None)
    finally:
        ctx.unload()


def test_registered_route_drops_default_additional_model_outside_allowlist():
    narrowed = [m for m in MODELS if m != "gpt-6-astra"]
    ctx, route = _register_with({"allowed_models": narrowed})
    try:
        assert "gpt-6-astra only when" not in route["description"]
        payload = {"mode": "single", "reason": "policy test",
                   "lanes": [lane("implementation", "gpt-6-astra")]}
        rejected = json.loads(ctx.tools["route_turn"](payload, session_id="narrowed", turn_id="narrowed-turn"))
        assert "use claude-opus-5" in rejected["error"]
    finally:
        ctx.unload()


def test_unapproved_additional_override_registration_fails_and_unloads():
    import tools.delegate_tool as native
    original = native.delegate_task
    ctx = FakeCtx()
    ctx.get_config = lambda key, default=None: ({"implementation": ["unapproved"]} if key == "additional_models" else six_settings(key, default))
    with pytest.raises(ValueError, match="additional_models.implementation=unapproved must be in allowed_models"):
        plugin.register(ctx)
    assert native.delegate_task is original


def test_unapproved_fixed_override_registration_fails_and_unloads():
    import tools.delegate_tool as native
    original = native.delegate_task
    ctx = FakeCtx()
    ctx.get_config = lambda key, default=None: ({"implementation": "unapproved"} if key == "fixed_models" else six_settings(key, default))
    with pytest.raises(ValueError, match="fixed_models.implementation=unapproved must be in allowed_models"):
        plugin.register(ctx)
    assert native.delegate_task is original


@pytest.mark.parametrize("model", CHAIN)
def test_exact_chain_suffix(model):
    assert list(plugin._recovery_models_after(model)) == CHAIN[CHAIN.index(model) + 1:]
    cfg = plugin._routed_fallback_config(model, MODELS, {}, automatic_recovery=True)
    assert cfg["fallback_providers"] == suffix_entries(model)


@pytest.mark.parametrize("model", ["gpt-6-astra", "claude-fable-5.1", "gpt-6-unknown", "gpt-6-luna-preview", "", "GPT-6-LUNA", " gpt-6.1-sol"])
def test_no_guessed_or_normalized_recovery(model):
    assert plugin._recovery_models_after(model) == ()


@pytest.mark.parametrize("model", CHAIN)
@pytest.mark.parametrize("parent_provider", ["openai-codex", "anthropic"])
def test_builder_chain_independent_of_parent_provider(monkeypatch, model, parent_provider):
    p = parent() if parent_provider == "openai-codex" else anthropic_parent()
    captured = build_captured(monkeypatch, model, p)
    assert captured["routing_cfg"]["fallback_providers"] == suffix_entries(model)
    target = "openai-codex" if model.startswith("gpt-") else "anthropic"
    assert bool(captured.get("override_provider")) == (target != parent_provider)
    if target != parent_provider:
        assert captured["override_provider"] == target
        assert captured["override_api_mode"] == ("codex_responses" if target == "openai-codex" else "anthropic_messages")


@pytest.mark.parametrize("model", CHAIN)
def test_recovery_allowlist_separate_from_primary(monkeypatch, model):
    captured = build_captured(monkeypatch, model, parent(), allowed=[model])
    assert captured["routing_cfg"]["fallback_providers"] == suffix_entries(model)


@pytest.mark.parametrize("override", [
    {"override_provider": "openai-codex"}, {"override_base_url": "https://operator.invalid"},
    {"override_api_key": "test-only-credential"}, {"override_api_mode": "codex_responses"},
    {"override_acp_command": "operator-command"}, {"override_acp_args": ["operator-arg"]},
])
def test_trusted_overrides_do_not_gain_implicit_fallback(monkeypatch, override):
    captured = build_captured(monkeypatch, "gpt-6.1-sol", parent(), **override)
    assert all(captured[key] == value for key, value in override.items())
    assert captured["routing_cfg"]["fallback_providers"] == []


@pytest.mark.parametrize("chain", [[], suffix_entries("gpt-6.1-sol"),
                                    [{"provider": "anthropic", "model": "claude-sonnet-5"}]])
def test_explicit_chain_preserved_without_mutation_even_with_override(monkeypatch, chain):
    cfg = {"fallback_providers": chain, "max_iterations": 9}
    before = copy.deepcopy(cfg)
    captured = build_captured(monkeypatch, "gpt-6-astra", parent(), routing_cfg=cfg,
                              override_provider="openai-codex")
    assert captured["routing_cfg"] == before and cfg == before
    assert captured["routing_cfg"] is not cfg
    assert captured["routing_cfg"]["fallback_providers"] is not chain


@pytest.mark.parametrize("chain", [
    [{"provider": "anthropic", "model": "claude-unapproved"}],
    [{"provider": "openai-codex", "model": "claude-opus-5"}],
    [{"provider": "anthropic", "model": "gpt-6-sol"}],
    [{"model": "claude-opus-5"}], [{"provider": "anthropic"}],
    ["claude-opus-5"], "claude-opus-5", [{"provider": "openai-codex", "model": " gpt-6-sol"}],
])
def test_bad_explicit_fallback_fails_before_build(monkeypatch, chain):
    with pytest.raises(ValueError, match="fallback"):
        build_captured(monkeypatch, "gpt-6.1-sol", parent(), routing_cfg={"fallback_providers": chain})


@pytest.mark.parametrize("model", ["claude-unapproved", "gpt-5.6-terra-900k", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-unknown"])
def test_fallback_only_or_unapproved_primary_is_rejected(model):
    module = fake_module()
    unload = plugin._install_patches(module, get_config=six_settings)
    try:
        result = json.loads(module.delegate_task(tasks=[routed_task(model=model)], parent_agent=parent()))
        assert "allowed_models" in result["error"]
    finally:
        unload()


@pytest.mark.parametrize("model", CHAIN)
def test_installed_core_preserves_every_chain_suffix_and_disables_empty(model):
    from tools.delegate_tool_config import _resolve_child_fallback_chain
    p = parent()
    p._fallback_chain = [{"provider": "anthropic", "model": "claude-sonnet-5"}]
    cfg = plugin._routed_fallback_config(model, [model], {}, automatic_recovery=True)
    chain = _resolve_child_fallback_chain(p, cfg, pinned=True)
    assert (chain or []) == suffix_entries(model)
    assert _resolve_child_fallback_chain(p, {"fallback_providers": []}, pinned=True) is None


@pytest.mark.parametrize("model", MODELS)
def test_all_primary_models_accepted_with_exact_metadata(monkeypatch, model):
    _install_capturing_creds(monkeypatch)
    module = fake_module()
    unload = plugin._install_patches(module, get_config=six_settings)
    try:
        result = json.loads(module.delegate_task(tasks=[routed_task(model=model, effort="medium")], parent_agent=parent()))
        routing = result["results"][0]["routing"]
        assert routing["requested_model"] == routing["actual_model"] == model
        assert routing["actual_reasoning_effort"] == "medium"
    finally:
        unload()
