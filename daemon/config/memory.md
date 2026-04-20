# HalOS Memory

## Remote Hosts

You may configure remote hosts in `config/config.yaml` under `claude_code.remote_host`.
Agents can execute commands on remote machines via SSH:

```bash
ssh <remote-alias> "<command>"
```

## Environment

- Primary daemon machine: local macOS
- Python 3.11+ required
- Claude Code CLI must be installed
- Optional: Kimi CLI for alternative engine
