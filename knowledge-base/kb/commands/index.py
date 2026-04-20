"""Index command implementation — generate abbreviated KB indexes for agents."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Default output locations for HalOS agents
AGENT_SESSION_DIRS = {
    'gamma': Path.home() / 'Projects/halos/sessions/gamma',
    'alpha': Path.home() / 'Projects/halos/sessions/alpha',
    'beta': Path.home() / 'Projects/halos/sessions/beta',
}


def cmd_index(agent: str = None, output: str = None, all_agents: bool = False):
    """
    Generate abbreviated KB index.

    Args:
        agent: Agent name (writes to agent's session dir by default)
        output: Override output path
        all_agents: Generate indexes for all configured agents
    """
    from kb.index import generate_index

    if all_agents:
        _generate_all(output_override=output)
        return

    if agent and agent not in AGENT_SESSION_DIRS and not output:
        print(f"✗ Unknown agent '{agent}'. Known agents: {', '.join(AGENT_SESSION_DIRS.keys())}")
        print("  Use --output to specify a custom path.")
        return

    # Determine output path
    output_path = None
    if output:
        output_path = Path(output)
    elif agent and agent in AGENT_SESSION_DIRS:
        output_path = AGENT_SESSION_DIRS[agent] / 'kb-index.md'

    index = generate_index(agent=agent, output_path=output_path)

    if output_path:
        print(f"✓ Index written to {output_path}")
    else:
        # Print to stdout if no output path
        print(index)


def _generate_all(output_override: str = None):
    """Generate indexes for all known agents."""
    from kb.index import generate_index

    for agent_name, session_dir in AGENT_SESSION_DIRS.items():
        if output_override:
            out = Path(output_override) / f'{agent_name}-kb-index.md'
        else:
            out = session_dir / 'kb-index.md'

        if not session_dir.exists() and not output_override:
            print(f"⚠ Session dir not found for {agent_name}: {session_dir}")
            continue

        generate_index(agent=agent_name, output_path=out)
        print(f"✓ {agent_name}: {out}")

    print(f"\nGenerated indexes for {len(AGENT_SESSION_DIRS)} agents.")
