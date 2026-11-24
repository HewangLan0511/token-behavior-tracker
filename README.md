# Token & AI Behavior Tracker (`token-behavior-tracker`)

An Antigravity plugin (inspired by `dsh-context`) that records, analyzes, and visualizes **per-conversation** and **global** token consumption, prompt cache hit rates, 5-category context window composition, multi-agent Swarm fleet hierarchy, and AI tool behaviors.

---

## ✨ Key Features

1. **Global Insights Dashboard (`全局洞察`)**
   - Cross-session KPI cards: Total Tokens (`Input` / `Cache Read` / `Output`), Average Prompt Cache Hit Rate Ring, Active/Total Conversations (Root vs. Swarm/Subagent children), AI Tool Calls & Error Rate, Peak Context Pressure, and Equivalent API Cost & Cache Savings.
   - **7-Day Token & Cache Trend Chart** and **7×24 Activity Heatmap**.
   - **5-Category Aggregate Context Composition Ring** (`System & Tools Baseline`, `User Input`, `Tool Outputs`, `Model Replies`, `Thinking`).
   - **Parent-Child Grouped Conversation Cards**: Expand any root conversation to inspect attached Swarm workers and subagents inline, with both own tokens and **Fleet Total Tokens**.

2. **Session Deep-Dive (`单对话深潜`)**
   - **Context Pressure Ring (`上下文窗口水位`)**: Real-time context occupancy against configurable warning (`120k`) and window limit (`200k`) thresholds.
   - **Session Prompt Cache Hit Ring** & **Turn-by-Turn Evolution Chart**: Step chart across every `PLANNER_RESPONSE` turn with automatic **Prefix Cache Drop (`⚠️ 前缀缓存雪崩/失效`)** detection.
   - **File & Command Hotspots**: Top read/edited files (with one-click open in Antigravity's side pane via `com.google.antigravity/toggle-aux-pane`) and top executed shell commands.
   - **AI Behavior Timeline & Context Browser (`单步行为与原文钻取`)**: Click any step to inspect raw `thinking`, full `tool_calls` JSON arguments, tool execution outputs, latency (`duration_ms`), and wire token headers on demand.

3. **Multi-Agent Fleet View (`多 Agent 舰队`)**
   - Visualizes Coordinator / Parent → Swarm Workers & Subagents hierarchy, token share bars, per-agent models, tool call counts, and Swarm `plan.json` metadata.

4. **Zero-Latency Lifecycle Hooks & Passive Context Warning**
   - Uses `SessionStart`, `PostInvocation`, `PostToolUse`, and `Stop` hooks (`hooks.json`) to fold `transcript.jsonl` incrementally using byte-offset watermarks (`<15ms`), without registering `PreToolUse` (zero tool-call latency overhead).
   - Emits a non-intrusive inline user markdown reminder suggesting `/compact` when a conversation crosses the configurable context warning threshold.

---

## 📁 Directory Structure

```text
token-behavior-tracker/
├── plugin.json                  # Plugin manifest & starter prompts
├── mcp_config.json              # Stdio MCP server configuration (token-behavior-tracker_tracker)
├── hooks.json                   # Lifecycle hooks (SessionStart, PostInvocation, PostToolUse, Stop)
├── assets/
│   └── logo.svg                 # High-contrast 256x256 SVG plugin icon
├── bin/
│   ├── engine.py                # SQLite WAL storage & incremental transcript.jsonl folding engine
│   ├── hook_collector.py        # Hook stdin/stdout handler & passive context warning injector
│   └── tracker_server.py        # Pure-Python 3.12 JSON-RPC 2.0 MCP Server + MCP App resource host
├── ui/
│   └── app.html                 # Self-contained MCP App interactive dashboard (4 tabs, dark/light theme)
├── skills/
│   └── token-tracker/
│       └── SKILL.md             # Agent instructions for token & behavior queries
└── deploy.py                    # One-click installer to ~/.gemini/config/plugins/token-behavior-tracker
```

---

## 🚀 Installation & Usage

Run the deployment script to install the plugin globally and pre-populate the SQLite database from existing `brain/` transcripts:

```bash
python d:\antigravity_workplace\token-behavior-tracker\deploy.py
```

Once enabled, you can:
- Open **Token & AI Tracker** from the left sidebar (`Global` entrypoint) or the conversation side pane `+` menu (`Aux Pane` entrypoint).
- Ask the agent in chat:
  - *"打开全局 Token 与 AI 行为洞察面板"*
  - *"分析当前对话的上下文构成、Prompt 缓存命中率以及多 Agent 舰队消耗"*
  - *"导出最近 7 天的 Token 消耗与 AI 工具调用行为分析报告"*
