"""Base task class for HalOS scheduled tasks."""

import time
import logging
from abc import ABC, abstractmethod
from ..model_selector import select_model, detect_task_type, estimate_complexity

logger = logging.getLogger(__name__)


class BaseTask(ABC):
    """Base class for all scheduled tasks."""

    task_type: str = "base"

    def __init__(self, db, claude_api=None, engine=None, notifier=None, kimi_api=None, kimi_engine=None):
        self.db = db
        self.claude_api = claude_api
        self.engine = engine
        self.notifier = notifier
        self.kimi_api = kimi_api
        self.kimi_engine = kimi_engine

    async def complete(
        self, 
        prompt: str, 
        system: str = None, 
        use_kimi: bool = False,
        task_type_hint: str = None,
        force_model: str = None,
        prefer_cheap: bool = False,
    ) -> str:
        """Call the LLM with a prompt. Uses model selector to pick appropriate model.
        
        Args:
            prompt: The task prompt
            system: Optional system message
            use_kimi: Whether to prefer Kimi over Claude
            task_type_hint: Hint for task type (social_media, code, etc.)
            force_model: Force a specific model (overrides selection)
            prefer_cheap: Prefer cheaper models when uncertain
        """
        # Auto-select model based on task characteristics
        selected_model = select_model(
            prompt=prompt,
            task_type_hint=task_type_hint,
            force_model=force_model,
            prefer_cheap=prefer_cheap,
        )
        
        detected_type = task_type_hint or detect_task_type(prompt)
        complexity, confidence = estimate_complexity(prompt)
        
        logger.info(
            f"Task {self.task_type}: using model={selected_model}, "
            f"task_type={detected_type}, complexity={complexity}"
        )
        
        if use_kimi and self.kimi_engine:
            return await self.kimi_engine.invoke_ephemeral(prompt, system, model=selected_model)
        if use_kimi and self.kimi_api:
            return await self.kimi_api.complete(prompt, system, model=selected_model)
        if self.kimi_engine:
            return await self.kimi_engine.invoke_ephemeral(prompt, system, model=selected_model)
        if self.kimi_api:
            return await self.kimi_api.complete(prompt, system, model=selected_model)
        if self.engine:
            try:
                return await self.engine.invoke_ephemeral(prompt, system, model="haiku")
            except Exception as e:
                logger.warning(f"Engine invoke_ephemeral failed, falling back: {e}")
        if self.claude_api:
            return await self.claude_api.complete(prompt, system)
        return "No LLM engine available"

    @abstractmethod
    async def execute(self, payload: dict = None) -> dict:
        """Execute the task. Returns {"success": bool, "result": str}."""
        pass

    async def run(self, payload: dict = None) -> dict:
        """Run the task with logging and error handling."""
        start = time.time()
        try:
            result = await self.execute(payload)
            duration_ms = int((time.time() - start) * 1000)
            await self.db.log_task(
                task_type=self.task_type,
                duration_ms=duration_ms,
                success=result.get("success", True),
                summary=result.get("result", "")[:200],
            )
            return result
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            await self.db.log_task(
                task_type=self.task_type,
                duration_ms=duration_ms,
                success=False,
                summary=str(e)[:200],
            )
            logger.error(f"Task {self.task_type} failed: {e}")
            return {"success": False, "result": f"Error: {str(e)}"}
