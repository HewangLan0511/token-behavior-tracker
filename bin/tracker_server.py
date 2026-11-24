#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Token & AI Behavior Tracker — Pure-Python 3.12 MCP JSON-RPC 2.0 Server (stdio).

Implements:
- MCP Lifecycle: initialize, notifications/initialized, ping
- MCP Resources: resources/list, resources/templates/list, resources/read (ui://tracker/dashboard.html)
- MCP Tools:
  - open_token_dashboard (MCP App UI entrypoint for aux_pane, global sidebar, and inline)
  - get_token_behavior_report (Model-facing rich Markdown diagnostic report)
  - export_token_report (Visible to model & app; exports markdown, json, or csv)
  - app_get_overview (App-only RPC)
  - app_get_conversation (App-only RPC)
  - app_get_step_raw (App-only RPC for Context Browser drill-down)
  - app_sync_all (App-only RPC for incremental/forced brain scan)
  - app_update_settings (App-only RPC for threshold/pricebook updates)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# Ensure sibling modules in bin/ (such as engine.py) are importable
BIN_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = BIN_DIR.parent
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

APP_URI = "ui://tracker/dashboard.html"
SERVER_NAME = "token-behavior-tracker"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _load_engine(data_dir: str | Path | None = None):
    """Lazily import and instantiate TrackerEngine from bin/engine.py."""
    from engine import TrackerEngine  # type: ignore
    resolved_dir = (
        data_dir
        or os.environ.get("PLUGIN_DATA")
        or (Path.home() / ".gemini" / "antigravity" / "token-behavior-tracker" / "data")
    )
    try:
        return TrackerEngine(data_dir=resolved_dir)
    except PermissionError:
        fallback_dir = PLUGIN_ROOT / ".data"
        return TrackerEngine(data_dir=fallback_dir)


def _read_app_html() -> str:
    """Read ../ui/app.html dynamically on every resources/read request."""
    env_override = os.environ.get("TRACKER_APP_HTML")
    candidates = []
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(PLUGIN_ROOT / "ui" / "app.html")

    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Token & AI Tracker</title></head>"
        "<body style='font-family:system-ui;padding:24px;'>"
        "<h2>Token & AI Behavior Tracker</h2>"
        "<p>UI bundle (<code>ui/app.html</code>) is loading or not found.</p>"
        "</body></html>"
    )


def _fmt_num(val: Any) -> str:
    try:
        return f"{int(val or 0):,}"
    except Exception:
        return "0"


def _fmt_pct(ratio: Any) -> str:
    try:
        r = float(ratio or 0.0)
        if r <= 1.0 and r >= 0.0:
            # Handle both 0..1 ratio and 0..100 percentage gracefully
            # Note: engine stores cache_hit_ratio as 0.0..1.0
            return f"{r * 100:.1f}%"
        return f"{r:.1f}%"
    except Exception:
        return "0.0%"


def _fmt_usd(val: Any) -> str:
    try:
        return f"${float(val or 0.0):.4f}"
    except Exception:
        return "$0.0000"


