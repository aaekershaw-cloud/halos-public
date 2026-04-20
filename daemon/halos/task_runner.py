"""Shared custom-task invocation used by scheduler and regression harness.

Factored out of TaskScheduler._run_task_inner so tests can run a task once and
get its output string back without wiring through notifications, dedup, or
scheduled-run bookkeeping.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .model_selector import select_model, detect_task_type


_THINKING_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL)


async def invoke_custom_task(
    payload: dict,
    *,
    engine=None,
    kimi_engine=None,
    config: Config = None,
    claude_api=None,
    skill_evolver=None,
) -> str:
    """Invoke a custom task once and return the cleaned result text.

    Mirrors the branch logic in TaskScheduler._run_task_inner: kimi CLI when
    the agent is configured for it, ClaudeCodeEngine otherwise, falling back
    to ephemeral or raw API. Strips <thinking>...</thinking> blocks.
    """
    prompt = (payload or {}).get("prompt", "")
    session_name = (payload or {}).get("session", "")

    if not prompt:
        return ""

    agent_cfg = config.agents.get(session_name) if (config and session_name) else None
    provider = getattr(agent_cfg, "provider", "") if agent_cfg else ""
    agent_model = getattr(agent_cfg, "model", "") if agent_cfg else ""
    use_kimi_cli = provider.lower() == "kimi"

    task_type_hint = detect_task_type(prompt)
    selected_model = select_model(
        prompt=prompt,
        task_type_hint=task_type_hint,
        force_model=agent_model or None,
    )

    if use_kimi_cli and kimi_engine and session_name:
        project_dir = _resolve_project_dir(kimi_engine, session_name)
        personality = _load_personality(project_dir)
        result_obj = await kimi_engine.invoke_streaming(
            instruction=prompt,
            project_name=session_name,
            project_dir=project_dir,
            model=selected_model,
            personality_override=personality,
        )
        result = result_obj.text

    elif engine and session_name:
        project_dir = _resolve_project_dir(engine, session_name)
        personality = _load_personality(project_dir)
        task_model = (
            (payload or {}).get("model")
            or (config.claude_code.default_model if config else None)
        )
        result_obj = await engine.invoke_streaming(
            instruction=prompt,
            project_name=session_name,
            project_dir=project_dir,
            model=task_model,
            personality_override=personality,
            timeout=0,
        )
        result = result_obj.text

    elif engine:
        system = "You are Hal, the user's AI assistant. Be concise and actionable."
        try:
            result = await engine.invoke_ephemeral(prompt, system, model="haiku")
        except Exception:
            result = (
                await claude_api.complete(prompt=prompt, system=system)
                if claude_api else ""
            )

    else:
        system = "You are Hal, the user's AI assistant. Be concise and actionable."
        result = (
            await claude_api.complete(prompt=prompt, system=system)
            if claude_api else ""
        )

    result = _THINKING_RE.sub("", result or "").strip()

    # Fire-and-forget crystallization for scheduled tasks
    if skill_evolver and session_name and result:
        import asyncio
        asyncio.create_task(
            skill_evolver.crystallize_turn(
                agent=session_name,
                source=f"scheduled:{session_name}",
                instruction=prompt[:2000],
                result_text=result[:4000],
                tool_calls=[],  # unknown at this layer; LLM heuristic-gates
                model="haiku",
            )
        )

    return result


def _resolve_project_dir(engine, session_name: str) -> str:
    project_dir = engine.project_map.get(session_name, "") or str(
        Path.home() / "Projects" / "halos" / "sessions" / session_name
    )
    return str(Path(project_dir).expanduser())


def _load_personality(project_dir: str) -> str:
    for fname in ("soul.md", "personality.md"):
        p = Path(project_dir) / fname
        if p.exists():
            return p.read_text().strip()
    return ""
