"""Model selection for HalOS tasks - automatically picks appropriate models based on task difficulty."""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ModelConfig:
    """Configuration for a model tier."""
    name: str
    description: str
    max_tokens: int = 4096
    cost_tier: str = "medium"


# Kimi model configurations
KIMI_MODELS = {
    # High-capability models for complex tasks
    "k2.5": ModelConfig(
        name="moonshotai/kimi-k2.5",
        description="Kimi K2.5 - Best for complex reasoning, creative writing, social media",
        max_tokens=8192,
        cost_tier="high"
    ),
    
    # Standard models for general tasks
    "kimi-128k": ModelConfig(
        name="moonshot-v1-128k",
        description="Kimi 128k - Good balance of capability and cost",
        max_tokens=4096,
        cost_tier="medium"
    ),
    
    "kimi-32k": ModelConfig(
        name="moonshot-v1-32k",
        description="Kimi 32k - Standard model for most tasks",
        max_tokens=4096,
        cost_tier="medium"
    ),
    
    # Lightweight models for simple tasks
    "kimi-8k": ModelConfig(
        name="moonshot-v1-8k",
        description="Kimi 8k - Fast and cheap for simple tasks",
        max_tokens=4096,
        cost_tier="low"
    ),
    
    # Flash model for quick/simple tasks
    "kimi-flash": ModelConfig(
        name="kimi/kimi-flash",
        description="Kimi Flash - Ultra-fast for simple completions",
        max_tokens=2048,
        cost_tier="lowest"
    ),
}


# Task patterns that indicate complexity
task_patterns = {
    "social_media": [
        r"social\s*media",
        r"tweet",
        r"post",
        r"instagram",
        r"linkedin",
        r"facebook",
        r"caption",
        r"hashtag",
        r"engagement",
        r"viral",
        r"content\s*creation",
        r"marketing\s*copy",
        r"ad\s*copy",
        r"promotional",
    ],
    
    "complex_reasoning": [
        r"analyze",
        r"research",
        r"investigate",
        r"strategize",
        r"architect",
        r"design",
        r"plan",
        r"complex",
        r"multi[- ]?step",
        r"reasoning",
        r"inference",
        r"debug",
        r"troubleshoot",
        r"optimize",
    ],
    
    "creative_writing": [
        r"write",
        r"draft",
        r"compose",
        r"create",
        r"story",
        r"blog",
        r"article",
        r"essay",
        r"narrative",
        r"script",
        r"creative",
    ],
    
    "code": [
        r"code",
        r"program",
        r"develop",
        r"implement",
        r"refactor",
        r"function",
        r"class",
        r"module",
        r"api",
        r"integration",
    ],
    
    "simple": [
        r"summarize",
        r"extract",
        r"format",
        r"convert",
        r"parse",
        r"list",
        r"count",
        r"find",
        r"get",
        r"fetch",
        r"check",
        r"monitor",
        r"status",
        r"simple",
        r"quick",
        r"brief",
    ],
}


def detect_task_type(prompt: str) -> str:
    """Detect the type of task from the prompt."""
    prompt_lower = prompt.lower()
    
    scores = {}
    for task_type, patterns in task_patterns.items():
        score = 0
        for pattern in patterns:
            matches = len(re.findall(pattern, prompt_lower))
            score += matches
        scores[task_type] = score
    
    # Return the task type with highest score
    if max(scores.values()) > 0:
        return max(scores, key=scores.get)
    
    return "general"