def _build_markdown_report(
    overview: dict[str, Any] | None,
    detail: dict[str, Any] | None,
    scope: str = "both",
    days: int = 7,
) -> str:
    """Format a concise, high-signal Markdown report for the model."""
    lines: list[str] = []
    lines.append("# 📊 Token 消耗与 AI 行为分析报告")
    lines.append("")

    if scope in ("global", "both") and overview:
        kpis = overview.get("kpis") or {}
        total_tokens = kpis.get("total_tokens", 0)
        inp = kpis.get("total_input_tokens", 0)
        cache_read = kpis.get("total_cache_read_tokens", 0)
        out = kpis.get("total_output_tokens", 0)
        hit_ratio = kpis.get("avg_cache_hit_ratio", 0.0)
        conv_count = kpis.get("total_conversations", 0)
        root_count = kpis.get("root_conversations", conv_count)
        inv_count = kpis.get("total_invocations", 0)
        tool_calls = kpis.get("total_tool_calls", 0)
        tool_errors = kpis.get("total_tool_errors", 0)
        peak_ctx = kpis.get("peak_context_tokens", 0)
        cost_usd = kpis.get("estimated_cost_usd", 0.0)
        saved_usd = kpis.get("estimated_cache_saved_usd", 0.0)

        lines.append(f"## 1. 全局概览 (最近 {days} 天 / 全量索引)")
        lines.append(
            f"- **总 Token 消耗**: `{_fmt_num(total_tokens)}` "
            f"(新输入: `{_fmt_num(inp)}` | 缓存命中读取: `{_fmt_num(cache_read)}` | 模型输出: `{_fmt_num(out)}`)"
        )
        lines.append(
            f"- **平均 Prompt 缓存命中率**: `{_fmt_pct(hit_ratio)}` "
            f"(等效 API 估算成本: `{_fmt_usd(cost_usd)}`，缓存节省: `{_fmt_usd(saved_usd)}`)"
        )
        lines.append(
            f"- **会话与调用规模**: 共 `{conv_count}` 个会话 (主会话 `{root_count}` 个) | "
            f"模型推理轮次: `{_fmt_num(inv_count)}` | Peak 上下文水位: `{_fmt_num(peak_ctx)}` tokens"
        )
        lines.append(
            f"- **AI 工具调用统计**: 共 `{_fmt_num(tool_calls)}` 次调用 | 报错次数: `{_fmt_num(tool_errors)}`"
        )

        comp = overview.get("aggregate_composition") or {}
        if comp:
            lines.append(
                "- **全局上下文构成 (5 类归因)**: "
                f"System/Tools 基线 `{_fmt_num(comp.get('system_tokens'))}` | "
                f"用户输入 `{_fmt_num(comp.get('user_tokens'))}` | "
                f"工具返回 `{_fmt_num(comp.get('tool_tokens'))}` | "
                f"模型回复 `{_fmt_num(comp.get('output_tokens'))}` | "
                f"深度思考 (Thinking) `{_fmt_num(comp.get('thinking_tokens'))}`"
            )

        models = overview.get("model_breakdown") or []
        if models:
            lines.append("")
            lines.append("### 模型消耗分布")
            lines.append("| 模型 | 推理轮次 | 输入 Tokens | 缓存读取 Tokens | 输出 Tokens | 缓存命中率 | 估算成本 |")
            lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: |")
            for m in models[:8]:
                lines.append(
                    f"| `{m.get('model') or 'unknown'}` | {_fmt_num(m.get('invocations'))} | "
                    f"{_fmt_num(m.get('input_tokens'))} | {_fmt_num(m.get('cache_read_tokens'))} | "
                    f"{_fmt_num(m.get('output_tokens'))} | {_fmt_pct(m.get('cache_hit_ratio'))} | "
                    f"{_fmt_usd(m.get('cost_usd'))} |"
                )

        tools = overview.get("tool_breakdown") or []
        if tools:
            lines.append("")
            lines.append("### 高频 AI 工具行为 Top 8")
            lines.append("| 工具名称 | 调用次数 | 错误次数 | 平均耗时 (ms) | 参数体积 (B) | 结果体积 (B) |")
            lines.append("| :--- | ---: | ---: | ---: | ---: | ---: |")
            for t in tools[:8]:
                lines.append(
                    f"| `{t.get('tool_name') or 'unknown'}` | {_fmt_num(t.get('call_count'))} | "
                    f"{_fmt_num(t.get('error_count'))} | {_fmt_num(t.get('avg_duration_ms'))} | "
                    f"{_fmt_num(t.get('total_arg_bytes'))} | {_fmt_num(t.get('total_result_bytes'))} |"
                )
        lines.append("")

    if scope in ("session", "both") and detail and detail.get("conversation"):
        conv = detail["conversation"]
        cid = conv.get("conversation_id", "")
        title = conv.get("title") or cid
        role = conv.get("agent_role") or "main"
        model = conv.get("primary_model") or "unknown"
        c_inp = conv.get("total_input_tokens", 0)
        c_cache = conv.get("total_cache_read_tokens", 0)
        c_out = conv.get("total_output_tokens", 0)
        c_total = c_inp + c_cache + c_out
        c_prompt = c_inp + c_cache
        c_hit = (c_cache / c_prompt) if c_prompt > 0 else 0.0

        pressure = detail.get("context_pressure") or {}
        curr_ctx = pressure.get("current_context_tokens", conv.get("peak_context_tokens", 0))
        peak_ctx = pressure.get("peak_context_tokens", conv.get("peak_context_tokens", 0))
        warn_th = pressure.get("warn_threshold", 120000)
        win_lim = pressure.get("window_limit", 200000)
        occ_ratio = pressure.get("occupancy_ratio", (curr_ctx / win_lim if win_lim else 0.0))

        lines.append(f"## 2. 单对话深潜: `{cid}`")
        lines.append(f"- **标题**: {title}")
        lines.append(f"- **角色 / 主模型**: `{role}` | `{model}`")
        lines.append(
            f"- **本会话 Token 消耗**: `{_fmt_num(c_total)}` "
            f"(新输入: `{_fmt_num(c_inp)}` | 缓存读取: `{_fmt_num(c_cache)}` | 输出: `{_fmt_num(c_out)}`)"
        )
        lines.append(
            f"- **会话缓存命中率**: `{_fmt_pct(c_hit)}` | "
            f"**上下文水位**: 当前 `{_fmt_num(curr_ctx)}` / 峰值 `{_fmt_num(peak_ctx)}` "
            f"(预警线 `{_fmt_num(warn_th)}`, 窗口上限 `{_fmt_num(win_lim)}`, 占用率 `{_fmt_pct(occ_ratio)}`)"
        )

        s_comp = detail.get("composition") or {}
        if s_comp:
            lines.append(
                "- **会话上下文构成**: "
                f"System/Tools 基线 `{_fmt_num(s_comp.get('system_tokens'))}` | "
                f"用户输入 `{_fmt_num(s_comp.get('user_tokens'))}` | "
                f"工具返回 `{_fmt_num(s_comp.get('tool_tokens'))}` | "
                f"模型回复 `{_fmt_num(s_comp.get('output_tokens'))}` | "
                f"Thinking `{_fmt_num(s_comp.get('thinking_tokens'))}`"
            )

        fleet = detail.get("fleet") or {}
        fleet_members = fleet.get("members") or fleet.get("agents") or fleet.get("children") or []
        if fleet_members or fleet.get("fleet_total_tokens"):
            lines.append(
                f"- **多 Agent 舰队 (Swarm / Subagents)**: "
                f"舰队总 Token `{_fmt_num(fleet.get('fleet_total_tokens', c_total))}` | "
                f"成员数 `{len(fleet_members)}`"
            )

        file_hotspots = detail.get("file_hotspots") or []
        if file_hotspots:
            top_files = ", ".join(
                f"`{Path(f.get('path') or f.get('target_resource') or '').name}` ({f.get('count', 0)}次)"
                for f in file_hotspots[:5]
            )
            lines.append(f"- **高频访问文件**: {top_files}")

        cmd_hotspots = detail.get("command_hotspots") or []
        if cmd_hotspots:
            top_cmds = ", ".join(
                f"`{(c.get('command') or c.get('target_resource') or '')[:40]}` ({c.get('count', 0)}次)"
                for c in cmd_hotspots[:4]
            )
            lines.append(f"- **高频执行命令**: {top_cmds}")

    return "\n".join(lines)


