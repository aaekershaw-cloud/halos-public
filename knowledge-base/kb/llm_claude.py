"""LLM integration using kimi CLI instead of Anthropic SDK"""

import json
import logging
import subprocess
import shutil
from typing import Dict, Any, Optional
from kb.errors import TransientError, PermanentError
from kb.llm import map_model_name, calculate_cost, estimate_tokens

logger = logging.getLogger(__name__)


def call_llm_with_kimi(
    prompt: str,
    model: str = 'sonnet',
    max_tokens: int = 4096,
    temperature: float = 0.0,
    system: Optional[str] = None
) -> Dict[str, Any]:
    """
    Call Claude LLM via kimi CLI.

    Args:
        prompt: User prompt
        model: Model name (haiku, sonnet, opus)
        max_tokens: Maximum tokens to generate
        temperature: Sampling temperature
        system: Optional system prompt

    Returns:
        {
            'content': str,
            'model': str,
            'input_tokens': int,
            'output_tokens': int,
            'cost_usd': float
        }

    Raises:
        TransientError: For retryable failures
        PermanentError: For permanent failures
    """
    # Find claude binary
    claude_bin = shutil.which("claude")
    if not claude_bin:
        import os
        from pathlib import Path
        common_paths = [
            Path("/opt/homebrew/bin/claude"),
            Path.home() / ".local/bin/claude",
            Path("/usr/local/bin/claude"),
        ]
        for path in common_paths:
            if path.exists():
                claude_bin = str(path)
                break

    if not claude_bin:
        raise PermanentError("claude CLI not found. Install Claude Code.")

    model_id = map_model_name(model)

    # Build full prompt with system if provided
    full_prompt = prompt
    if system:
        full_prompt = f"{system}\n\n{prompt}"

    # Build claude command — prompt piped via stdin to avoid process list exposure
    cmd = [
        claude_bin,
        "--print",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--model", model_id,
    ]

    try:
        # Run claude with prompt on stdin (not -p) to keep it out of ps
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "Unknown error"

            # Classify errors
            error_lower = error_msg.lower()

            if "authentication" in error_lower or "api key" in error_lower:
                raise PermanentError(f"Authentication failed: {error_msg}")
            elif "rate limit" in error_lower or "429" in error_lower:
                raise TransientError(f"Rate limited: {error_msg}")
            elif "timeout" in error_lower:
                raise TransientError(f"Timeout: {error_msg}")
            elif "context" in error_lower or "token" in error_lower:
                raise PermanentError(f"Context window exceeded: {error_msg}")
            else:
                raise PermanentError(f"kimi CLI error: {error_msg}")

        # Parse NDJSON output from claude CLI
        # Format: {"type":"assistant","message":{"content":[{"type":"text","text":"..."}],...}}
        #         {"type":"result","result":"...","total_cost_usd":0.01,"usage":{...}}
        output_text = []
        input_tokens = 0
        output_tokens = 0
        cost_usd_override = None

        for line in result.stdout.strip().split('\n'):
            if not line:
                continue

            try:
                event = json.loads(line)
                event_type = event.get("type", "")

                # Collect assistant text from assistant message events
                if event_type == "assistant":
                    message = event.get("message", {})
                    content_blocks = message.get("content", [])
                    for block in content_blocks:
                        if block.get("type") == "text":
                            output_text.append(block.get("text", ""))

                # Extract usage and cost from result event
                elif event_type == "result":
                    cost_usd_override = event.get("total_cost_usd")
                    usage = event.get("usage", {})
                    input_tokens = usage.get("input_tokens", input_tokens)
                    output_tokens = usage.get("output_tokens", output_tokens)

            except json.JSONDecodeError:
                continue

        content = "".join(output_text).strip()

        if not content:
            raise PermanentError("claude returned empty response")

        # Estimate tokens if not provided by result event
        if input_tokens == 0:
            input_tokens = estimate_tokens(full_prompt)
        if output_tokens == 0:
            output_tokens = estimate_tokens(content)

        # Use actual cost from result event if available, else calculate
        cost_usd = cost_usd_override if cost_usd_override is not None else calculate_cost(model_id, input_tokens, output_tokens)

        logger.info(
            f"claude LLM call successful: {model} "
            f"({input_tokens} in, {output_tokens} out, ${cost_usd:.4f})"
        )

        return {
            'content': content,
            'model': model_id,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cost_usd': cost_usd
        }

    except subprocess.TimeoutExpired:
        raise TransientError("kimi CLI timed out after 5 minutes")

    except FileNotFoundError:
        raise PermanentError(
            "kimi binary not found. Install from: https://github.com/example/kimi"
        )

    except Exception as e:
        if isinstance(e, (TransientError, PermanentError)):
            raise
        raise PermanentError(f"Unexpected error calling kimi: {e}") from e
