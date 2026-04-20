"""
Agent Manager - Programmatic agent add/remove for HalOS daemon

This module provides functions to add and remove agents from HalOS configuration
without manually editing config.yaml. Used by the halos-telegram plugin.

Design principles:
- Atomic writes (write to .tmp, rename) to prevent corruption
- Backup config.yaml before modification
- Tokens stored in secrets.yaml (chmod 600), never in config.yaml
- Reload marker written to trigger daemon hot-reload
"""

import os
import yaml
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

# Paths resolved at import time for testability
HALOS_ROOT = Path("~/Projects/halos")
CONFIG_PATH = HALOS_ROOT / "config" / "config.yaml"


def add_agent(
    name: str,
    bot_token: str,
    username: str,
    model: str = "claude-sonnet-4-6",
    project_dir: Optional[str] = None
) -> None:
    """
    Add a new agent to HalOS configuration.

    Args:
        name: Agent name (used as key in config.yaml)
        bot_token: Telegram bot token
        username: Telegram bot username (for display/link purposes)
        model: Claude model to use (default: claude-sonnet-4-6)
        project_dir: Session directory path (default: ~/Projects/halos/sessions/{name})

    Raises:
        ValueError: If agent already exists or validation fails
        IOError: If file operations fail
    """
    # Validate inputs
    if not name or not name.strip():
        raise ValueError("Agent name cannot be empty")

    if not bot_token or not bot_token.strip():
        raise ValueError("Bot token cannot be empty")

    # Validate token format (basic check: number:alphanumeric)
    import re
    if not re.match(r'^\d+:[\w-]+$', bot_token):
        raise ValueError(f"Invalid bot token format: {bot_token[:20]}...")

    # Default project directory
    if not project_dir:
        project_dir = str(HALOS_ROOT / "sessions" / name)

    # Paths
    halos_home = Path.home() / ".halos"
    halos_home.mkdir(exist_ok=True)

    secrets_path = halos_home / "secrets.yaml"
    reload_marker = halos_home / "reload_agents"

    # Read current config
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")

    with open(CONFIG_PATH, 'r') as f:
        config = yaml.safe_load(f) or {}

    # Check if agent already exists
    if 'agents' in config and name in config['agents']:
        raise ValueError(f"Agent '{name}' already exists in config.yaml")

    # Backup config
    backup_path = CONFIG_PATH.with_suffix('.yaml.bak')
    shutil.copy2(CONFIG_PATH, backup_path)

    # Add agent entry to config
    if 'agents' not in config:
        config['agents'] = {}

    config['agents'][name] = {
        'project_dir': project_dir,
        'model': model
    }

    # Add to claude_code.projects
    if 'claude_code' not in config:
        config['claude_code'] = {}
    if 'projects' not in config['claude_code']:
        config['claude_code']['projects'] = {}

    config['claude_code']['projects'][name] = project_dir

    # Write config atomically
    config_tmp = CONFIG_PATH.with_suffix('.yaml.tmp')
    with open(config_tmp, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    config_tmp.rename(CONFIG_PATH)

    # Write token to secrets.yaml
    if secrets_path.exists():
        with open(secrets_path, 'r') as f:
            secrets = yaml.safe_load(f) or {}
    else:
        secrets = {}

    if 'agent_tokens' not in secrets:
        secrets['agent_tokens'] = {}

    secrets['agent_tokens'][name] = bot_token

    # Write secrets atomically
    secrets_tmp = secrets_path.with_suffix('.yaml.tmp')
    with open(secrets_tmp, 'w') as f:
        yaml.dump(secrets, f, default_flow_style=False)

    secrets_tmp.rename(secrets_path)

    # Set chmod 600 on secrets.yaml
    os.chmod(secrets_path, 0o600)

    # Create session directory
    session_dir = Path(project_dir)
    session_dir.mkdir(parents=True, exist_ok=True)

    # Write CLAUDE.md template
    claude_md = session_dir / "CLAUDE.md"
    claude_content = f"""# {name} Agent

Session bot created by halos-telegram plugin on {datetime.now().strftime('%Y-%m-%d')}.

## Operating Mode
You are {name}, a HalOS agent running on Telegram.
Your project directory is: {project_dir}

Bot username: @{username}

## Memory
Use `kb memory` commands for persistent memory (see main HalOS CLAUDE.md).

## Multi-Session Awareness
You are one of potentially several active sessions. If asked about topics
outside your domain, suggest switching sessions via @{username}.
"""

    with open(claude_md, 'w') as f:
        f.write(claude_content)

    # Write soul.md template
    soul_md = session_dir / "soul.md"
    soul_content = f"""# {name}

A HalOS session agent. Helpful, concise, and direct.

Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Bot: @{username}
"""

    with open(soul_md, 'w') as f:
        f.write(soul_content)

    # Write reload marker
    with open(reload_marker, 'w') as f:
        f.write(f"{datetime.now().timestamp()}\n")

    print(f"Agent '{name}' added successfully")
    print(f"  Token: stored in {secrets_path}")
    print(f"  Config: {CONFIG_PATH}")
    print(f"  Session: {session_dir}")
    print(f"  Reload marker: {reload_marker}")


def remove_agent(name: str) -> None:
    """
    Remove an agent from HalOS configuration.

    Args:
        name: Agent name to remove

    Raises:
        ValueError: If agent is a core agent or doesn't exist
        IOError: If file operations fail
    """
    # Protect core agents
    core_agents = ['alpha', 'gamma', 'beta', 'kbagent']
    if name.lower() in core_agents:
        raise ValueError(f"Cannot remove core agent: {name}")

    # Paths
    halos_home = Path.home() / ".halos"
    secrets_path = halos_home / "secrets.yaml"
    reload_marker = halos_home / "reload_agents"

    # Read current config
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")

    with open(CONFIG_PATH, 'r') as f:
        config = yaml.safe_load(f) or {}

    # Check if agent exists
    if 'agents' not in config or name not in config['agents']:
        raise ValueError(f"Agent '{name}' not found in config.yaml")

    # Get project_dir before removal
    project_dir = config['agents'][name].get('project_dir')

    # Backup config
    backup_path = CONFIG_PATH.with_suffix('.yaml.bak')
    shutil.copy2(CONFIG_PATH, backup_path)

    # Remove from config
    del config['agents'][name]

    # Remove from claude_code.projects
    if 'claude_code' in config and 'projects' in config['claude_code']:
        if name in config['claude_code']['projects']:
            del config['claude_code']['projects'][name]

    # Write config atomically
    config_tmp = CONFIG_PATH.with_suffix('.yaml.tmp')
    with open(config_tmp, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    config_tmp.rename(CONFIG_PATH)

    # Remove token from secrets.yaml
    if secrets_path.exists():
        with open(secrets_path, 'r') as f:
            secrets = yaml.safe_load(f) or {}

        if 'agent_tokens' in secrets and name in secrets['agent_tokens']:
            del secrets['agent_tokens'][name]

            # Write secrets atomically
            secrets_tmp = secrets_path.with_suffix('.yaml.tmp')
            with open(secrets_tmp, 'w') as f:
                yaml.dump(secrets, f, default_flow_style=False)

            secrets_tmp.rename(secrets_path)
            os.chmod(secrets_path, 0o600)

    # Remove session directory
    if project_dir:
        session_dir = Path(project_dir)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    # Write reload marker
    with open(reload_marker, 'w') as f:
        f.write(f"{datetime.now().timestamp()}\n")

    print(f"Agent '{name}' removed successfully")
    print(f"  Removed from: {CONFIG_PATH}")
    print(f"  Token removed from: {secrets_path}")
    if project_dir:
        print(f"  Session directory deleted: {project_dir}")
    print(f"  Reload marker: {reload_marker}")