def _fallback_export(
    engine: Any,
    conversation_id: str | None,
    fmt: str,
) -> str:
    """Fallback report exporter if engine.export_report is not called or returns empty."""
    fmt_lower = (fmt or "markdown").lower().strip()
    if hasattr(engine, "export_report"):
        try:
            res = engine.export_report(conversation_id=conversation_id, fmt=fmt_lower)
            if isinstance(res, str) and res.strip():
                return res
        except TypeError:
            try:
                res = engine.export_report(conversation_id, fmt_lower)
                if isinstance(res, str) and res.strip():
                    return res
            except Exception:
                pass
        except Exception:
            pass

    overview = engine.get_global_overview(days=30)
    detail = engine.get_conversation_detail(conversation_id) if conversation_id else None

    if fmt_lower == "json":
        return json.dumps(
            {
                "conversation_id": conversation_id,
                "overview": overview,
                "detail": detail,
            },
            ensure_ascii=False,
            indent=2,
        )
    if fmt_lower == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        if detail and detail.get("invocations"):
            writer.writerow(
                [
                    "conversation_id",
                    "step_index",
                    "created_at",
                    "model",
                    "input_tokens",
                    "cache_read_tokens",
                    "output_tokens",
                    "prompt_total_tokens",
                    "cache_hit_ratio",
                    "cache_drop_flag",
                    "tool_call_count",
                    "estimated_cost_usd",
                ]
            )
            for inv in detail.get("invocations", []):
                writer.writerow(
                    [
                        inv.get("conversation_id", conversation_id),
                        inv.get("step_index"),
                        inv.get("created_at"),
                        inv.get("model"),
                        inv.get("input_tokens"),
                        inv.get("cache_read_tokens"),
                        inv.get("output_tokens"),
                        inv.get("prompt_total_tokens"),
                        inv.get("cache_hit_ratio"),
                        inv.get("cache_drop_flag"),
                        inv.get("tool_call_count"),
                        inv.get("estimated_cost_usd"),
                    ]
                )
        else:
            writer.writerow(
                [
                    "conversation_id",
                    "title",
                    "agent_role",
                    "primary_model",
                    "total_input_tokens",
                    "total_cache_read_tokens",
                    "total_output_tokens",
                    "fleet_total_tokens",
                    "peak_context_tokens",
                    "total_tool_calls",
                    "updated_at",
                ]
            )
            for c in overview.get("conversations", []):
                writer.writerow(
                    [
                        c.get("conversation_id"),
                        c.get("title"),
                        c.get("agent_role"),
                        c.get("primary_model"),
                        c.get("total_input_tokens"),
                        c.get("total_cache_read_tokens"),
                        c.get("total_output_tokens"),
                        c.get("fleet_total_tokens"),
                        c.get("peak_context_tokens"),
                        c.get("total_tool_calls"),
                        c.get("updated_at"),
                    ]
                )
        return buf.getvalue()

    return _build_markdown_report(
        overview=overview,
        detail=detail or engine.get_conversation_detail(None),
        scope="session" if conversation_id else "both",
        days=30,
    )


