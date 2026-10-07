"""Six-model policy tests: hermetic providers, no gateway/session-store access.

Filename retained from the original five-model regression suite.
"""
from __future__ import annotations

import copy
import json

import pytest

from test_routing import (
    _install_capturing_creds, anthropic_parent, fake_module, parent, plugin,
    routed_task, settings,
)

MODELS = [
    "claude-opus-4-8", "claude-fable-5.1", "gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna",
    "claude-sonnet-5",
]
PEERS = [
    ("gpt-6-luna", "claude-sonnet-5"),
    ("gpt-6.1-sol", "claude-opus-4-8"),
    ("gpt-6-astra", "claude-fable-5.1"),
]


def six_settings(key, default=None):
    return MODELS if key == "allowed_models" else settings(key, default)


def build_captured(monkeypatch, model, parent_agent, *, allowed=MODELS, **overrides):
    """Exercise the installed builder shim, not a duplicated routing algorithm."""
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


def test_default_roster_and_delegate_schema_are_exact():
    assert list(plugin.DEFAULT_ALLOWED_MODELS) == MODELS
    schema = plugin._make_schema(
        fake_module(), allowed_models=MODELS,
        allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"),
    )
    assert plugin._task_item_properties(schema)["model"]["enum"] == MODELS
    desc = plugin.ROUTE_TURN_SCHEMA["description"]
    assert all(model in desc for model in MODELS)
    assert "Terra" not in desc
    assert len(MODELS) == len(set(MODELS)) == 6
    assert "general/medium Claude alternative to Sol" in desc
    assert "Sol) at medium/high is the general default" in desc
    assert "critical independent verification" in desc
    assert "Luna→Sonnet" in desc and "Sol→Opus" in desc and "Astra→Fable" in desc
    assert "cost-appropriate outage recovery" in desc
    assert plugin.ROUTE_TURN_SCHEMA["parameters"]["properties"]["mode"]["enum"] == [
        "direct", "single", "parallel", "worker_verifier",
    ]


@pytest.mark.parametrize("model,peer", PEERS)
def test_exact_peer_mapping(model, peer):
    assert plugin._anthropic_peer_for(model) == peer


@pytest.mark.parametrize("model", [
    "gpt-6-unknown", "gpt-6", "gpt-6-luna-preview", "gpt-5.6-terra-900k",
    "claude-sonnet-5", "claude-opus-4-8", "", "GPT-6-LUNA", " gpt-6.1-sol", "gpt-6-sol",
])
def test_no_guessed_or_normalized_peer(model):
    assert plugin._anthropic_peer_for(model) is None


@pytest.mark.parametrize("model,peer", PEERS)
@pytest.mark.parametrize("cross_provider", [False, True])
def test_fallback_independent_of_parent_provider(monkeypatch, model, peer, cross_provider):
    p = anthropic_parent() if cross_provider else parent()
    captured = build_captured(monkeypatch, model, p)
    assert captured["routing_cfg"]["fallback_providers"] == [
        {"provider": "anthropic", "model": peer},
    ]
    assert bool(captured.get("override_provider")) is cross_provider
    if cross_provider:
        assert captured["override_provider"] == "openai-codex"
        assert captured["override_api_mode"] == "codex_responses"
        assert captured["override_base_url"] == "https://chatgpt.com/backend-api/codex"


@pytest.mark.parametrize("model,peer", PEERS)
def test_unapproved_peer_disables_fallback(monkeypatch, model, peer):
    captured = build_captured(monkeypatch, model, parent(), allowed=[model])
    assert captured["routing_cfg"]["fallback_providers"] == []


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-4-8", "claude-fable-5.1", "gpt-6-unknown"])
def test_no_invented_recovery_for_anthropic_or_unknown_models(monkeypatch, model):
    p = anthropic_parent() if model.startswith("claude") else parent()
    captured = build_captured(monkeypatch, model, p, allowed=[*MODELS, model])
    assert captured["routing_cfg"]["fallback_providers"] == []


@pytest.mark.parametrize("override", [
    {"override_provider": "openai-codex"},
    {"override_base_url": "https://operator.invalid"},
    {"override_api_key": "test-only-credential"},
    {"override_api_mode": "codex_responses"},
    {"override_acp_command": "operator-command"},
])
def test_trusted_overrides_do_not_gain_implicit_fallback(monkeypatch, override):
    captured = build_captured(monkeypatch, "gpt-6.1-sol", parent(), **override)
    assert all(captured[key] == value for key, value in override.items())
    assert captured["routing_cfg"]["fallback_providers"] == []


