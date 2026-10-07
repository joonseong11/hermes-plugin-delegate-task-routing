"""Per-task routing for Hermes' native delegate_task.

This is an in-process compatibility extension, not a subprocess wrapper. It
keeps the native delegation lifecycle and adds exact-allowlisted task fields:
``model``, ``reasoning_effort``, and ``toolsets``.

The patch is deliberately narrow and fail-closed. If the installed Hermes
internals no longer match the verified contract, registration raises instead
of guessing. If upstream later ships all three fields natively, this plugin
leaves the built-in implementation untouched.
"""

from __future__ import annotations

import contextvars
import copy
import inspect
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


PLUGIN_VERSION = "0.5.0"
PLUGIN_ID = "delegate-task-routing"
_PATCH_MARKER = "_delegate_task_routing_plugin_v1"
_ROUTE_FIELDS = ("label", "model", "reasoning_effort", "toolsets")
_LOG = logging.getLogger(__name__)
DEFAULT_ALLOWED_MODELS = (
    "claude-opus-5",
    "claude-fable-5.1",
    "gpt-6-astra",
    "gpt-6.1-sol",
    "gpt-6-luna",
    "claude-sonnet-5",
)
DEFAULT_FIXED_MODELS = {
    "implementation": "claude-opus-5",
    "research": "claude-opus-5",
    "verification": "gpt-6.1-sol",
}
WORK_TYPES = ("implementation", "research", "verification", "mechanical", "architecture")
# Recovery authorization is independent of primary lane selection.
DEFAULT_RECOVERY_CHAIN = (
    "gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna",
    "claude-opus-5", "claude-sonnet-5",
)
DEFAULT_FALLBACK_MODELS = frozenset(DEFAULT_RECOVERY_CHAIN)
DEFAULT_ALLOWED_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_ALLOWED_TOOLSETS = (
    "browser",
    "code_execution",
    "computer_use",
    "figma_lossless",
    "file",
    "image_gen",
    "reader_db",
    "session_search",
    "skills",
    "terminal",
    "tts",
    "vision",
    "web",
)

# Each gateway turn has its own context. The native function constructs all
# children synchronously before dispatching them in the background, so the
# routing map is available exactly while child objects are built.
_ACTIVE_ROUTES: contextvars.ContextVar[dict[int, dict[str, Any]] | None] = (
    contextvars.ContextVar("delegate_task_routing_active_routes", default=None)
)
_ACTIVE_TURN_ID: contextvars.ContextVar[tuple[str, str] | None] = (
    contextvars.ContextVar("delegate_task_routing_active_turn", default=None)
)

_POLICY_LOCK = threading.RLock()
_TURN_PLANS: dict[tuple[str, str], dict[str, Any]] = {}
_CURRENT_TURN: dict[str, str] = {}
_SKIP_HEADER_SESSIONS: set[str] = set()
_FORCED_ROUTE_REQUESTS: set[str] = set()
# Consecutive route_turn forcings per turn key that have not yet produced a
# stored plan. Guards against forcing loops: if the executed route_turn call
# cannot land its plan under the key the middleware reads (whatever the
# cause), enforcement must give up for the turn instead of re-forcing until
# the iteration budget burns out.
_FORCED_ROUTE_COUNTS: dict[tuple[str, str], int] = {}
_MAX_FORCED_ROUTE_ATTEMPTS = 3
# Forced delegate_task requests allowed per accepted plan before the dispatch
# is made. A plan that still has not dispatched after this many forced calls
# (rejected tasks, or list/steer/stop calls that dispatch nothing) stops being
# forced, so the parent can report the blocker in text or re-declare the plan
# instead of repeating the call until the core tool-loop guardrail halts the
# turn. The count lives on the plan, so a new route_turn starts from zero.
_MAX_FORCED_DELEGATE_ATTEMPTS = 3
# Last reasoning effort observed in each Slack parent session's own LLM
# request payload. registry.dispatch gives tool handlers no agent object, so
# this locally observed value is the only honest source for the header's
# main-agent effort ("Sol-medium"); when the payload carries no effort the
# header keeps "unknown" rather than inventing one.
_MAIN_EFFORTS: dict[str, str] = {}
_PLUGIN_STATE: Any = None
_DELEGATE_TASK_REQUEST_SCHEMA: dict[str, Any] | None = None
_ENFORCED_PLATFORMS: set[str] = {"slack"}
_HEADER_RE = re.compile(r"^_Alex:\s*[^\n]*_\s*\n*", re.IGNORECASE)
_ASYNC_DELEGATION_RE = re.compile(
    r"\[ASYNC DELEGATION(?: BATCH)? COMPLETE — ([^\]]+)\]"
)
# --- Cross-provider routing + graceful fallback ---------------------------
# A routed lane model may belong to a DIFFERENT provider than the parent
# gateway (e.g. parent on anthropic/claude-opus while a lane requests a codex
# gpt-*). The native child builder inherits the parent provider when no
# override_provider is passed, so a gpt-* model gets sent to api.anthropic.com
# and 404s ("model: gpt-5-6-terra-900k"). We classify the model family, pin the
# matching provider via override_provider (letting the runtime re-derive
# base_url/api_mode/credentials), and attach an ordered recovery chain so
# eligible provider failures degrade with a visible requested→actual note.
_ANTHROPIC_PROVIDER = "anthropic"
_CODEX_PROVIDER = "openai-codex"


def _model_provider_family(model: str) -> str | None:
    """Canonical provider a routed model id belongs to, or None if unknown.

    Prefix-based so forward-compat ids (e.g. a future gpt-6-*) still resolve.
    """
    text = str(model or "").strip().lower()
    if not text:
        return None
    if text.startswith(("gpt-", "gpt5", "o1", "o1-", "o3", "o3-", "o4", "o4-", "codex")):
        return _CODEX_PROVIDER
    if text.startswith("claude"):
        return _ANTHROPIC_PROVIDER
    return None


def _recovery_models_after(model: str) -> tuple[str, ...]:
    """Exact ordered suffix; models outside the chain have no automatic recovery."""
    if model not in DEFAULT_RECOVERY_CHAIN:
        return ()
    return DEFAULT_RECOVERY_CHAIN[DEFAULT_RECOVERY_CHAIN.index(model) + 1:]


def _routed_fallback_config(
    model: str,
    allowed_models: Sequence[str],
    routing_cfg: Any,
    *,
    automatic_recovery: bool,
) -> dict[str, Any]:
    """Validate operator chains against primary + separately authorized recovery IDs.

    Explicit chains (including []) win. Reject malformed or unauthorized routes
    before construction rather than letting the native normalizer silently drop
    them. Provider-only entries let core resolve fresh credentials through the
    shared provider runtime at activation, never copying the parent's key.
    """
    if routing_cfg is not None and not isinstance(routing_cfg, Mapping):
        raise ValueError("Routed fallback config must be an object")
    cfg = copy.deepcopy(dict(routing_cfg or {}))
    fallback_allow = set(allowed_models) | DEFAULT_FALLBACK_MODELS
    declared = cfg.get("fallback_providers")
    if declared is not None:
        if not isinstance(declared, list):
            raise ValueError("Routed fallback_providers must be a list")
        for entry in declared:
            if not isinstance(entry, Mapping):
                raise ValueError("Routed fallback entries must be objects")
            peer = entry.get("model")
            provider = entry.get("provider")
            if not isinstance(peer, str) or peer not in fallback_allow:
                raise ValueError("Routed fallback model is not in the fallback allowlist")
            if not provider or provider != _model_provider_family(peer):
                raise ValueError("Routed fallback provider does not match the approved model")
    else:
        cfg["fallback_providers"] = [
            {"provider": _model_provider_family(peer), "model": peer}
            for peer in (_recovery_models_after(model) if automatic_recovery else ())
        ]
    return cfg

