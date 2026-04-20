# LLM Backend Options

The knowledge base supports two ways to call Claude:

## Option 1: kimi CLI (Recommended for HalOS integration)

If you have kimi installed and configured, the KB will use it automatically.

**Advantages:**
- No separate API key needed in KB
- Uses your existing kimi configuration
- Integrates seamlessly with HalOS workflow
- Same authentication as your other kimi sessions

**Setup:**
1. Install kimi: https://github.com/example/kimi
2. Configure kimi with your API key
3. KB will auto-detect and use kimi

**Test:**
```bash
kimi --print -p "Say hello"
```

If that works, KB will work.

## Option 2: Anthropic SDK (Direct API)

Use Anthropic's Python SDK directly.

**Advantages:**
- Simple setup
- No additional tools needed
- Direct API access

**Setup:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**Test:**
```bash
python3 -c "import anthropic; print('SDK ready')"
```

## How the KB Chooses

The KB automatically tries:
1. **kimi CLI** first (if `KB_LLM_BACKEND` != "anthropic")
2. Falls back to **Anthropic SDK** if kimi not available
3. Error if neither is available

## Force a Specific Backend

```bash
# Force Anthropic SDK (skip kimi)
export KB_LLM_BACKEND=anthropic
python3 -m kb.cli compile file <id>

# Use kimi (default)
unset KB_LLM_BACKEND
python3 -m kb.cli compile file <id>
```

## Current Status

Your setup:
- ✓ Anthropic SDK available
- ✓ kimi CLI configured and working (OAuth authentication)

**Active:** KB is using kimi CLI by default (no API key needed).

**Test:**
```bash
python3 -m kb.cli compile file <raw-file-id> --auto-approve
```

This will use kimi's OAuth credentials automatically.

## Benefits of kimi Integration

Once kimi is configured:
- HalOS agents can compile KB articles using their existing auth
- No need to set KB_ANTHROPIC_API_KEY separately
- Unified authentication across all your AI tools
- kimi session continuity (same conversation context)