@pytest.mark.parametrize("chain", [[], [{"provider": "anthropic", "model": "claude-opus-4-8"}],
                                    [{"provider": "anthropic", "model": "claude-sonnet-5"}]])
def test_explicit_chain_and_disable_are_preserved_without_shared_mutation(monkeypatch, chain):
    cfg = {"fallback_providers": chain, "max_iterations": 9}
    before = copy.deepcopy(cfg)
    captured = build_captured(monkeypatch, "gpt-6-astra", parent(), routing_cfg=cfg)
    assert captured["routing_cfg"] == before
    assert captured["routing_cfg"] is not cfg
    assert captured["routing_cfg"]["fallback_providers"] is not chain
    assert cfg == before


@pytest.mark.parametrize("chain", [
    [{"provider": "anthropic", "model": "claude-unapproved"}],
    [{"provider": "openai-codex", "model": "claude-opus-4-8"}],
    [{"model": "claude-opus-4-8"}],
    [{"provider": "anthropic"}],
    ["claude-opus-4-8"],
    "claude-opus-4-8",
])
def test_bad_explicit_fallback_fails_before_build(monkeypatch, chain):
    with pytest.raises(ValueError, match="fallback"):
        build_captured(
            monkeypatch, "gpt-6.1-sol", parent(),
            routing_cfg={"fallback_providers": chain},
        )


@pytest.mark.parametrize("model", ["claude-unapproved", "gpt-5.6-terra-900k", "gpt-6-sol", "gpt-6-unknown"])
def test_retired_or_unknown_primary_is_rejected(model):
    module = fake_module()
    unload = plugin._install_patches(module, get_config=six_settings)
    try:
        result = json.loads(module.delegate_task(tasks=[routed_task(model=model)], parent_agent=parent()))
        assert "allowed_models" in result["error"]
    finally:
        unload()


@pytest.mark.parametrize("model,peer", PEERS)
def test_installed_native_runtime_retains_approved_chain_for_pinned_model(model, peer):
    # Calls the installed core normalizer; no provider initialization or API call.
    from tools.delegate_tool_config import _resolve_child_fallback_chain
    p = parent()
    p._fallback_chain = [{"provider": "anthropic", "model": "claude-sonnet-5"}]
    cfg = plugin._routed_fallback_config(model, MODELS, {}, automatic_peer=True)
    chain = _resolve_child_fallback_chain(p, cfg, pinned=True)
    assert chain and len(chain) == 1
    assert chain[0]["provider"] == "anthropic" and chain[0]["model"] == peer
    cfg = plugin._routed_fallback_config(model, [model], {}, automatic_peer=True)
    assert _resolve_child_fallback_chain(p, cfg, pinned=True) is None


@pytest.mark.parametrize("model", MODELS)
def test_all_six_primary_models_are_accepted_with_exact_metadata(monkeypatch, model):
    _install_capturing_creds(monkeypatch)
    module = fake_module()
    unload = plugin._install_patches(module, get_config=six_settings)
    try:
        result = json.loads(module.delegate_task(
            tasks=[routed_task(model=model, effort="medium")], parent_agent=parent(),
        ))
        routing = result["results"][0]["routing"]
        assert routing["requested_model"] == routing["actual_model"] == model
        assert routing["actual_reasoning_effort"] == "medium"
    finally:
        unload()


def test_risk_policy_still_requires_separate_worker_and_verifier():
    kwargs = dict(
        allowed_models=MODELS, allowed_efforts=settings("allowed_reasoning_efforts"),
        allowed_toolsets=settings("allowed_toolsets"), require_worker_verifier=True,
    )
    with pytest.raises(ValueError, match="worker_verifier"):
        plugin._validate_plan({"mode": "direct", "reason": "security review", "lanes": []}, **kwargs)
    worker = {"phase": "worker", "label": "implement", "model": "gpt-6.1-sol",
              "reasoning_effort": "high", "toolsets": ["file"]}
    verifier = {"phase": "verifier", "label": "verify", "model": "claude-opus-4-8",
                "reasoning_effort": "high", "toolsets": ["file"]}
    plan = plugin._validate_plan({"mode": "worker_verifier", "reason": "security review",
                                  "lanes": [worker, verifier]}, **kwargs)
    assert plan["lanes"] == [worker, verifier]
    assert plan["expected_lanes"] == [worker]
