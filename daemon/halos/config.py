"""Configuration loading for HalOS."""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv


@dataclass
class AnthropicConfig:
    model: str = "moonshotai/kimi-k2.5"
    model_heavy: str = "anthropic/claude-opus-4-7"
    max_tokens: int = 4096
    api_key: str = ""


@dataclass
class KimiConfig:
    model: str = "moonshot-v1-8k"
    max_tokens: int = 4096
    api_key: str = ""


@dataclass
class TelegramConfig:
    bot_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    typing_indicator: bool = True
    max_message_length: int = 4000


@dataclass
class ClaudeCodeConfig:
    enabled: bool = True
    binary_path: str = "claude"
    default_project_dir: str = "~/Projects"
    general_session_dir: str = "~/.halos"
    skip_permissions: bool = True
    timeout_seconds: int = 300
    default_model: str = "claude-opus-4-7"
    code_model: str = "claude-sonnet-4-6"
    heavy_model: str = "claude-opus-4-7"
    progress_debounce_secs: int = 5
    max_output_chars: int = 50000
    projects: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)
    remote_host: str = ""            # SSH alias or user@host (e.g. "my-remote")
    remote_binary_path: str = ""     # path to claude on remote (e.g. "/opt/homebrew/bin/claude")


@dataclass
class SchedulerConfig:
    timezone: str = "America/Edmonton"
    startup_tasks: list[str] = field(default_factory=list)


@dataclass
class MemoryConfig:
    auto_extract: bool = False
    max_conversation_history: int = 50
    summary_after: int = 30


@dataclass
class NotificationConfig:
    telegram_chat_id: int = None
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"


@dataclass
class AppStoreConnectConfig:
    key_id: str = ""
    issuer_id: str = ""
    private_key_path: str = ""
    app_bundle_id: str = "com.example.app"
    app_id: str = ""  # optional override


@dataclass
class AgentConfig:
    bot_token: str = ""      # Telegram bot token
    project_dir: str = ""    # working directory; personality.md auto-loaded from here
    provider: str = ""       # "claude" | "kimi" | "openrouter"
    model: str = ""          # override model for this agent
    remote: bool = False             # execute on remote machine via SSH
    remote_project_dir: str = ""     # project_dir on the remote machine (cwd for claude)