def get_tools_manifest() -> list[dict[str, Any]]:
    """Return the MCP tools list with MCP App metadata and schemas."""
    return [
        {
            "name": "open_token_dashboard",
            "title": "Token & AI Tracker",
            "description": (
                "Open the interactive Token & AI Behavior Tracker dashboard (MCP App UI) "
                "to visualize global and per-conversation token usage, prompt cache hit rates, "
                "5-category context composition, Swarm fleet hierarchy, file/command hotspots, "
                "and step-by-step AI behavior timeline."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Optional conversation ID to focus on. Leave omitted for global overview or latest session.",
                    },
                    "tab": {
                        "type": "string",
                        "enum": ["global", "session", "fleet", "settings"],
                        "description": "Initial dashboard tab to display ('global', 'session', 'fleet', or 'settings').",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days for the global trend window (default: 7).",
                    },
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {
                    "resourceUri": APP_URI,
                    "visibility": ["model", "app"],
                },
                "com.google.antigravity/ui": {
                    "entrypoints": [
                        {"type": "aux_pane", "title": "Token & AI Tracker"},
                        {"type": "global", "title": "Token & AI Tracker"},
                    ]
                },
            },
        },
        {
            "name": "get_token_behavior_report",
            "title": "Get Token & AI Behavior Report",
            "description": (
                "Sync conversation transcripts and return a detailed Markdown diagnostic report "
                "of token usage, prompt cache hit rates, 5-category context window composition, "
                "Swarm fleet breakdown, and AI tool behaviors."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Optional conversation ID to analyze. Defaults to the most recent active conversation.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["session", "global", "both"],
                        "description": "Report scope: 'session', 'global', or 'both' (default: 'both').",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days to include in global trend analysis (default: 7).",
                    },
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {
                    "visibility": ["model", "app"],
                }
            },
        },
        {
            "name": "export_token_report",
            "title": "Export Token & Behavior Report",
            "description": (
                "Export token usage and AI behavior telemetry as Markdown, JSON, or CSV, "
                "and optionally save it to disk."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Optional conversation ID. Omit to export global report across all conversations.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "json", "csv"],
                        "description": "Export format: 'markdown', 'json', or 'csv' (default: 'markdown').",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Optional file path where the exported report should be saved.",
                    },
                },
                "additionalProperties": False,
            },
            "_meta": {
                "ui": {
                    "visibility": ["model", "app"],
                }
            },
        },
        {
            "name": "app_get_overview",
            "title": "App RPC: Get Global Overview",
            "description": "App-only RPC to sync transcripts and fetch global token & behavior overview.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer"},
                    "search": {"type": "string"},
                    "include_children": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
            "_meta": {
                "ui": {
                    "visibility": ["app"],
                }
            },
        },
        {
            "name": "app_get_conversation",
            "title": "App RPC: Get Conversation Deep-Dive",
            "description": "App-only RPC to fetch full per-conversation telemetry, timeline, hotspots, and Swarm fleet tree.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "_meta": {
                "ui": {
                    "visibility": ["app"],
                }
            },
        },
        {
            "name": "app_get_step_raw",
            "title": "App RPC: Get Step Raw Payload",
            "description": "App-only RPC to drill down into raw thinking, tool calls, and tool outputs for a specific step.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "step_index": {"type": "integer"},
                },
                "required": ["conversation_id", "step_index"],
                "additionalProperties": True,
            },
            "_meta": {
                "ui": {
                    "visibility": ["app"],
                }
            },
        },
        {
            "name": "app_sync_all",
            "title": "App RPC: Sync All Brains",
            "description": "App-only RPC to trigger incremental or full transcript scanning across all brain directories.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "force": {"type": "boolean"},
                    "days": {"type": "integer"},
                },
                "additionalProperties": True,
            },
            "_meta": {
                "ui": {
                    "visibility": ["app"],
                }
            },
        },
        {
            "name": "app_update_settings",
            "title": "App RPC: Update Tracker Settings",
            "description": "App-only RPC to update context warning thresholds, window limits, hook toggles, and model pricebook.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "settings": {"type": "object"},
                },
                "additionalProperties": True,
            },
            "_meta": {
                "ui": {
                    "visibility": ["app"],
                }
            },
        },
    ]


