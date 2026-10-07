"""Installed-core multi-step recovery consumption, with hermetic credential clients."""
from types import SimpleNamespace

import pytest

from test_routing import plugin


@pytest.mark.parametrize("unavailable", [set(), {"gpt-6-sol"}, {"gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna"}])
def test_core_walks_ordered_chain_and_resolves_each_provider(monkeypatch, unavailable):
    import agent.chat_completion_helpers as core
    import agent.auxiliary_client as auxiliary
    import agent.fallback_cooldown as cooldown
    import agent.client_lifecycle as lifecycle
    import agent.agent_runtime_helpers as runtime
    import agent.native_compaction as compaction

    requested = []
    switched = []

    def resolve(provider, *, model, **kwargs):
        requested.append((provider, model))
        assert kwargs["raw_codex"] is True
        assert kwargs["explicit_api_key"] is None  # never inherits primary creds
        if model in unavailable:
            raise ValueError("test provider/model unavailable")
        return SimpleNamespace(base_url=("https://chatgpt.com/backend-api/codex"
                              if provider == "openai-codex" else "https://api.anthropic.com")), model

    def swap(agent, client, provider, model, base_url, api_mode):
        switched.append((provider, model, api_mode))

    monkeypatch.setattr(auxiliary, "resolve_provider_client", resolve)
    monkeypatch.setattr(cooldown, "_arm_rate_limit_cooldown", lambda *args: None)
    monkeypatch.setattr(lifecycle, "_swap_fallback_clients", swap)
    monkeypatch.setattr(runtime, "sync_credential_pool_entry_id", lambda *args: None)
    monkeypatch.setattr(compaction, "resolve_native_compaction_capabilities", lambda **kwargs: {})
    for name in ("_rebind_fallback_credential_pool", "_update_fallback_context_compressor",
                 "_reresolve_fallback_reasoning_config", "_rescope_fallback_extra_body",
                 "rewrite_prompt_model_identity", "_reset_stale_streak"):
        monkeypatch.setattr(core, name, lambda *args: None)
    agent = SimpleNamespace(
        model="gpt-6.1-sol", provider="openai-codex", base_url="https://chatgpt.com/backend-api/codex",
        _fallback_chain=plugin._routed_fallback_config("gpt-6.1-sol", plugin.DEFAULT_ALLOWED_MODELS,
                                                     {}, automatic_recovery=True)["fallback_providers"],
        _fallback_index=0, _unavailable_fallback_keys=set(),
        _is_azure_openai_url=lambda url: False, _is_direct_openai_url=lambda url: False,
        _provider_model_requires_responses_api=lambda *args, **kwargs: False,
        _anthropic_prompt_cache_policy=lambda **kwargs: (False, False),
        _ensure_lmstudio_runtime_loaded=lambda: None, _buffer_status=lambda notice: None,
    )
    suffix = list(plugin._recovery_models_after("gpt-6.1-sol"))
    for model in suffix:
        if model in unavailable:
            continue
        assert core.try_activate_fallback(agent) is True
        provider = "openai-codex" if model.startswith("gpt-") else "anthropic"
        mode = "codex_responses" if provider == "openai-codex" else "anthropic_messages"
        assert (agent.provider, agent.model, agent.api_mode) == (provider, model, mode)
        assert switched[-1] == (provider, model, mode)
    assert core.try_activate_fallback(agent) is False
    assert requested == [("openai-codex" if m.startswith("gpt-") else "anthropic", m) for m in suffix]
