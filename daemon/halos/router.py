"""Task routing for HalOS — returns RouteDecision metadata, no API calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)

CODE_KEYWORDS = [
    "fix", "debug", "refactor", "write code", "edit", "update the",
    "add a feature", "implement", "build", "deploy", "commit", "push",
    "git ", "lint", "test", "migration", "component", "endpoint",
    "api route", "bug in", "error in", ".tsx", ".ts", ".py", ".js",
]

HEAVY_KEYWORDS = [
    "analyze", "think through", "strategy", "plan", "compare",
    "evaluate", "deep dive", "pros and cons", "architecture",
]


@dataclass
class RouteDecision:
    session_type: str           # "project" | "general"
    project_name: Optional[str]
    project_dir: Optional[str]
    model: str                  # "haiku" | "sonnet" | "opus" | "kimi"
    effort: str                 # "low" | "medium" | "high"
    use_fallback: bool = False  # True = use OpenRouter instead
    use_kimi: bool = False      # True = use native Kimi API instead


class Router:
    def __init__(self, config: Config, engine=None):
        self.config = config
        self.engine = engine

    def classify(self, message: str, sticky_session: Optional[dict] = None) -> RouteDecision:
        """Classify a message and return routing metadata."""
        lower = message.lower()

        # Explicit Kimi request
        if any(k in lower for k in ("kimi", "moonshot")):
            return RouteDecision(
                session_type="general",
                project_name=None,
                project_dir=None,
                model="kimi",
                effort="medium",
                use_kimi=True,
            )

        # Active sticky session → route to that project
        if sticky_session:
            return RouteDecision(
                session_type="project",
                project_name=sticky_session["name"],
                project_dir=sticky_session["project_dir"],
                model="sonnet",
                effort="medium",
            )

        # Detect project name in message
        detected_project = None
        detected_dir = None
        if self.engine:
            for name in self.engine.project_map:
                if name in lower:
                    _, detected_dir = self.engine.resolve_project(name)
                    detected_project = name
                    break
            if not detected_project:
                for alias, canonical in self.engine.aliases.items():
                    if alias in lower:
                        detected_project = canonical
                        _, detected_dir = self.engine.resolve_project(canonical)
                        break

        code_score = sum(1 for kw in CODE_KEYWORDS if kw in lower)
        if code_score >= 2 and detected_project:
            return RouteDecision(
                session_type="project",
                project_name=detected_project,
                project_dir=detected_dir,
                model="sonnet",
                effort="high",
            )

        if any(kw in lower for kw in HEAVY_KEYWORDS):
            return RouteDecision(
                session_type="general",
                project_name=None,
                project_dir=None,
                model="opus",
                effort="high",
            )

        return RouteDecision(
            session_type="general",
            project_name=None,
            project_dir=None,
            model="haiku",
            effort="low",
        )
