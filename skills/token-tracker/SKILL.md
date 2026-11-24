---
name: token-tracker
description: >-
  Track, inspect, and visualize per-conversation and global token usage, prompt
  cache hit rates, 5-category context window composition, multi-agent Swarm
  fleet telemetry, file/command hotspots, and AI tool behaviors (inspired by
  dsh-context). Activate when the user asks about token consumption, context
  window pressure, cache hit rates, AI tool usage statistics, exporting usage
  reports, or opening the Token & AI Behavior Tracker dashboard.
---

# Token & AI Behavior Tracker Skill (`token-tracker`)

Use this skill whenever the user wants to inspect or visualize **token usage**, **prompt cache efficiency**, **context window composition**, **multi-agent Swarm fleet consumption**, or **AI tool execution behaviors** for the current conversation or across all recorded conversations.

## Available MCP Tools (Server: `token-behavior-tracker_tracker`)

1. **`open_token_dashboard`** (Interactive MCP App UI)
   - Opens the visual **Token & AI Tracker** dashboard (supports in-conversation side pane, full-page global sidebar, and inline view).
   - Call this tool first whenever the user asks to "打开面板", "查看 Token 仪表盘", "看看上下文构成图", or wants an interactive visual view.
   - Arguments (all optional):
     - `conversation_id` (string): Target a specific conversation ID (omit for global overview or current/latest conversation).
     - `tab` (`"global"` | `"session"` | `"fleet"` | `"settings"`): Initial view tab to open.
     - `days` (integer): Time window in days for the global trend chart (default `7`).

2. **`get_token_behavior_report`** (Model Diagnostic Query)
   - Syncs all `<appDataDir>/brain/*/.system_generated/logs/transcript.jsonl` files into the SQLite telemetry database and returns a structured Markdown report.
   - Call this tool when the user asks questions in chat such as:
     - "当前对话消耗了多少 Token？缓存命中率是多少？"
     - "为什么这轮上下文增长这么快？哪些工具或文件占用了最多上下文？"
     - "这个 Swarm 多 Agent 任务总共花了多少 Token？每个子 Agent 占比多少？"
     - "最近 7 天所有对话一共消耗了多少 Token？"
   - Arguments:
     - `conversation_id` (string, optional): Specific conversation ID ( you can pass the active `Conversation ID` from `<user_information>` when analyzing the current session).
     - `scope` (`"session"` | `"global"` | `"both"`, default `"both"`).
     - `days` (integer, default `7`).

3. **`export_token_report`** (Markdown / JSON / CSV Exporter)
   - Exports a full telemetry report in `"markdown"`, `"json"`, or `"csv"` format and optionally writes it to `output_path`.
   - Call this tool when the user asks to save or export their token usage history or session diagnostics to a file.

## Recommended Workflow

1. **For Current Conversation Analysis**:
   - Look up the current `Conversation ID` in `<user_information>`.
   - Call `open_token_dashboard` with `{"conversation_id": "<current-conversation-id>", "tab": "session"}` (or `get_token_behavior_report` with `{"conversation_id": "<current-conversation-id>", "scope": "both"}`) so the user gets both the visual dashboard and a clear diagnostic summary.
2. **For Global Cross-Session Analytics**:
   - Call `open_token_dashboard` with `{"tab": "global", "days": 7}`.
   - Highlight total tokens, prompt cache hit rate, top models/tools, and any high-consumption Swarm fleets.
3. **For High Context Pressure (>120k tokens) or Cache Drops**:
   - Inspect the session's `context_pressure` and `invocations` cache drop flags (`cache_drop_flag == 1`).
   - Recommend running `/compact` when context window occupancy approaches the warning threshold.
