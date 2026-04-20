"""LLM integration for knowledge base compilation"""

import json
import logging
import os
from typing import Dict, Any, Optional
from kb.errors import TransientError, PermanentError

logger = logging.getLogger(__name__)

# LLM backend selection
# Set KB_LLM_BACKEND=anthropic to use Anthropic SDK directly
# Otherwise defaults to kimi CLI if available
USE_KIMI = os.environ.get("KB_LLM_BACKEND") != "anthropic"

# Model mapping
MODEL_MAP = {
    'haiku': 'claude-haiku-4-5-20251001',
    'sonnet': 'claude-sonnet-4-6',
    'opus': 'claude-opus-4-6'
}

# Cost per 1M tokens (USD)
COSTS = {
    'claude-haiku-4-5-20251001': {'input': 0.80, 'output': 4.00},
    'claude-sonnet-4-6': {'input': 3.00, 'output': 15.00},
    'claude-opus-4-6': {'input': 5.00, 'output': 25.00}
}


def map_model_name(model: str) -> str:
    """
    Map short model name to full model ID.

    Args:
        model: Short name (haiku, sonnet, opus)

    Returns:
        Full model ID
    """
    return MODEL_MAP.get(model, MODEL_MAP['sonnet'])


def calculate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """
    Calculate cost in USD for LLM call.

    Args:
        model_id: Full model ID
        input_tokens: Input token count
        output_tokens: Output token count

    Returns:
        Cost in USD
    """
    if model_id not in COSTS:
        logger.warning(f"Unknown model for cost calculation: {model_id}")
        return 0.0

    costs = COSTS[model_id]
    input_cost = (input_tokens / 1_000_000) * costs['input']
    output_cost = (output_tokens / 1_000_000) * costs['output']

    return input_cost + output_cost


def call_llm(
    prompt: str,
    model: str = 'sonnet',
    max_tokens: int = 4096,
    temperature: float = 0.0,
    system: Optional[str] = None
) -> Dict[str, Any]:
    """
    Call Claude LLM with proper error classification.

    Uses kimi CLI by default if available, falls back to Anthropic SDK.
    Set KB_LLM_BACKEND=anthropic to force Anthropic SDK usage.

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
        TransientError: For retryable failures (rate limits, timeouts)
        PermanentError: For permanent failures (auth, context exceeded)
    """
    # Try claude CLI first if enabled
    if USE_KIMI:
        try:
            from kb.llm_claude import call_llm_with_kimi
            return call_llm_with_kimi(prompt, model, max_tokens, temperature, system)
        except Exception as e:
            # Fall back to Anthropic SDK on any claude CLI failure
            logger.warning(f"claude CLI failed ({type(e).__name__}: {e}), falling back to Anthropic SDK")

    # Use Anthropic SDK
    try:
        import anthropic
    except ImportError:
        raise PermanentError(
            "Neither kimi CLI nor anthropic package available. "
            "Install kimi or run: pip install anthropic"
        )

    model_id = map_model_name(model)

    try:
        client = anthropic.Anthropic()

        # Build message
        messages = [{'role': 'user', 'content': prompt}]

        # Build request parameters
        params = {
            'model': model_id,
            'max_tokens': max_tokens,
            'temperature': temperature,
            'messages': messages
        }

        if system:
            params['system'] = system

        # Call API
        response = client.messages.create(**params)

        # Extract content
        content = ''
        for block in response.content:
            if hasattr(block, 'text'):
                content += block.text

        # Calculate cost
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost_usd = calculate_cost(model_id, input_tokens, output_tokens)

        logger.info(
            f"LLM call successful: {model} "
            f"({input_tokens} in, {output_tokens} out, ${cost_usd:.4f})"
        )

        return {
            'content': content,
            'model': model_id,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cost_usd': cost_usd
        }

    except anthropic.RateLimitError as e:
        # 429 - rate limited, retry with backoff
        raise TransientError(f"Rate limited: {e}") from e

    except anthropic.APITimeoutError as e:
        # Network timeout, retryable
        raise TransientError(f"API timeout: {e}") from e

    except anthropic.APIConnectionError as e:
        # Network issue, retryable
        raise TransientError(f"Connection error: {e}") from e

    except anthropic.InternalServerError as e:
        # 5xx errors, retryable
        raise TransientError(f"Server error: {e}") from e

    except anthropic.AuthenticationError as e:
        # 401 - bad API key, not retryable
        raise PermanentError(f"Authentication failed: {e}") from e

    except anthropic.PermissionDeniedError as e:
        # 403 - insufficient permissions, not retryable
        raise PermanentError(f"Permission denied: {e}") from e

    except anthropic.BadRequestError as e:
        # 400 - malformed request or context too long
        error_msg = str(e).lower()
        if 'context' in error_msg or 'token' in error_msg or 'length' in error_msg:
            raise PermanentError(f"Context window exceeded: {e}") from e
        else:
            raise PermanentError(f"Bad request: {e}") from e

    except Exception as e:
        # Unknown error - treat as permanent to avoid infinite retries
        raise PermanentError(f"Unexpected error: {e}") from e


def parse_llm_output(raw_output: str, expected_format: str = 'json') -> Any:
    """
    Parse LLM output.

    Args:
        raw_output: Raw LLM response text
        expected_format: Expected format (json or text)

    Returns:
        Parsed output (dict for JSON, str for text)

    Raises:
        PermanentError: If output is not valid (not retryable)
    """
    if expected_format == 'text':
        return raw_output.strip()

    # JSON format - extract from markdown code block if present
    try:
        # Try to extract JSON from markdown code block
        if '```json' in raw_output:
            start = raw_output.find('```json') + 7
            end = raw_output.find('```', start)
            if end == -1:
                # No closing backticks
                raise ValueError("Incomplete JSON code block")
            json_str = raw_output[start:end].strip()
        elif '```' in raw_output:
            # Generic code block
            start = raw_output.find('```') + 3
            end = raw_output.find('```', start)
            if end == -1:
                raise ValueError("Incomplete code block")
            json_str = raw_output[start:end].strip()
        else:
            # No code block, try parsing whole output
            json_str = raw_output.strip()

        return json.loads(json_str)

    except (json.JSONDecodeError, ValueError) as e:
        # LLM returned garbage - this won't improve with retry
        raise PermanentError(
            f"Could not parse LLM output as JSON: {e}\n"
            f"Raw output:\n{raw_output[:500]}"
        ) from e


def estimate_tokens(text: str) -> int:
    """
    Estimate token count for text.

    Rough approximation: 1 token ≈ 4 characters for English text.

    Args:
        text: Input text

    Returns:
        Estimated token count
    """
    return len(text) // 4


def check_token_limit(prompt: str, model: str, max_tokens: int = 10000) -> bool:
    """
    Check if prompt is within token limit for model.

    Args:
        prompt: Input prompt
        model: Model name
        max_tokens: Maximum allowed tokens

    Returns:
        True if within limit, False otherwise
    """
    estimated = estimate_tokens(prompt)

    if estimated > max_tokens:
        logger.warning(
            f"Prompt may exceed token limit: ~{estimated} tokens "
            f"(limit: {max_tokens})"
        )
        return False

    return True