class TrackerMCPServer:
    """Stdio JSON-RPC 2.0 MCP Server for Token & AI Behavior Tracker."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.data_dir = data_dir
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            self._engine = _load_engine(self.data_dir)
        return self._engine

    def handle_tool_call(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = arguments or {}

        if name == "open_token_dashboard":
            conversation_id = args.get("conversation_id") or None
            tab = args.get("tab") or None
            days = int(args.get("days") or 7)

            self.engine.sync_all_brains(force=False)
            overview = self.engine.get_global_overview(days=days)
            detail = self.engine.get_conversation_detail(conversation_id)
            resolved_cid = (
                conversation_id
                or ((detail or {}).get("conversation") or {}).get("conversation_id")
            )
            view = tab or ("session" if conversation_id else "global")

            payload = {
                "view": view,
                "tab": view,
                "conversation_id": resolved_cid,
                "days": days,
                "overview": overview,
                "detail": detail,
                "settings": overview.get("settings") or self.engine.get_settings(),
            }
            md_summary = _build_markdown_report(
                overview=overview,
                detail=detail,
                scope="both",
                days=days,
            )
            return {
                "content": [{"type": "text", "text": md_summary}],
                "structuredContent": payload,
            }

        if name == "get_token_behavior_report":
            conversation_id = args.get("conversation_id") or None
            scope = args.get("scope") or ("session" if conversation_id else "both")
            days = int(args.get("days") or 7)

            self.engine.sync_all_brains(force=False)
            overview = self.engine.get_global_overview(days=days) if scope in ("global", "both") else None
            detail = self.engine.get_conversation_detail(conversation_id) if scope in ("session", "both") else None

            report_md = _build_markdown_report(
                overview=overview,
                detail=detail,
                scope=scope,
                days=days,
            )
            return {
                "content": [{"type": "text", "text": report_md}],
                "structuredContent": {
                    "scope": scope,
                    "days": days,
                    "conversation_id": conversation_id or ((detail or {}).get("conversation") or {}).get("conversation_id"),
                    "overview": overview,
                    "detail": detail,
                },
            }

        if name == "export_token_report":
            conversation_id = args.get("conversation_id") or None
            fmt = (args.get("format") or "markdown").lower().strip()
            output_path = args.get("output_path") or None

            self.engine.sync_all_brains(force=False)
            report_text = _fallback_export(self.engine, conversation_id=conversation_id, fmt=fmt)

            saved_path: str | None = None
            if output_path:
                out_file = Path(output_path).expanduser().resolve()
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(report_text, encoding="utf-8")
                saved_path = str(out_file)

            display_text = report_text
            if saved_path:
                display_text = f"Report exported to `{saved_path}` ({len(report_text)} chars).\n\n{report_text}"

            return {
                "content": [{"type": "text", "text": display_text}],
                "structuredContent": {
                    "format": fmt,
                    "conversation_id": conversation_id,
                    "output_path": saved_path,
                    "report": report_text,
                    "content": report_text,
                },
            }

        if name == "app_get_overview":
            days = int(args.get("days") or 7)
            search = str(args.get("search") or "")
            include_children = bool(args.get("include_children", False))

            self.engine.sync_all_brains(force=False)
            overview = self.engine.get_global_overview(
                days=days,
                search=search,
                include_children=include_children,
            )
            return {
                "content": [{"type": "text", "text": json.dumps(overview, ensure_ascii=False)}],
                "structuredContent": overview,
            }

        if name == "app_get_conversation":
            conversation_id = args.get("conversation_id") or None
            self.engine.sync_all_brains(force=False)
            detail = self.engine.get_conversation_detail(conversation_id)
            return {
                "content": [{"type": "text", "text": json.dumps(detail, ensure_ascii=False)}],
                "structuredContent": detail,
            }

        if name == "app_get_step_raw":
            conversation_id = str(args.get("conversation_id") or "")
            step_index = int(args.get("step_index") or 0)
            raw_payload = self.engine.get_step_raw_payload(conversation_id, step_index)
            return {
                "content": [{"type": "text", "text": json.dumps(raw_payload, ensure_ascii=False)}],
                "structuredContent": raw_payload,
            }

        if name == "app_sync_all":
            force = bool(args.get("force", False))
            days = int(args.get("days") or 7)
            sync_stats = self.engine.sync_all_brains(force=force)
            overview = self.engine.get_global_overview(days=days)
            if isinstance(overview, dict):
                overview["sync_stats"] = sync_stats
            return {
                "content": [{"type": "text", "text": json.dumps(overview, ensure_ascii=False)}],
                "structuredContent": overview,
            }

        if name == "app_update_settings":
            new_settings = args.get("settings")
            if not isinstance(new_settings, dict):
                new_settings = {k: v for k, v in args.items() if k != "settings"}
            updated = self.engine.update_settings(new_settings)
            result_payload = {"settings": updated, "ok": True}
            if isinstance(updated, dict):
                result_payload.update(updated)
            return {
                "content": [{"type": "text", "text": json.dumps(result_payload, ensure_ascii=False)}],
                "structuredContent": result_payload,
            }

        raise ValueError(f"Unknown tool: {name}")

    def handle_request(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        # Notifications have no "id" and must not receive a response
        if msg_id is None:
            return None

        try:
            if method == "initialize":
                client_proto = params.get("protocolVersion")
                negotiated_proto = (
                    client_proto
                    if client_proto in SUPPORTED_PROTOCOL_VERSIONS
                    else "2024-11-05"
                )
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": negotiated_proto,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"subscribe": False, "listChanged": False},
                        },
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": SERVER_VERSION,
                        },
                    },
                }

            if method == "ping":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {},
                }

            if method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": get_tools_manifest(),
                    },
                }

            if method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments") or {}
                try:
                    call_result = self.handle_tool_call(tool_name, tool_args)
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": call_result,
                    }
                except Exception as tool_exc:
                    err_text = f"Tool '{tool_name}' failed: {tool_exc}"
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "isError": True,
                            "content": [{"type": "text", "text": err_text}],
                        },
                    }

            if method == "resources/list":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "resources": [
                            {
                                "uri": APP_URI,
                                "name": "Token & AI Behavior Dashboard",
                                "description": (
                                    "Interactive MCP App dashboard for global and per-conversation "
                                    "token usage, prompt cache hit rate, context composition, "
                                    "Swarm fleet hierarchy, and AI behavior analytics."
                                ),
                                "mimeType": "text/html;profile=mcp-app",
                                "_meta": {
                                    "ui": {
                                        "prefersBorder": False,
                                    }
                                },
                            }
                        ]
                    },
                }

            if method == "resources/templates/list":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "resourceTemplates": [],
                    },
                }

            if method == "resources/read":
                uri = params.get("uri", "")
                if uri != APP_URI:
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {
                            "code": -32602,
                            "message": f"Unknown resource URI: {uri}",
                        },
                    }
                html_text = _read_app_html()
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "contents": [
                            {
                                "uri": APP_URI,
                                "mimeType": "text/html;profile=mcp-app",
                                "text": html_text,
                                "_meta": {
                                    "ui": {
                                        "prefersBorder": False,
                                    }
                                },
                            }
                        ]
                    },
                }

            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }

        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32603,
                    "message": f"Internal error: {exc}",
                },
            }


def run_stdio_loop(server: TrackerMCPServer) -> None:
    """Read JSON-RPC 2.0 messages from stdin (newline-delimited or Content-Length) and write responses to stdout."""
    # Ensure UTF-8 streams on Windows
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    while True:
        line = sys.stdin.readline()
        if not line:
            break

        stripped = line.strip()
        if not stripped:
            continue

        # Support optional Content-Length header framing if sent by a test client
        if stripped.lower().startswith("content-length:"):
            try:
                content_length = int(stripped.split(":", 1)[1].strip())
                # Consume blank header separator lines
                while True:
                    hdr_line = sys.stdin.readline()
                    if not hdr_line or not hdr_line.strip():
                        break
                body = sys.stdin.read(content_length)
                msg = json.loads(body)
            except Exception as exc:
                sys.stderr.write(f"[tracker_server] Failed to parse Content-Length frame: {exc}\n")
                continue
        else:
            try:
                msg = json.loads(stripped)
            except Exception as exc:
                sys.stderr.write(f"[tracker_server] Failed to parse JSON line: {exc}\n")
                continue

        if isinstance(msg, list):
            responses = []
            for item in msg:
                if isinstance(item, dict):
                    resp = server.handle_request(item)
                    if resp is not None:
                        responses.append(resp)
            if responses:
                sys.stdout.write(json.dumps(responses, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        elif isinstance(msg, dict):
            resp = server.handle_request(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Token & AI Behavior Tracker MCP Server")
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        default=os.environ.get("PLUGIN_DATA"),
        help="Path to persistent plugin data directory for SQLite tracker.db",
    )
    args, _ = parser.parse_known_args()

    server = TrackerMCPServer(data_dir=args.data_dir)
    run_stdio_loop(server)


if __name__ == "__main__":
    main()
