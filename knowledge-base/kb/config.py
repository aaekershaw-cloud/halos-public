"""Configuration management"""

import os
import yaml
from pathlib import Path
from typing import Dict, Any


_config_cache = None
_config_mtime = None
_config_path_cached = None


def load_config(config_path: str = None) -> Dict[str, Any]:
    """
    Load configuration from YAML file.

    Caches the result and reloads automatically if the file changes on disk.

    Args:
        config_path: Path to config file (default: ./config.yaml)

    Returns:
        Configuration dictionary
    """
    global _config_cache, _config_mtime, _config_path_cached

    if config_path is None:
        config_path = Path(__file__).parent.parent / 'config.yaml'

    config_path = Path(config_path)

    # Check if cached config is still valid
    if _config_cache is not None and _config_path_cached == str(config_path):
        try:
            current_mtime = config_path.stat().st_mtime
            if current_mtime == _config_mtime:
                return _config_cache
        except OSError:
            pass

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    # Expand environment variables in strings
    config = _expand_env_vars(config)

    # Cache with mtime for invalidation
    _config_cache = config
    _config_mtime = config_path.stat().st_mtime
    _config_path_cached = str(config_path)

    return config


def reload_config():
    """Force reload configuration from disk."""
    global _config_cache, _config_mtime
    _config_cache = None
    _config_mtime = None
    return load_config()


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${VAR} environment variables in config"""
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    elif isinstance(obj, str):
        # Replace ${VAR} with environment variable
        if '${' in obj:
            import re
            def replacer(match):
                var_name = match.group(1)
                return os.environ.get(var_name, match.group(0))
            return re.sub(r'\$\{(\w+)\}', replacer, obj)
    return obj


def get_project_root() -> Path:
    """Get knowledge base project root directory"""
    config = load_config()
    root = config.get('knowledge_base', {}).get('root', '~/Projects/knowledge-base')
    return Path(root).expanduser()


def get_db_path() -> Path:
    """Get SQLite database path"""
    root = get_project_root()
    config = load_config()
    db_path = config.get('knowledge_base', {}).get('db_path', '.kb/kb.db')
    return root / db_path
