# AIPM — AI Project Manager

Autonomous AI engineering workforce for GitHub projects. Tracks issues across multiple repos, automatically picks up work, executes with the right AI model, runs quality checks, and notifies you via Telegram when decisions are needed.

## Quick Start

```bash
# Install
pip install -e .

# Initialize database
aipm init

# Configure (copy and edit)
cp config.example.toml config.toml

# Sync issues from GitHub
aipm sync

# List tracked issues
aipm list

# Manually work on an issue
aipm work owner/repo#123

# Start the daemon (scheduler + dashboard)
aipm run
```

## Architecture

- **Human** = CEO — sets vision, makes key decisions
- **Opus** = Engineering Manager — prioritizes, reviews, plans
- **Sonnet** = Senior Engineer — standard coding tasks
- **Haiku** = Junior Engineer — classification, simple fixes

## Configuration

See `config.example.toml` for all options. Secrets are loaded from environment variables:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_TOKEN=ghp_...
export TELEGRAM_BOT_TOKEN=123456:ABC...
export TELEGRAM_CHAT_ID=your_chat_id
```

## Dashboard

Start the daemon with `aipm run` and visit http://localhost:8000.