ROUTE_TURN_SCHEMA = {
    "name": "route_turn",
    "description": (
        "Required first-step execution decision for every parent turn. Choose "
        "direct whenever you can already answer fully and safely from context, "
        "loaded skills, or a trivial lookup — never delegate a question you can "
        "answer immediately, because delegation runs in the background and sends "
        "the user a second message later; it must buy substantial independent "
        "work. Use single for one bounded chunk of substantial work; parallel "
        "for two or more independent outcomes, not implementation plus its tests. "
        "General implementation uses single with tests in the same worker lane; "
        "the parent reviews the evidence. No verification stage runs automatically. "
        "Add a verification lane only when the user explicitly asks for an "
        "independent check or review of something that already exists; it is an "
        "ordinary lane with work_type=verification. A review depends on the work "
        "it reviews: never put a review in the same parallel plan as that work, "
        "and a completion turn cannot dispatch further lanes. When one message "
        "asks for work and an independent review of it, delegate the work only, "
        "and say in the final answer that the review has not run and can be "
        "requested in a follow-up message. "
        "Small reversible local edits can stay direct. "
        "For a user-requested skill refresh, choose direct with empty lanes, then call "
        "skill_view for the changed skills before planning or editing; do not delegate "
        "merely to reload skills. If refreshed instructions reveal a larger scope, "
        "call route_turn again before any write to declare the required lanes. "
        "When uncertain whether delegation is worth it, choose direct. With "
        "mode=direct, lanes MUST be the empty array []. "
        "Per-lane work_type is required: implementation (code changes, file/sheet/doc "
        "writing, Figma edits, deploy preparation, any local/external write) and "
        "research (web research, DB query analysis, root-cause diagnosis, comparison "
        "or recommendation) MUST use claude-opus-5. Verification MUST use "
        "gpt-6.1-sol with work_type=verification. Mechanical work (extraction, "
        "reformatting, deterministic checks, simple visual QA) and exceptional "
        "architecture may choose any allowlisted model, e.g. gpt-6-luna, gpt-6-astra, "
        "claude-sonnet-5 or claude-fable-5.1. These fixed models are operator-"
        "configurable; follow the registered work_type guidance and validation errors. "
        "Recovery follows the ordered suffix of gpt-6.1-sol → gpt-6-sol → "
        "gpt-5.6-sol → gpt-6-luna → claude-opus-5 → claude-sonnet-5; models outside "
        "this chain have no automatic fallback. Give identical tasks identical models. "
        "Declare the exact model, effort, and "
        "least-privilege toolsets for every lane. Normally call route_turn once per "
        "turn. Only a direct skill-refresh preflight may reroute before any write "
        "when the refreshed instructions require delegation. Never reroute "
        "an accepted delegation or an async-delegation completion turn "
        "(routing is already recorded there)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["direct", "single", "parallel"],
            },
            "reason": {"type": "string", "minLength": 1},
            "lanes": {
                "type": "array",
                "description": "Delegated lanes. MUST be [] when mode=direct.",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1},
                        "work_type": {
                            "type": "string", "enum": list(WORK_TYPES),
                            "description": "Required semantic work category. implementation/research: claude-opus-5; verification: gpt-6.1-sol; mechanical/architecture: any allowlisted model.",
                        },
                        "model": {"type": "string"},
                        "reasoning_effort": {"type": "string"},
                        "toolsets": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        },
                    },
                    "required": [
                        "label",
                        "work_type",
                        "model",
                        "reasoning_effort",
                        "toolsets",
                    ],
                },
            },
        },
        "required": ["mode", "reason", "lanes"],
    },
}