@dataclass
class Config:
    name: str = "Hal"
    log_level: str = "INFO"
    db_path: str = "~/.halos/halos.db"
    memory_path: str = "~/.halos/memory.md"
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    kimi: KimiConfig = field(default_factory=KimiConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    claude_code: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    appstore_connect: AppStoreConnectConfig = field(default_factory=AppStoreConnectConfig)
    agents: dict = field(default_factory=dict)


def load_config(config_path: str = "config/config.yaml") -> Config:
    """Load configuration from YAML file and environment variables."""
    load_dotenv()

    config = Config()

    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        daemon = raw.get("daemon", {})
        config.name = daemon.get("name", config.name)
        config.log_level = daemon.get("log_level", config.log_level)
        config.db_path = daemon.get("db_path", config.db_path)
        config.memory_path = daemon.get("memory_path", config.memory_path)

        anth = raw.get("anthropic", {})
        config.anthropic.model = anth.get("model", config.anthropic.model)
        config.anthropic.model_heavy = anth.get("model_heavy", config.anthropic.model_heavy)
        config.anthropic.max_tokens = anth.get("max_tokens", config.anthropic.max_tokens)

        kimi = raw.get("kimi", {})
        config.kimi.model = kimi.get("model", config.kimi.model)
        config.kimi.max_tokens = kimi.get("max_tokens", config.kimi.max_tokens)

        tg = raw.get("telegram", {})
        config.telegram.allowed_user_ids = tg.get("allowed_user_ids", [])
        config.telegram.typing_indicator = tg.get("typing_indicator", True)
        config.telegram.max_message_length = tg.get("max_message_length", 4000)

        cc = raw.get("claude_code", {})
        config.claude_code.enabled = cc.get("enabled", config.claude_code.enabled)
        config.claude_code.binary_path = cc.get("binary_path", config.claude_code.binary_path)
        config.claude_code.default_project_dir = cc.get("default_project_dir", config.claude_code.default_project_dir)
        config.claude_code.general_session_dir = cc.get("general_session_dir", config.claude_code.general_session_dir)
        config.claude_code.skip_permissions = cc.get("skip_permissions", config.claude_code.skip_permissions)
        config.claude_code.timeout_seconds = cc.get("timeout_seconds", config.claude_code.timeout_seconds)
        config.claude_code.default_model = cc.get("default_model", config.claude_code.default_model)
        config.claude_code.code_model = cc.get("code_model", config.claude_code.code_model)
        config.claude_code.heavy_model = cc.get("heavy_model", config.claude_code.heavy_model)
        config.claude_code.progress_debounce_secs = cc.get("progress_debounce_secs", config.claude_code.progress_debounce_secs)
        config.claude_code.max_output_chars = cc.get("max_output_chars", config.claude_code.max_output_chars)
        config.claude_code.projects = cc.get("projects", {})
        config.claude_code.aliases = cc.get("aliases", {})
        config.claude_code.remote_host = cc.get("remote_host", "")
        config.claude_code.remote_binary_path = cc.get("remote_binary_path", "")

        sched = raw.get("scheduler", {})
        config.scheduler.timezone = sched.get("timezone", config.scheduler.timezone)
        config.scheduler.startup_tasks = sched.get("startup_tasks", [])

        mem = raw.get("memory", {})
        config.memory.auto_extract = mem.get("auto_extract", config.memory.auto_extract)
        config.memory.max_conversation_history = mem.get("max_conversation_history", 50)
        config.memory.summary_after = mem.get("summary_after", 30)

        notif = raw.get("notifications", {})
        config.notifications.telegram_chat_id = notif.get("telegram_chat_id")
        qh = notif.get("quiet_hours", {})
        config.notifications.quiet_hours_start = qh.get("start", "22:00")
        config.notifications.quiet_hours_end = qh.get("end", "07:00")

        asc = raw.get("appstore_connect", {})
        config.appstore_connect.key_id = asc.get("key_id", "")
        config.appstore_connect.issuer_id = asc.get("issuer_id", "")
        config.appstore_connect.private_key_path = asc.get("private_key_path", "")
        config.appstore_connect.app_bundle_id = asc.get("app_bundle_id", "com.example.app")
        config.appstore_connect.app_id = asc.get("app_id", "")

        # Load secrets.yaml for agent tokens
        secrets = {}
        secrets_paths = [
            Path.home() / ".halos" / "secrets.yaml",
            Path("config/secrets.yaml")
        ]
        for secrets_path in secrets_paths:
            if secrets_path.exists():
                with open(secrets_path) as f:
                    secrets = yaml.safe_load(f) or {}
                break

        agent_tokens = secrets.get("agent_tokens", {})

        agents_raw = raw.get("agents", {})
        for agent_name, agent_data in (agents_raw or {}).items():
            # Merge token from secrets.yaml if not in config.yaml
            bot_token = agent_data.get("bot_token", "")
            if not bot_token and agent_name in agent_tokens:
                bot_token = agent_tokens[agent_name]

            config.agents[agent_name] = AgentConfig(
                bot_token=bot_token,
                project_dir=agent_data.get("project_dir", ""),
                provider=agent_data.get("provider", ""),
                model=agent_data.get("model", ""),
                remote=agent_data.get("remote", False),
                remote_project_dir=agent_data.get("remote_project_dir", ""),
            )

    # Environment variables override config file
    config.anthropic.api_key = os.environ.get("OPENROUTER_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
    config.kimi.api_key = os.environ.get("KIMI_API_KEY", "")
    config.telegram.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    # Expand paths
    config.db_path = str(Path(config.db_path).expanduser())
    config.memory_path = str(Path(config.memory_path).expanduser())
    config.claude_code.default_project_dir = str(Path(config.claude_code.default_project_dir).expanduser())

    # Ensure directories exist
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)

    return config