def estimate_complexity(prompt: str) -> tuple[str, float]:
    """Estimate task complexity based on prompt characteristics.
    
    Returns:
        Tuple of (complexity_level, confidence_score)
    """
    complexity_score = 0
    prompt_lower = prompt.lower()
    word_count = len(prompt.split())
    
    # Length factors
    if word_count > 500:
        complexity_score += 3
    elif word_count > 200:
        complexity_score += 2
    elif word_count > 50:
        complexity_score += 1
    
    # Complexity indicators
    complex_indicators = [
        r"compare.*contrast",
        r"pros?\s+and\s+cons?",
        r"evaluate",
        r"synthesize",
        r"comprehensive",
        r"detailed",
        r"in[- ]?depth",
        r"thorough",
        r"extensive",
        r"complex",
        r"sophisticated",
        r"nuanced",
    ]
    
    for indicator in complex_indicators:
        if re.search(indicator, prompt_lower):
            complexity_score += 2
    
    # Context indicators (more context = more complex)
    context_indicators = [
        r"based on",
        r"considering",
        r"taking into account",
        r"given that",
        r"with respect to",
        r"in the context of",
    ]
    
    for indicator in context_indicators:
        if re.search(indicator, prompt_lower):
            complexity_score += 1
    
    # Simple task indicators reduce complexity
    simple_indicators = [
        r"simple",
        r"brief",
        r"quick",
        r"short",
        r"one sentence",
        r"one word",
        r"yes/no",
    ]
    
    for indicator in simple_indicators:
        if re.search(indicator, prompt_lower):
            complexity_score -= 2
    
    # Determine level
    if complexity_score >= 6:
        return ("high", min(complexity_score / 10, 1.0))
    elif complexity_score >= 3:
        return ("medium", min(complexity_score / 6, 1.0))
    else:
        return ("low", max(0.3, 1.0 - abs(complexity_score) / 5))


def select_model(
    prompt: str,
    task_type_hint: Optional[str] = None,
    force_model: Optional[str] = None,
    prefer_cheap: bool = False,
) -> str:
    """Select the most appropriate model for a given task.
    
    Args:
        prompt: The task prompt/description
        task_type_hint: Optional hint about task type (social_media, code, etc.)
        force_model: Optional model to force (overrides selection)
        prefer_cheap: If True, prefer cheaper models when uncertain
    
    Returns:
        Model identifier string
    """
    if force_model:
        return force_model
    
    # Detect task type
    detected_type = task_type_hint or detect_task_type(prompt)
    complexity, confidence = estimate_complexity(prompt)
    
    # Social media posts always get K2.5 for best quality
    if detected_type == "social_media":
        return KIMI_MODELS["k2.5"].name
    
    # Code tasks benefit from strong reasoning
    if detected_type == "code":
        if complexity == "high":
            return KIMI_MODELS["k2.5"].name
        elif complexity == "medium":
            return KIMI_MODELS["kimi-128k"].name
        else:
            return KIMI_MODELS["kimi-32k"].name
    
    # Creative writing tasks
    if detected_type == "creative_writing":
        if complexity == "high" or (complexity == "medium" and not prefer_cheap):
            return KIMI_MODELS["k2.5"].name
        elif complexity == "medium":
            return KIMI_MODELS["kimi-128k"].name
        else:
            return KIMI_MODELS["kimi-32k"].name
    
    # Complex reasoning tasks
    if detected_type == "complex_reasoning":
        if complexity == "high":
            return KIMI_MODELS["k2.5"].name
        elif complexity == "medium":
            return KIMI_MODELS["kimi-128k"].name
        else:
            return KIMI_MODELS["kimi-32k"].name
    
    # Simple tasks - use lightweight models
    if detected_type == "simple" or complexity == "low":
        if prefer_cheap and confidence > 0.7:
            return KIMI_MODELS["kimi-flash"].name
        return KIMI_MODELS["kimi-8k"].name
    
    # General tasks - balanced selection based on complexity
    if complexity == "high":
        return KIMI_MODELS["k2.5"].name
    elif complexity == "medium":
        if prefer_cheap:
            return KIMI_MODELS["kimi-32k"].name
        return KIMI_MODELS["kimi-128k"].name
    else:
        return KIMI_MODELS["kimi-8k"].name


def get_model_info(model_name: str) -> ModelConfig:
    """Get configuration for a specific model."""
    for config in KIMI_MODELS.values():
        if config.name == model_name:
            return config
    # Return default if not found
    return KIMI_MODELS["kimi-32k"]


def should_use_extended_context(prompt: str) -> bool:
    """Determine if the task would benefit from extended context."""
    word_count = len(prompt.split())
    
    # Check for long content
    if word_count > 8000:
        return True
    
    # Check for code blocks (often need more context)
    code_block_count = prompt.count("```")
    if code_block_count >= 4:  # At least 2 code blocks
        return True
    
    # Check for multiple documents/data sections
    section_markers = ["---", "# ", "## ", "### ", "Document", "Section"]
    section_count = sum(prompt.count(marker) for marker in section_markers)
    if section_count > 10:
        return True
    
    return False