def _as_string_list(value: Any, *, setting: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RuntimeError(f"{setting} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise RuntimeError(f"{setting} must contain only non-empty strings")
        clean = item.strip()
        if clean not in out:
            out.append(clean)
    return out


def _task_item_properties(schema: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return schema["parameters"]["properties"]["tasks"]["items"]["properties"]
    except (KeyError, TypeError):
        return {}


def _native_has_full_routing(delegate_module: Any) -> bool:
    properties = _task_item_properties(delegate_module.DELEGATE_TASK_SCHEMA)
    return all(field in properties for field in _ROUTE_FIELDS)


def _assert_compatible(delegate_module: Any) -> None:
    required_callables = (
        "delegate_task",
        "_build_child_preserving_parent_tools",
        "_run_single_child",
        "check_delegate_requirements",
    )
    missing = [
        name
        for name in required_callables
        if not callable(getattr(delegate_module, name, None))
    ]
    if missing:
        raise RuntimeError(
            "delegate-task-routing is incompatible with this Hermes build; "
            f"missing callables: {', '.join(missing)}"
        )

    delegate_params = inspect.signature(delegate_module.delegate_task).parameters
    for name in ("tasks", "parent_agent", "credentials_cfg"):
        if name not in delegate_params:
            raise RuntimeError(
                "delegate-task-routing is incompatible with this Hermes build; "
                f"delegate_task lacks {name!r}"
            )

    # The public preservation wrapper intentionally exposes only **kwargs in
    # current Hermes builds. Validate its concrete downstream builder instead.
    build_target = getattr(delegate_module, "_build_child_agent", None)
    if not callable(build_target):
        raise RuntimeError(
            "delegate-task-routing is incompatible with this Hermes build; "
            "missing _build_child_agent"
        )
    build_params = inspect.signature(build_target).parameters
    for name in ("task_index", "model", "toolsets", "parent_agent"):
        if name not in build_params:
            raise RuntimeError(
                "delegate-task-routing is incompatible with this Hermes build; "
                f"child builder lacks {name!r}"
            )


def _effective_effort(reasoning_config: Any) -> str | None:
    if not isinstance(reasoning_config, Mapping):
        return None
    if reasoning_config.get("enabled") is False:
        return "none"
    effort = reasoning_config.get("effort")
    return str(effort) if effort else "medium"


def _make_schema(
    delegate_module: Any,
    *,
    allowed_models: Sequence[str],
    allowed_efforts: Sequence[str],
    allowed_toolsets: Sequence[str],
) -> dict[str, Any]:
    # Start from the current build's dynamic schema so limits and control-plane
    # documentation stay aligned after ordinary Hermes updates.
    schema = copy.deepcopy(delegate_module.DELEGATE_TASK_SCHEMA)
    dynamic = getattr(delegate_module, "_build_dynamic_schema_overrides", None)
    if callable(dynamic):
        overrides = dynamic()
        if isinstance(overrides, Mapping):
            schema.update(copy.deepcopy(dict(overrides)))

    properties = _task_item_properties(schema)
    if not properties:
        raise RuntimeError("delegate_task tasks[] schema shape is incompatible")

    properties["label"] = {
        "type": "string",
        "minLength": 1,
        "description": "Human-readable lane name used in the execution header.",
    }
    properties["model"] = {
        "type": "string",
        "enum": list(allowed_models),
        "description": (
            "Required exact model id for this child. Match the accepted route_turn "
            "lane and its operator-configured work_type model policy."
        ),
    }
    properties["reasoning_effort"] = {
        "type": "string",
        "enum": list(allowed_efforts),
        "description": (
            "Required reasoning depth for this child. Use the least expensive level "
            "that safely fits the task."
        ),
    }
    properties["toolsets"] = {
        "type": "array",
        "minItems": 1,
        "items": {"type": "string", "enum": list(allowed_toolsets)},
        "uniqueItems": True,
        "description": (
            "Required exact toolsets for this child. They must be an approved subset "
            "of the parent session's enabled toolsets."
        ),
    }
    task_items = schema["parameters"]["properties"]["tasks"]["items"]
    required = list(task_items.get("required") or [])
    for field in ("goal", "label", "model", "reasoning_effort", "toolsets"):
        if field not in required:
            required.append(field)
    task_items["required"] = required
    schema["description"] = (
        "Spawn one or more native Hermes subagents in isolated contexts. Each task "
        "may route its child to an approved model, reasoning effort, and subset of "
        "toolsets. Use direct parent execution for genuinely simple work; delegate "
        "substantial execution or independent verification. Actual routing metadata "
        "is returned with each child result."
    )
    return schema


def _validate_routes(
    tasks: Any,
    *,
    parent_agent: Any,
    allowed_models: Sequence[str],
    allowed_efforts: Sequence[str],
    allowed_toolsets: Sequence[str],
) -> dict[int, dict[str, Any]]:
    if not isinstance(tasks, list):
        return {}

    model_allow = set(allowed_models)
    effort_allow = set(allowed_efforts)
    toolset_allow = set(allowed_toolsets)
    parent_toolsets_raw = getattr(parent_agent, "enabled_toolsets", None)
    parent_toolsets = (
        set(parent_toolsets_raw)
        if isinstance(parent_toolsets_raw, (list, tuple, set))
        else None
    )

    routes: dict[int, dict[str, Any]] = {}
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            raise ValueError(f"Task {index} must be an object.")
        missing = [
            field
            for field in ("goal", "label", "model", "reasoning_effort", "toolsets")
            if task.get(field) is None
            or (isinstance(task.get(field), str) and not str(task.get(field)).strip())
        ]
        if missing:
            raise ValueError(
                f"Task {index} is missing mandatory routing fields: "
                f"{', '.join(missing)}."
            )
        route: dict[str, Any] = {"label": str(task["label"]).strip()}

        if task.get("model") is not None:
            model = str(task.get("model") or "").strip()
            if not model or model not in model_allow:
                raise ValueError(
                    f"Task {index} model {model!r} is not in the exact allowed_models list."
                )
            route["model"] = model

        if task.get("reasoning_effort") is not None:
            effort = str(task.get("reasoning_effort") or "").strip().lower()
            if not effort or effort not in effort_allow:
                raise ValueError(
                    f"Task {index} reasoning_effort {effort!r} is not allowed."
                )
            route["reasoning_effort"] = effort

        if task.get("toolsets") is not None:
            toolsets = _as_string_list(
                task.get("toolsets"), setting=f"tasks[{index}].toolsets"
            )
            if not toolsets:
                raise ValueError(f"Task {index} toolsets must not be empty.")
            unknown = set(toolsets) - toolset_allow
            if unknown:
                raise ValueError(
                    f"Task {index} requested unapproved toolsets: "
                    f"{', '.join(sorted(unknown))}."
                )
            if parent_toolsets is not None and not set(toolsets).issubset(parent_toolsets):
                raise ValueError(
                    f"Task {index} toolsets would broaden the parent session's permissions."
                )
            route["toolsets"] = toolsets

        if route:
            requested_role = str(task.get("role") or "leaf").strip().lower()
            if requested_role != "leaf":
                raise ValueError(
                    f"Task {index} uses per-task routing and must remain a leaf; "
                    "nested orchestrator children can gain the delegation toolset."
                )
            routes[index] = route
    return routes


def _install_patches(
    delegate_module: Any,
    *,
    get_config: Callable[[str, Any], Any],
) -> Callable[[], None]:
    if _native_has_full_routing(delegate_module):
        _LOG.info(
            "Hermes already exposes full per-task delegation routing; "
            "delegate-task-routing is inactive."
        )
        return lambda: None

    _assert_compatible(delegate_module)
    if getattr(delegate_module, _PATCH_MARKER, None):
        raise RuntimeError("delegate-task-routing is already installed")

    allowed_models = _as_string_list(
        get_config("allowed_models", list(DEFAULT_ALLOWED_MODELS)),
        setting="allowed_models",
    )
    allowed_efforts = [
        value.lower()
        for value in _as_string_list(
            get_config("allowed_reasoning_efforts", list(DEFAULT_ALLOWED_EFFORTS)),
            setting="allowed_reasoning_efforts",
        )
    ]
    allowed_toolsets = _as_string_list(
        get_config("allowed_toolsets", list(DEFAULT_ALLOWED_TOOLSETS)),
        setting="allowed_toolsets",
    )
    if not allowed_models or not allowed_efforts or not allowed_toolsets:
        raise RuntimeError(
            "delegate-task-routing requires non-empty allowed_models, "
            "allowed_reasoning_efforts, and allowed_toolsets settings"
        )

    from hermes_constants import parse_reasoning_effort
    from tools.registry import tool_error

    original_delegate = delegate_module.delegate_task
    original_builder = delegate_module._build_child_preserving_parent_tools
    original_run_single = delegate_module._run_single_child

    def routed_builder(*args: Any, **kwargs: Any):
        task_index = kwargs.get("task_index", args[0] if args else 0)
        routes = _ACTIVE_ROUTES.get() or {}
        route = routes.get(int(task_index), {})
        if route.get("model"):
            kwargs["model"] = route["model"]
            # Cross-provider routing + graceful fallback. If the requested lane
            # model belongs to a different provider than the parent gateway,
            # resolve that provider's FULL credential bundle (base_url, api_key,
            # api_mode via the runtime provider system — the same path the native
            # delegation.provider config uses) and pin it. Without this the child
            # inherits the parent's provider/base_url/key and a gpt-* lane 404s
            # against api.anthropic.com. Skip credential resolution when a
            # trusted delegation override is already in play. Fallback policy is
            # separate below, so same-provider Codex lanes also get recovery.
            parent_agent = kwargs.get("parent_agent")
            parent_provider = str(getattr(parent_agent, "provider", "") or "").strip().lower()
            target_provider = _model_provider_family(route["model"])
            already_pinned = any(
                kwargs.get(k) for k in (
                    "override_provider", "override_base_url", "override_api_key",
                    "override_api_mode", "override_acp_command", "override_acp_args",
                )
            )
            if target_provider and not already_pinned and target_provider != parent_provider:
                try:
                    from tools.delegate_tool_config import _resolve_delegation_credentials
                    creds = _resolve_delegation_credentials(
                        {"provider": target_provider, "model": route["model"]}, parent_agent
                    )
                    kwargs["override_provider"] = creds.get("provider")
                    kwargs["override_base_url"] = creds.get("base_url")
                    kwargs["override_api_key"] = creds.get("api_key")
                    kwargs["override_api_mode"] = creds.get("api_mode")
                    if creds.get("request_overrides") is not None:
                        kwargs["override_request_overrides"] = creds.get("request_overrides")
                    if creds.get("command"):
                        kwargs["override_acp_command"] = creds.get("command")
                        kwargs["override_acp_args"] = list(creds.get("args") or [])
                except Exception as exc:
                    # Fail loud rather than build a child that will 404: raising a
                    # ValueError makes delegate_task surface a tool_error so the
                    # parent can route to the Anthropic peer explicitly.
                    _LOG.warning(
                        "delegate-task-routing: could not resolve provider %r for model %r: %s",
                        target_provider, route["model"], exc,
                    )
                    raise ValueError(
                        f"Cannot route lane model '{route['model']}' to provider "
                        f"'{target_provider}': {exc}"
                    ) from exc
            # The route pins the child model, so the native runtime does not
            # inherit parent fallback. Attach the approved recovery suffix independently of
            # cross-provider credential resolution, but never invent recovery
            # for operator-pinned credentials/transports. Explicit chains still
            # work there after validation. [] always disables fallback.
            kwargs["routing_cfg"] = _routed_fallback_config(
                route["model"], allowed_models, kwargs.get("routing_cfg"),
                automatic_recovery=not already_pinned,
            )
        if "toolsets" in route:
            kwargs["toolsets"] = list(route["toolsets"])

        child = original_builder(*args, **kwargs)
        requested_toolsets = set(route.get("toolsets") or ())
        if requested_toolsets:
            effective_raw = getattr(child, "enabled_toolsets", None)
            effective = (
                set(effective_raw)
                if isinstance(effective_raw, (list, tuple, set))
                else set()
            )
            unexpected = effective - requested_toolsets
            if unexpected:
                try:
                    child.close()
                except Exception:
                    pass
                raise ValueError(
                    "Native child construction broadened requested toolsets with: "
                    f"{', '.join(sorted(unexpected))}. Refusing to launch."
                )
        if route.get("reasoning_effort"):
            parsed = parse_reasoning_effort(route["reasoning_effort"])
            if parsed is None:
                raise ValueError(
                    f"Unsupported reasoning effort: {route['reasoning_effort']!r}"
                )
            child.reasoning_config = parsed

        child._delegate_label = route.get("label")
        child._delegate_requested_model = route.get("model")
        child._delegate_requested_reasoning_effort = route.get("reasoning_effort")
        child._delegate_requested_toolsets = tuple(route.get("toolsets") or ())
        child._delegate_routing_plugin_version = PLUGIN_VERSION
        return child

    def routed_run_single(*args: Any, **kwargs: Any):
        child = kwargs.get("child")
        if child is None and len(args) >= 3:
            child = args[2]
        result = original_run_single(*args, **kwargs)
        if not isinstance(result, dict) or child is None:
            return result
        if getattr(child, "_delegate_routing_plugin_version", None) != PLUGIN_VERSION:
            return result

        requested_toolsets = list(
            getattr(child, "_delegate_requested_toolsets", ()) or ()
        )
        effective_toolsets_raw = getattr(child, "enabled_toolsets", None)
        effective_toolsets = (
            list(effective_toolsets_raw)
            if isinstance(effective_toolsets_raw, (list, tuple, set))
            else None
        )
        result["routing"] = {
            "label": getattr(child, "_delegate_label", None),
            "requested_model": getattr(child, "_delegate_requested_model", None),
            "actual_model": getattr(child, "model", None),
            "actual_provider": getattr(child, "provider", None),
            "requested_reasoning_effort": getattr(
                child, "_delegate_requested_reasoning_effort", None
            ),
            "actual_reasoning_effort": _effective_effort(
                getattr(child, "reasoning_config", None)
            ),
            "requested_toolsets": requested_toolsets or None,
            "effective_toolsets": effective_toolsets,
            "child_session_id": getattr(child, "session_id", None),
            "extension": f"{PLUGIN_ID}@{PLUGIN_VERSION}",
        }
        return result

    def routed_delegate_task(*args: Any, **kwargs: Any):
        action = str(kwargs.get("action") or "").strip().lower()
        if action in {"list", "steer", "stop"}:
            return original_delegate(*args, **kwargs)

        tasks = kwargs.get("tasks")
        if tasks is None and len(args) >= 3:
            tasks = args[2]
        if isinstance(tasks, str):
            try:
                tasks = json.loads(tasks)
            except json.JSONDecodeError:
                return original_delegate(*args, **kwargs)

        parent_agent = kwargs.get("parent_agent")
        policy_plan = None
        if (
            parent_agent is not None
            and getattr(parent_agent, "session_id", None)
            and getattr(parent_agent, "_current_turn_id", None)
            and getattr(parent_agent, "_delegate_depth", 0) == 0
            and str(getattr(parent_agent, "platform", "") or "").lower()
            in _ENFORCED_PLATFORMS
        ):
            try:
                policy_plan = _validate_delegate_against_plan(parent_agent, tasks)
            except (RuntimeError, ValueError) as exc:
                return tool_error(str(exc))
        try:
            routes = _validate_routes(
                tasks,
                parent_agent=parent_agent,
                allowed_models=allowed_models,
                allowed_efforts=allowed_efforts,
                allowed_toolsets=allowed_toolsets,
            )
        except (RuntimeError, ValueError) as exc:
            return tool_error(str(exc))

        requested_top_role = str(kwargs.get("role") or "leaf").strip().lower()
        if routes and requested_top_role != "leaf":
            return tool_error(
                "Per-task routed delegations must use leaf children; nested "
                "orchestrator routing is blocked to preserve toolset boundaries."
            )

        token = _ACTIVE_ROUTES.set(routes)
        try:
            try:
                result = original_delegate(*args, **kwargs)
                if policy_plan is not None:
                    try:
                        parsed = json.loads(result) if isinstance(result, str) else result
                    except (TypeError, json.JSONDecodeError):
                        parsed = {}
                    if isinstance(parsed, Mapping):
                        with _POLICY_LOCK:
                            _mark_delegation_dispatch(policy_plan, parsed)
                return result
            except ValueError as exc:
                # Current native delegate_task converts child-construction
                # ValueError to tool_error itself. Keep the extension fail-closed
                # across compatible builds that may let it propagate.
                return tool_error(str(exc))
        finally:
            _ACTIVE_ROUTES.reset(token)

    routed_builder.__name__ = "routed_build_child_preserving_parent_tools"
    routed_run_single.__name__ = "routed_run_single_child"
    routed_delegate_task.__name__ = "routed_delegate_task"

    delegate_module._build_child_preserving_parent_tools = routed_builder
    delegate_module._run_single_child = routed_run_single
    delegate_module.delegate_task = routed_delegate_task
    setattr(delegate_module, _PATCH_MARKER, PLUGIN_VERSION)

    def unload() -> None:
        if delegate_module.delegate_task is routed_delegate_task:
            delegate_module.delegate_task = original_delegate
        if delegate_module._build_child_preserving_parent_tools is routed_builder:
            delegate_module._build_child_preserving_parent_tools = original_builder
        if delegate_module._run_single_child is routed_run_single:
            delegate_module._run_single_child = original_run_single
        if getattr(delegate_module, _PATCH_MARKER, None) == PLUGIN_VERSION:
            delattr(delegate_module, _PATCH_MARKER)

    return unload


def _turn_key(session_id: Any, turn_id: Any) -> tuple[str, str]:
    turn = str(turn_id or "")
    return ("turn", turn) if turn else ("session", str(session_id or ""))


def delegation_phase_for_turn(session_id: Any, turn_id: Any) -> str:
    """Return the non-sensitive phase of a routed delegation turn.

    Kept as a cross-plugin contract for presentation observers. The automatic
    verifier stage was removed in 0.5.0, so every turn is a ``worker`` turn.
    """
    return "worker"


def delegation_lifecycle_for_turn(session_id: Any, turn_id: Any) -> dict[str, Any]:
    """Return an authenticated, non-sensitive lifecycle view for observers.

    The plan is created by this plugin only after route validation or durable
    async-completion authentication.  Presentation plugins can therefore use
    it without parsing user-controlled completion-marker text.  No goals,
    labels, model names, toolsets, or result payloads are exposed.
    """
    with _POLICY_LOCK:
        plan = _TURN_PLANS.get(_turn_key(session_id, turn_id))
        if not isinstance(plan, Mapping):
            return {"mode": "unrouted", "delegation_ids": []}
        mode = str(plan.get("mode") or "unrouted")
        ids: list[str] = []
        for value in (
            *(plan.get("owned_delegation_ids") or []),
            plan.get("source_delegation_id"),
            plan.get("delegation_id"),
        ):
            value = str(value or "").strip()
            if value and value not in ids:
                ids.append(value)
        return {"mode": mode, "delegation_ids": ids, "dispatched": bool(plan.get("dispatched"))}


def _resolve_turn_context(parent_agent: Any, kw: Mapping[str, Any]) -> tuple[str, str]:
    """Resolve (session_id, turn_id) for a tool-handler invocation.

    registry.dispatch passes only task_id/session_id/user_task to plain tool
    handlers — no parent_agent and no turn_id; those reach agent-loop tools
    (delegate_task) only. Without a turn_id the plan would be stored under a
    session key while the LLM middleware and hooks look it up under the turn
    key, so the middleware would keep re-forcing route_turn every API call.
    Fall back to the session→turn map that _pre_llm_policy refreshes before
    every API call of the turn.
    """
    session_id = str(
        getattr(parent_agent, "session_id", "") or kw.get("session_id") or ""
    )
    turn_id = str(
        getattr(parent_agent, "_current_turn_id", "") or kw.get("turn_id") or ""
    )
    if not turn_id:
        with _POLICY_LOCK:
            turn_id = _CURRENT_TURN.get(session_id, "")
    return session_id, turn_id


def _is_enforced_parent(*, platform: Any, task_id: Any, parent_session_id: Any = "") -> bool:
    platform_value = getattr(platform, "value", platform)
    if str(platform_value or "").lower() not in _ENFORCED_PLATFORMS:
        return False
    if str(task_id or "").startswith("sa-") or str(parent_session_id or ""):
        return False
    return True


def _fixed_model_policy(value: Any = None) -> dict[str, str]:
    """Merge partial operator overrides without accepting typos or empty models."""
    if value is None:
        return dict(DEFAULT_FIXED_MODELS)
    if not isinstance(value, Mapping):
        raise ValueError("fixed_models must be an object")
    if set(value) - set(DEFAULT_FIXED_MODELS):
        raise ValueError("fixed_models accepts only implementation, research, verification")
    for work_type, model in value.items():
        if not isinstance(model, str) or not model or model != model.strip():
            raise ValueError(f"fixed_models.{work_type} must be an exact non-empty model ID")
    return {**DEFAULT_FIXED_MODELS, **value}


def _validate_plan(
    payload: Mapping[str, Any],
    *,
    allowed_models: Sequence[str],
    allowed_efforts: Sequence[str],
    allowed_toolsets: Sequence[str],
    fixed_models: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    fixed = _fixed_model_policy(fixed_models)
    mode = str(payload.get("mode") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    lanes = payload.get("lanes")
    if mode not in {"direct", "single", "parallel"}:
        raise ValueError("mode must be direct, single, or parallel")
    if not reason:
        raise ValueError("reason is required")
    if not isinstance(lanes, list):
        raise ValueError("lanes must be a list")
    if mode == "direct" and lanes:
        raise ValueError("direct mode must have no lanes")
    if mode == "single" and len(lanes) != 1:
        raise ValueError("single mode requires exactly one lane")
    if mode == "parallel" and len(lanes) < 2:
        raise ValueError("parallel mode requires at least two lanes")

    normalized: list[dict[str, Any]] = []
    for index, lane in enumerate(lanes):
        if not isinstance(lane, Mapping):
            raise ValueError(f"lane {index} must be an object")
        missing = [
            field
            for field in ("label", "work_type", "model", "reasoning_effort", "toolsets")
            if lane.get(field) is None
            or (isinstance(lane.get(field), str) and not str(lane.get(field)).strip())
        ]
        if missing:
            raise ValueError(f"lane {index} missing: {', '.join(missing)}")
        label = str(lane["label"]).strip()
        model = str(lane["model"]).strip()
        effort = str(lane["reasoning_effort"]).strip().lower()
        toolsets = _as_string_list(lane["toolsets"], setting=f"lanes[{index}].toolsets")
        work_type = lane["work_type"]
        if not isinstance(work_type, str) or work_type not in WORK_TYPES:
            raise ValueError(f"lane {index} work_type must be one of {', '.join(WORK_TYPES)}")
        required_model = fixed.get(work_type)
        if required_model and model != required_model:
            raise ValueError(
                f"lane {index} work_type={work_type} requires model={required_model}; "
                f"use {required_model} instead of {model}"
            )
        if model not in set(allowed_models):
            raise ValueError(f"lane {index} model is not allowed")
        if effort not in set(allowed_efforts):
            raise ValueError(f"lane {index} reasoning_effort is not allowed")
        unknown = set(toolsets) - set(allowed_toolsets)
        if unknown:
            raise ValueError(f"lane {index} has unapproved toolsets: {', '.join(sorted(unknown))}")
        normalized.append(
            {
                "label": label,
                "work_type": work_type,
                "model": model,
                "reasoning_effort": effort,
                "toolsets": toolsets,
            }
        )
    labels = [lane["label"].casefold() for lane in normalized]
    if len(labels) != len(set(labels)):
        raise ValueError("lane labels must be unique")
    return {
        "mode": mode,
        "reason": reason,
        "lanes": normalized,
        "dispatched": False,
        "created_at": time.time(),
    }


def _validate_delegate_against_plan(parent_agent: Any, tasks: Any) -> dict[str, Any] | None:
    session_id = getattr(parent_agent, "session_id", "") or ""
    turn_id = getattr(parent_agent, "_current_turn_id", "") or ""
    with _POLICY_LOCK:
        plan = _TURN_PLANS.get(_turn_key(session_id, turn_id))
    if not plan:
        raise ValueError("route_turn must be completed before delegate_task")
    if plan.get("mode") == "policy_bypass":
        # Enforcement was skipped for this turn (route_turn not directly
        # callable); per-task routing still applies to the dispatched tasks.
        return None
    if plan["mode"] == "direct":
        raise ValueError("delegate_task is blocked because route_turn declared direct mode")
    if not isinstance(tasks, list):
        raise ValueError("strict orchestration requires the tasks[] form")
    expected = plan["lanes"]
    if len(tasks) != len(expected):
        raise ValueError(
            f"delegate_task has {len(tasks)} tasks but route_turn declared {len(expected)} lanes"
        )
    for index, (task, lane) in enumerate(zip(tasks, expected)):
        if not isinstance(task, Mapping):
            raise ValueError(f"task {index} must be an object")
        for field in ("label", "model", "reasoning_effort"):
            if str(task.get(field) or "").strip().lower() != str(lane[field]).lower():
                raise ValueError(f"task {index} {field} does not match route_turn")
        if set(task.get("toolsets") or []) != set(lane["toolsets"]):
            raise ValueError(f"task {index} toolsets do not match route_turn")
    return plan


def _short_model(model: Any) -> str:
    text = str(model or "unknown").lower()
    if "luna" in text:
        return "Luna"
    if "terra" in text:
        return "Terra"
    if "sol" in text:
        return "Sol"
    return str(model or "unknown")


def _lane_text(route: Mapping[str, Any], index: int) -> str:
    label = str(route.get("label") or f"작업{index + 1}")
    req_model = _short_model(route.get("requested_model") or route.get("model"))
    req_effort = str(
        route.get("requested_reasoning_effort")
        or route.get("reasoning_effort")
        or "unknown"
    )
    status = str(route.get("status") or "")
    actual_model_raw = route.get("actual_model")
    actual_effort_raw = route.get("actual_reasoning_effort")
    if status and status not in {"completed", "success"}:
        if status == "pending":
            return f"{label}: {req_model}-{req_effort} · 대기"
        return f"{label}: {req_model}-{req_effort} → 실패"
    if actual_model_raw is None and not route.get("completed"):
        return f"{label}: {req_model}-{req_effort} · 실행 중"
    actual_model = _short_model(actual_model_raw)
    actual_effort = str(actual_effort_raw or "unknown")
    if actual_model != req_model or actual_effort != req_effort:
        return f"{label}: {req_model}-{req_effort} → {actual_model}-{actual_effort}"
    return f"{label}: {actual_model}-{actual_effort}"


def _load_actual_routes(delegation_id: str) -> list[dict[str, Any]]:
    try:
        from hermes_constants import get_hermes_home

        db = Path(get_hermes_home()) / "state.db"
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        row = con.execute(
            "SELECT event_json FROM async_delegations WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        con.close()
        event = json.loads(row[0]) if row and row[0] else {}
    except Exception as exc:
        _LOG.warning("Could not resolve delegation routing %s: %s", delegation_id, exc)
        return []
    routes: list[dict[str, Any]] = []
    for index, result in enumerate(event.get("results") or []):
        routing = dict(result.get("routing") or {})
        routing["status"] = result.get("status")
        routing.setdefault("label", f"작업{index + 1}")
        routing["completed"] = True
        routes.append(routing)
    return routes


def _durable_delegations() -> dict[str, Any]:
    if _PLUGIN_STATE is None:
        return {}
    try:
        value = _PLUGIN_STATE.get("delegations", {})
        return dict(value) if isinstance(value, Mapping) else {}
    except Exception as exc:
        _LOG.warning("Could not read orchestration state: %s", exc)
        return {}


def _persist_delegation_policy(delegation_id: str, record: Mapping[str, Any]) -> None:
    if not delegation_id or _PLUGIN_STATE is None:
        raise RuntimeError("durable orchestration state is unavailable")
    try:
        records = _durable_delegations()
        records[delegation_id] = dict(record)
        if len(records) > 100:
            prunable = sorted(
                (key for key in records if key != delegation_id),
                key=lambda key: float(records[key].get("created_at") or 0),
            )
            while len(records) > 80 and prunable:
                records.pop(prunable.pop(0), None)
        _PLUGIN_STATE.set("delegations", records)
    except Exception as exc:
        _LOG.warning("Could not persist orchestration state %s: %s", delegation_id, exc)
        raise RuntimeError("could not persist completion state") from exc


def _claim_completion_once(delegation_id: str, session_id: str, turn_id: str) -> dict[str, Any] | None:
    """Atomically reserve one authenticated completion envelope per gateway process.

    The durable stage makes replays fail closed across restarts. Records left in
    a dispatched stage by the pre-0.5.0 worker/verifier chain are claimed once
    as ordinary completions; ``claimed_from_stage`` names that legacy stage.
    """
    with _POLICY_LOCK:
        records = _durable_delegations()
        current = records.get(delegation_id)
        legacy_stage = ""
        if isinstance(current, Mapping):
            record = dict(current)
            if not _owned_session(str(record.get("parent_session_id") or ""), session_id):
                return None
            legacy_stage = str(record.get("stage") or "")
            if legacy_stage not in {"workers_dispatched", "verifiers_dispatched"}:
                return None
        else:
            record = {"parent_session_id": session_id, "created_at": time.time()}
        record.update(stage="completion_consumed", claimed_turn_id=turn_id, updated_at=time.time())
        _persist_delegation_policy(delegation_id, record)
        record["claimed_from_stage"] = legacy_stage
        return record


def _compression_tip(con: sqlite3.Connection, session_id: str) -> str:
    """Return only the verified compression-continuation tip."""
    current = str(session_id or "")
    seen = {current}
    for _ in range(100):
        row = con.execute(
            """SELECT child.id FROM sessions parent JOIN sessions child
               ON child.parent_session_id = parent.id
               WHERE parent.id = ? AND parent.end_reason = 'compression'
                 AND json_extract(COALESCE(child.model_config, '{}'), '$._branched_from') IS NULL
                 AND json_extract(COALESCE(child.model_config, '{}'), '$._delegate_from') IS NULL
                 AND COALESCE(child.source, '') != 'tool'
               ORDER BY CASE WHEN child.end_reason = 'compression' THEN 0
                             WHEN child.ended_at IS NULL THEN 1 ELSE 2 END,
                        COALESCE(child.last_activity_at, child.ended_at, child.started_at) DESC,
                        child.started_at DESC, child.id DESC LIMIT 1""",
            (current,),
        ).fetchone()
        if not row or not row[0] or str(row[0]) in seen:
            return current
        current = str(row[0])
        seen.add(current)
    return current


def _owned_session(
    owner_session_id: str,
    delivery_session_id: str,
    con: sqlite3.Connection | None = None,
) -> bool:
    owner = str(owner_session_id or "")
    delivery = str(delivery_session_id or "")
    if not owner or not delivery:
        return False
    if owner == delivery:
        return True
    close = False
    try:
        if con is None:
            from hermes_constants import get_hermes_home
            con = sqlite3.connect(
                f"file:{Path(get_hermes_home()) / 'state.db'}?immutable=1", uri=True
            )
            close = True
        return _compression_tip(con, owner) == delivery
    except Exception:
        return False
    finally:
        if close and con is not None:
            con.close()


def _verified_completion_routes(
    delegation_id: str, session_id: str
) -> list[dict[str, Any]]:
    """Authenticate an async completion against the durable core ledger."""
    try:
        from hermes_constants import get_hermes_home

        db = Path(get_hermes_home()) / "state.db"
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        row = con.execute(
            "SELECT state, parent_session_id, event_json "
            "FROM async_delegations WHERE delegation_id = ?",
            (delegation_id,),
        ).fetchone()
        # Hermes records terminal async outcomes as completed, error, or
        # restart-recovered unknown. They are all owned results; collapsing
        # error/unknown into a fake "missing information" header hid failures.
        if not row or str(row[0] or "") not in {"completed", "error", "unknown"}:
            con.close()
            return []
        if not _owned_session(str(row[1] or ""), session_id, con):
            con.close()
            return []
        event = json.loads(row[2]) if row[2] else {}
        con.close()
    except Exception as exc:
        _LOG.warning("Could not authenticate delegation completion %s: %s", delegation_id, exc)
        return []
    routes: list[dict[str, Any]] = []
    for index, result in enumerate(event.get("results") or []):
        routing = dict(result.get("routing") or {})
        routing["status"] = result.get("status")
        routing.setdefault("label", f"작업{index + 1}")
        routing["completed"] = True
        routes.append(routing)
    return routes


def _mark_delegation_dispatch(plan: dict[str, Any], parsed: Mapping[str, Any]) -> None:
    if parsed.get("error"):
        return
    delegation_id = str(parsed.get("delegation_id") or "")
    if delegation_id:
        plan["delegation_id"] = delegation_id
    if isinstance(parsed.get("results"), list):
        plan["actual_routes"] = [
            {
                **dict(item.get("routing") or {}),
                "status": item.get("status"),
                "completed": True,
            }
            for item in parsed["results"]
        ]
    plan["dispatched"] = True


def _pre_llm_policy(**kwargs: Any) -> Any:
    session_id = str(kwargs.get("session_id") or "")
    turn_id = str(kwargs.get("turn_id") or "")
    task_id = kwargs.get("task_id")
    platform = kwargs.get("platform")
    parent_session_id = kwargs.get("parent_session_id")
    enforced = _is_enforced_parent(
        platform=platform,
        task_id=task_id,
        parent_session_id=parent_session_id,
    )
    with _POLICY_LOCK:
        _CURRENT_TURN[session_id] = turn_id
        _ACTIVE_TURN_ID.set((session_id, turn_id))
        if len(_TURN_PLANS) > 500:
            stale = sorted(
                _TURN_PLANS,
                key=lambda key: float(_TURN_PLANS[key].get("created_at") or 0),
            )[:-400]
            for key in stale:
                _TURN_PLANS.pop(key, None)
        if len(_FORCED_ROUTE_COUNTS) > 500:
            for key in list(_FORCED_ROUTE_COUNTS)[:-400]:
                _FORCED_ROUTE_COUNTS.pop(key, None)
        if len(_FORCED_ROUTE_REQUESTS) > 500:
            _FORCED_ROUTE_REQUESTS.clear()
        if not enforced:
            _SKIP_HEADER_SESSIONS.add(session_id)
            return None
        _SKIP_HEADER_SESSIONS.discard(session_id)
    message = str(kwargs.get("user_message") or "")
    match = _ASYNC_DELEGATION_RE.search(message)
    if not match:
        return None
    delegation_id = match.group(1).strip()
    actual = _verified_completion_routes(delegation_id, session_id)
    if not actual:
        with _POLICY_LOCK:
            _TURN_PLANS[_turn_key(session_id, turn_id)] = {
                "mode": "policy_error",
                "reason": "async completion could not be authenticated",
                "lanes": [],
                "actual_routes": [],
                "dispatched": False,
                "delegation_id": delegation_id,
                "created_at": time.time(),
            }
        return {
            "context": (
                "The async completion marker could not be authenticated against a "
                "completed delegation owned by this parent session. Do not "
                "claim completion."
            )
        }
    record = _claim_completion_once(delegation_id, session_id, turn_id)
    if record is None:
        with _POLICY_LOCK:
            _TURN_PLANS[_turn_key(session_id, turn_id)] = {
                "mode": "policy_error", "reason": "async completion was already consumed",
                "lanes": [], "actual_routes": [], "dispatched": False,
                "delegation_id": delegation_id, "created_at": time.time(),
            }
        return {"context": "This authenticated async completion was already consumed. Do not dispatch or answer it again."}
    owned_delegation_ids = [delegation_id]
    if record.get("claimed_from_stage") == "verifiers_dispatched":
        # A pre-0.5.0 verifier delegation that was in flight at upgrade: keep
        # the attribution of the workers that ran before it.
        actual = list(record.get("prior_routes") or []) + actual
        source_delegation_id = str(record.get("source_delegation_id") or "").strip()
        if source_delegation_id and source_delegation_id not in owned_delegation_ids:
            owned_delegation_ids.insert(0, source_delegation_id)
    with _POLICY_LOCK:
        _TURN_PLANS[_turn_key(session_id, turn_id)] = {
            "mode": "completion",
            "reason": "background delegation completion",
            "lanes": [],
            "actual_routes": actual,
            "dispatched": True,
            "delegation_id": delegation_id,
            "owned_delegation_ids": owned_delegation_ids,
            "created_at": time.time(),
        }
    if actual:
        return {
            "context": (
                "Actual delegation routing metadata is available to the output "
                "policy. Do NOT call route_turn in this turn — routing is already "
                "recorded; use the delegated results and write the final answer. "
                "If your earlier interim message already fully answered the user "
                "and these results add nothing new, reply with exactly NO_REPLY "
                "so no duplicate message is sent."
            )
        }
    return {
        "context": (
            "Delegation completed, but actual routing metadata could not be "
            "resolved. Do NOT call route_turn in this turn."
        )
    }


def _extract_request_effort(request: Mapping[str, Any]) -> str:
    """Read the reasoning effort out of a provider request payload, if any."""
    value = request.get("reasoning_effort")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    for container_key in ("reasoning", "extra_body"):
        container = request.get(container_key)
        if isinstance(container, Mapping):
            nested = container.get("reasoning")
            if isinstance(nested, Mapping):
                effort = nested.get("effort")
                if isinstance(effort, str) and effort.strip():
                    return effort.strip().lower()
            effort = container.get("effort")
            if isinstance(effort, str) and effort.strip():
                return effort.strip().lower()
    return ""


def _forced_tool_choice(name: str, api_mode: Any) -> dict[str, Any]:
    mode = str(api_mode or "").lower()
    if mode == "anthropic_messages":
        return {"type": "tool", "name": name}
    if mode == "bedrock_converse":
        return {"tool": {"name": name}}
    if mode == "codex_responses":
        return {"type": "function", "name": name}
    return {"type": "function", "function": {"name": name}}


def _pin_route_turn_as_core(name: str = "route_turn") -> Callable[[], None] | None:
    """Keep route_turn out of tool-search deferral.

    Tool Search treats every non-core plugin tool as deferrable and collapses
    it behind the tool_search/tool_call bridge. The provider-side declaration
    injected by _ensure_route_tool_declared lets the model EMIT a forced
    route_turn call, but the executor validates calls against the collapsed
    surface (agent.valid_tool_names) and rejects the name, killing the turn
    after three retries. Appending route_turn to toolsets._HERMES_CORE_TOOLS
    keeps it eager on both the provider and executor surfaces.

    Returns an unpin callable on success, a no-op callable when the name is
    already pinned, or None when the core list cannot be found (future Hermes
    layout) — in that case _route_turn_enforceable() must gate enforcement.
    """
    try:
        import toolsets as toolsets_module
    except Exception:
        return None
    core = getattr(toolsets_module, "_HERMES_CORE_TOOLS", None)
    if not isinstance(core, list):
        return None
    if name in core:
        return lambda: None
    core.append(name)

    def _unpin() -> None:
        try:
            while name in core:
                core.remove(name)
        except Exception:
            pass

    return _unpin


def _route_turn_enforceable() -> bool:
    """Return False when forcing route_turn would be rejected by the executor.

    The live Tool Search layer defers non-core plugin tools; a deferred tool
    is only callable through the tool_call bridge, so a forced direct
    route_turn call fails validation ("Tool 'route_turn' does not exist").
    When that condition is detected the per-turn policy must be skipped for
    the turn instead of killing it. Environments where the runtime cannot be
    inspected (unit-test doubles, older Hermes without tool_search) keep
    enforcement on: there the collapse cannot happen.
    """
    try:
        from tools.tool_search import is_deferrable_tool_name
    except Exception:
        return True
    try:
        return not is_deferrable_tool_name(ROUTE_TURN_SCHEMA["name"])
    except Exception:
        return True


def _ensure_tool_declared(
    request: dict[str, Any], api_mode: Any, schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Inject one provider-native tool declaration idempotently."""
    mode = str(api_mode or "").lower()
    name = str(schema.get("name") or "").strip()
    description = str(schema.get("description") or "")
    parameters = schema.get("parameters")
    if not name or not isinstance(parameters, Mapping):
        raise RuntimeError("forced tool schema is unavailable or invalid")
    parameters = dict(parameters)
    if mode == "bedrock_converse":
        tool_config = dict(request.get("toolConfig") or {})
        tools = list(tool_config.get("tools") or [])
        if not any(
            isinstance(item, Mapping)
            and isinstance(item.get("toolSpec"), Mapping)
            and item["toolSpec"].get("name") == name
            for item in tools
        ):
            tools.append(
                {
                    "toolSpec": {
                        "name": name,
                        "description": description,
                        "inputSchema": {"json": parameters},
                    }
                }
            )
        tool_config["tools"] = tools
        request["toolConfig"] = tool_config
        return request
    tools = list(request.get("tools") or [])
    if mode == "anthropic_messages":
        present = any(isinstance(item, Mapping) and item.get("name") == name for item in tools)
        if not present:
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "input_schema": parameters,
                }
            )
    elif mode == "codex_responses":
        present = any(isinstance(item, Mapping) and item.get("name") == name for item in tools)
        if not present:
            tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                }
            )
    else:
        present = any(
            isinstance(item, Mapping)
            and isinstance(item.get("function"), Mapping)
            and item["function"].get("name") == name
            for item in tools
        )
        if not present:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                }
            )
    request["tools"] = tools
    return request


def _ensure_route_tool_declared(
    request: dict[str, Any], api_mode: Any
) -> dict[str, Any]:
    """Keep route_turn provider-visible even when tool_search defers it."""
    return _ensure_tool_declared(request, api_mode, ROUTE_TURN_SCHEMA)


def _llm_request_policy(request: dict[str, Any], **kwargs: Any) -> Any:
    if not _is_enforced_parent(
        platform=kwargs.get("platform"),
        task_id=kwargs.get("task_id"),
    ):
        return None
    key = _turn_key(kwargs.get("session_id"), kwargs.get("turn_id"))
    observed_effort = _extract_request_effort(request)
    with _POLICY_LOCK:
        if observed_effort:
            _MAIN_EFFORTS[str(kwargs.get("session_id") or "")] = observed_effort
            if len(_MAIN_EFFORTS) > 500:
                for stale_sid in list(_MAIN_EFFORTS)[:-400]:
                    _MAIN_EFFORTS.pop(stale_sid, None)
        plan = _TURN_PLANS.get(key)
    rewritten = dict(request)
    if plan is None:
        if not _route_turn_enforceable():
            with _POLICY_LOCK:
                _TURN_PLANS[key] = {
                    "mode": "policy_bypass",
                    "reason": (
                        "route_turn is deferred by tool search and would be "
                        "rejected by the executor; enforcement skipped for this turn"
                    ),
                    "lanes": [],
                    "dispatched": False,
                    "created_at": time.time(),
                }
            _LOG.warning(
                "route_turn is not directly callable (tool-search deferral); "
                "skipping per-turn orchestration policy for %s", key,
            )
            return None
        with _POLICY_LOCK:
            attempts = _FORCED_ROUTE_COUNTS.get(key, 0)
            if attempts >= _MAX_FORCED_ROUTE_ATTEMPTS:
                # A forcing loop: route_turn ran but its plan never landed
                # under this key (or the model kept failing validation).
                # Give the turn back to the model instead of burning the
                # iteration budget on endless forced route_turn calls.
                _TURN_PLANS[key] = {
                    "mode": "policy_bypass",
                    "reason": (
                        "route_turn forcing produced no usable plan after "
                        f"{attempts} attempts; enforcement skipped for this turn"
                    ),
                    "lanes": [],
                    "dispatched": False,
                    "created_at": time.time(),
                }
                _FORCED_ROUTE_COUNTS.pop(key, None)
                _LOG.warning(
                    "route_turn forcing looped %d times without a stored plan "
                    "for %s; skipping per-turn orchestration policy for this "
                    "turn", attempts, key,
                )
                return None
            _FORCED_ROUTE_COUNTS[key] = attempts + 1
        api_mode = kwargs.get("api_mode")
        rewritten = _ensure_route_tool_declared(rewritten, api_mode)
        choice = _forced_tool_choice("route_turn", api_mode)
        if str(api_mode or "").lower() == "bedrock_converse":
            tool_config = dict(rewritten.get("toolConfig") or {})
            tool_config["toolChoice"] = choice
            rewritten["toolConfig"] = tool_config
        else:
            rewritten["tool_choice"] = choice
            rewritten["parallel_tool_calls"] = False
        request_id = str(kwargs.get("api_request_id") or "")
        if request_id:
            with _POLICY_LOCK:
                _FORCED_ROUTE_REQUESTS.add(request_id)
        return {"request": rewritten, "source": PLUGIN_ID, "reason": "route required"}
    if plan.get("mode") in {"single", "parallel"} and not plan.get(
        "dispatched"
    ):
        with _POLICY_LOCK:
            attempts = int(plan.get("forced_delegate_attempts") or 0)
            if attempts >= _MAX_FORCED_DELEGATE_ATTEMPTS:
                if not plan.get("delegate_forcing_released"):
                    plan["delegate_forcing_released"] = True
                    _LOG.warning(
                        "delegate_task forcing made no dispatch after %d "
                        "attempts for %s; releasing the forced tool choice "
                        "for this plan", attempts, key,
                    )
                return None
            plan["forced_delegate_attempts"] = attempts + 1
        api_mode = kwargs.get("api_mode")
        if not isinstance(_DELEGATE_TASK_REQUEST_SCHEMA, Mapping):
            raise RuntimeError("delegate_task request schema was not retained at plugin registration")
        rewritten = _ensure_tool_declared(
            rewritten, api_mode, _DELEGATE_TASK_REQUEST_SCHEMA
        )
        choice = _forced_tool_choice("delegate_task", api_mode)
        if str(api_mode or "").lower() == "bedrock_converse":
            tool_config = dict(rewritten.get("toolConfig") or {})
            tool_config["toolChoice"] = choice
            rewritten["toolConfig"] = tool_config
        else:
            rewritten["tool_choice"] = choice
            rewritten["parallel_tool_calls"] = False
        return {"request": rewritten, "source": PLUGIN_ID, "reason": "delegation required"}
    return None


_DELEGATE_FORCING_RELEASED_NOTE = (
    "delegate_task did not dispatch the declared route_turn plan after "
    f"{_MAX_FORCED_DELEGATE_ATTEMPTS} forced attempts, so it is no longer "
    "forced. Do not repeat the same call: tell the user in text what blocked "
    "the delegation, or call route_turn again to declare a plan you can carry out."
)


def _delegate_forcing_exhausted(parent_agent: Any) -> bool:
    """True once the current plan has used up its forced delegate_task calls."""
    session_id = getattr(parent_agent, "session_id", "") or ""
    turn_id = getattr(parent_agent, "_current_turn_id", "") or ""
    with _POLICY_LOCK:
        plan = _TURN_PLANS.get(_turn_key(session_id, turn_id))
        return bool(
            plan
            and not plan.get("dispatched")
            and int(plan.get("forced_delegate_attempts") or 0)
            >= _MAX_FORCED_DELEGATE_ATTEMPTS
        )


def _pre_tool_policy(tool_name: str, **kwargs: Any) -> Any:
    session_id = str(kwargs.get("session_id") or "")
    with _POLICY_LOCK:
        if session_id in _SKIP_HEADER_SESSIONS:
            return None
    if not _is_enforced_parent(
        platform=kwargs.get("platform") or "slack",
        task_id=kwargs.get("task_id"),
    ):
        return None
    key = _turn_key(session_id, kwargs.get("turn_id"))
    with _POLICY_LOCK:
        plan = _TURN_PLANS.get(key)
        forced_request = str(kwargs.get("api_request_id") or "") in _FORCED_ROUTE_REQUESTS
    if plan is not None and plan.get("mode") == "policy_bypass":
        return None
    if forced_request and tool_name != "route_turn":
        return {
            "action": "block",
            "message": "Only route_turn is allowed from a forced routing response",
        }
    if plan is None and tool_name != "route_turn":
        return {"action": "block", "message": "route_turn must be the first action of this turn"}
    if (
        plan
        and plan.get("mode") in {"single", "parallel"}
        and not plan.get("dispatched")
        and tool_name not in {"route_turn", "delegate_task"}
    ):
        if plan.get("delegate_forcing_released"):
            return {"action": "block", "message": _DELEGATE_FORCING_RELEASED_NOTE}
        return {"action": "block", "message": "delegate_task must follow the declared route_turn plan"}
    return None


def _is_silence_marker(text: str) -> bool:
    """True when the response is exactly a gateway silence marker.

    The gateway suppresses delivery only when the WHOLE response is the
    marker; prepending the Alex header would turn NO_REPLY into a delivered
    message, so the header must stay off silence replies.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return False
    try:
        from gateway.response_filters import is_intentional_silence_response

        return bool(is_intentional_silence_response(stripped))
    except Exception:
        return stripped in {"NO_REPLY", "[SILENT]", "SILENT"}


def _transform_header(response_text: str, session_id: str, model: str, **kwargs: Any) -> Any:
    if _is_silence_marker(response_text):
        return None
    with _POLICY_LOCK:
        if session_id in _SKIP_HEADER_SESSIONS:
            return None
        explicit_turn_id = str(kwargs.get("turn_id") or "")
        active_turn = _ACTIVE_TURN_ID.get()
        turn_id = (
            explicit_turn_id
            or (active_turn[1] if active_turn and active_turn[0] == session_id else "")
            or _CURRENT_TURN.get(session_id, "")
        )
        plan = _TURN_PLANS.get(_turn_key(session_id, turn_id))
    main_effort = str((plan or {}).get("main_reasoning_effort") or "")
    if not main_effort or main_effort == "unknown":
        with _POLICY_LOCK:
            main_effort = _MAIN_EFFORTS.get(str(session_id), "") or main_effort or "medium"
    main = f"{_short_model(model)}-{main_effort}"
    if plan is None:
        header = f"_Alex: {main} · 위임 기록 만료 · 실행경로 미검증_"
    elif plan.get("mode") == "direct":
        header = f"_Alex: {main} · 직접 처리_"
    elif plan.get("mode") == "policy_bypass":
        header = f"_Alex: {main} · 라우팅 정책 미적용_"
    elif plan.get("delegate_forcing_released") and not plan.get("dispatched"):
        # Nothing was dispatched; the declared lanes must not read as running.
        header = f"_Alex: {main} · 위임 미실행_"
    else:
        routes = plan.get("actual_routes") or plan.get("lanes") or []
        lane_parts = [_lane_text(route, index) for index, route in enumerate(routes)]
        detail = " · ".join(lane_parts) if lane_parts else "위임 기록 만료 · 실행경로 미검증"
        header = f"_Alex: {main} · {detail}_"
    body = _HEADER_RE.sub("", str(response_text or "")).lstrip()
    return f"{header}\n\n{body}"


def register(ctx) -> None:
    global _PLUGIN_STATE, _DELEGATE_TASK_REQUEST_SCHEMA
    import tools.delegate_tool as delegate_module

    _PLUGIN_STATE = ctx.state
    if _PLUGIN_STATE is None or not all(callable(getattr(_PLUGIN_STATE, method, None)) for method in ("get", "set")):
        raise RuntimeError("profile-scoped plugin state is unavailable")

    if _native_has_full_routing(delegate_module):
        _LOG.info(
            "Native delegate_task already has model, reasoning_effort, and toolsets; "
            "plugin did not override it."
        )
        return

    unload = _install_patches(delegate_module, get_config=ctx.get_config)
    try:
        allowed_models = _as_string_list(
            ctx.get_config("allowed_models", list(DEFAULT_ALLOWED_MODELS)),
            setting="allowed_models",
        )
        allowed_efforts = [
            value.lower()
            for value in _as_string_list(
                ctx.get_config(
                    "allowed_reasoning_efforts", list(DEFAULT_ALLOWED_EFFORTS)
                ),
                setting="allowed_reasoning_efforts",
            )
        ]
        allowed_toolsets = _as_string_list(
            ctx.get_config("allowed_toolsets", list(DEFAULT_ALLOWED_TOOLSETS)),
            setting="allowed_toolsets",
        )
        schema = _make_schema(
            delegate_module,
            allowed_models=allowed_models,
            allowed_efforts=allowed_efforts,
            allowed_toolsets=allowed_toolsets,
        )
        _DELEGATE_TASK_REQUEST_SCHEMA = copy.deepcopy(schema)
        fixed_models = _fixed_model_policy(ctx.get_config("fixed_models", None))
        for work_type, model in fixed_models.items():
            if model not in allowed_models:
                raise ValueError(f"fixed_models.{work_type}={model} must be in allowed_models")
        route_schema = copy.deepcopy(ROUTE_TURN_SCHEMA)
        lane_schema = route_schema["parameters"]["properties"]["lanes"]["items"]
        lane_schema["properties"]["model"]["enum"] = list(allowed_models)
        lane_schema["properties"]["work_type"]["description"] = (
            f"implementation: {fixed_models['implementation']}; "
            f"research: {fixed_models['research']}; "
            f"verification: {fixed_models['verification']}; "
            "mechanical/architecture: any allowlisted model."
        )
        # Replace only policy IDs, not recovery-chain IDs, in the selection text.
        start = route_schema["description"].index("Per-lane work_type")
        end = route_schema["description"].index("Recovery follows", start)
        route_schema["description"] = (
            route_schema["description"][:start]
            + "Per-lane work_type is required. "
            + lane_schema["properties"]["work_type"]["description"]
            + " implementation includes code/file/sheet/doc/Figma edits, deploy preparation "
            "and any local/external write; research includes web/DB analysis, diagnosis "
            "and comparison/recommendation; mechanical includes extraction, reformatting, "
            "deterministic checks and simple visual QA; architecture is exceptional design. "
            + route_schema["description"][end:]
        )

        def route_handler(args: dict[str, Any], **kw: Any):
            parent_agent = kw.get("parent_agent")
            try:
                guard_session_id, guard_turn_id = _resolve_turn_context(parent_agent, kw)
                guard_key = _turn_key(guard_session_id, guard_turn_id)
                with _POLICY_LOCK:
                    existing = _TURN_PLANS.get(guard_key)
                if existing is not None and existing.get("mode") == "completion":
                    # An async completion turn already carries its routing
                    # state. A redundant route_turn call here must not clobber
                    # it — that would erase the actual lane attribution from
                    # the final header.
                    return json.dumps(
                        {
                            "status": "already_routed",
                            "mode": existing.get("mode"),
                            "note": (
                                "This turn's routing is already recorded by the "
                                "async-delegation policy. Do not call route_turn "
                                "again. Write the final user-facing answer now."
                            ),
                        },
                        ensure_ascii=False,
                    )
                plan = _validate_plan(
                    args,
                    allowed_models=allowed_models,
                    allowed_efforts=allowed_efforts,
                    allowed_toolsets=allowed_toolsets,
                    fixed_models=fixed_models,
                )
                parent_sets = set(getattr(parent_agent, "enabled_toolsets", None) or [])
                for index, lane in enumerate(plan["lanes"]):
                    if parent_sets and not set(lane["toolsets"]).issubset(parent_sets):
                        raise ValueError(
                            f"lane {index} toolsets would broaden parent permissions"
                        )
                session_id, turn_id = guard_session_id, guard_turn_id
                effort = _effective_effort(getattr(parent_agent, "reasoning_config", None))
                if not effort:
                    with _POLICY_LOCK:
                        effort = _MAIN_EFFORTS.get(str(session_id), "")
                plan["main_reasoning_effort"] = effort or "unknown"
                plan["parent_session_id"] = str(session_id)
                key = _turn_key(session_id, turn_id)
                with _POLICY_LOCK:
                    _TURN_PLANS[key] = plan
                    if turn_id:
                        _CURRENT_TURN[str(session_id)] = str(turn_id)
                    _FORCED_ROUTE_COUNTS.pop(key, None)
                    request_id = str(kw.get("api_request_id") or "")
                    if request_id:
                        _FORCED_ROUTE_REQUESTS.discard(request_id)
                acceptance_note = ""
                if plan["mode"] != "direct":
                    acceptance_note = (
                        "Lanes run in the BACKGROUND. After dispatching via "
                        "delegate_task, reply with ONLY a one-line interim "
                        "status. Do not answer the user's question inline — "
                        "the complete answer must come from the completion "
                        "turn after the delegated results return, otherwise "
                        "the user receives the same answer twice."
                    )
                return json.dumps(
                    {
                        "status": "accepted",
                        "mode": plan["mode"],
                        "reason": plan["reason"],
                        **({"note": acceptance_note} if acceptance_note else {}),
                        "lanes": plan["lanes"],
                        "dispatch_lanes": plan["lanes"],
                    },
                    ensure_ascii=False,
                )
            except (RuntimeError, ValueError) as exc:
                from tools.registry import tool_error

                return tool_error(str(exc))

        def handler(args: dict[str, Any], **kw: Any):
            action = str(args.get("action") or "").strip().lower()
            parent_agent = kw.get("parent_agent")
            tasks = delegate_module._strip_model_hidden_task_fields(args.get("tasks"))
            enforce_plan = (
                parent_agent is not None
                and getattr(parent_agent, "_delegate_depth", 0) == 0
                and str(getattr(parent_agent, "platform", "") or "").lower()
                in _ENFORCED_PLATFORMS
            )
            if action not in {"list", "steer", "stop"} and enforce_plan:
                try:
                    _validate_delegate_against_plan(parent_agent, tasks)
                except (RuntimeError, ValueError) as exc:
                    from tools.registry import tool_error

                    message = str(exc)
                    if _delegate_forcing_exhausted(parent_agent):
                        message = f"{message}. {_DELEGATE_FORCING_RELEASED_NOTE}"
                    return tool_error(message)
            # Injection and dispatch bookkeeping belong to the patched core
            # entrypoint; both registry and live-dispatch calls use it.
            return delegate_module.delegate_task(
                goal=args.get("goal"),
                context=args.get("context"),
                tasks=tasks,
                max_iterations=args.get("max_iterations"),
                role=args.get("role"),
                background=delegate_module._model_background_value(
                    args, parent_agent
                ),
                output_schema=args.get("output_schema"),
                action=args.get("action"),
                subagent_id=args.get("subagent_id"),
                message=args.get("message"),
                parent_agent=parent_agent,
            )

        route_registration = ctx.register_tool(
            name="route_turn",
            toolset="delegation",
            schema=route_schema,
            handler=route_handler,
            description=route_schema["description"],
            emoji="🧭",
        )
        if route_registration is None:
            raise RuntimeError("failed to register route_turn")

        core_unpin = _pin_route_turn_as_core()
        if core_unpin is not None:
            base_unload = unload

            def _unload_with_unpin() -> None:
                core_unpin()
                base_unload()

            unload = _unload_with_unpin
        if not _route_turn_enforceable():
            _LOG.warning(
                "route_turn remains deferrable after core pinning; per-turn "
                "enforcement will be skipped at request time instead of "
                "killing turns. Check tool-search/core-tool layout of this "
                "Hermes build."
            )

        registration = ctx.register_tool(
            name="delegate_task",
            toolset="delegation",
            schema=schema,
            handler=handler,
            check_fn=delegate_module.check_delegate_requirements,
            description=schema["description"],
            emoji="🔀",
            override=True,
        )
        if registration is None:
            raise RuntimeError("failed to override native delegate_task registration")

        ctx.register_hook("pre_llm_call", _pre_llm_policy)
        ctx.register_hook("pre_tool_call", _pre_tool_policy)
        ctx.register_hook("transform_llm_output", _transform_header)
        ctx.register_middleware("llm_request", _llm_request_policy)
    except Exception:
        unload()
        raise

    ctx.on_unload(unload)
    _LOG.info(
        "Enabled native delegate_task per-task routing for models=%s efforts=%s "
        "toolsets=%s",
        allowed_models,
        allowed_efforts,
        allowed_toolsets,
    )
