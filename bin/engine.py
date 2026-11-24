#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Core SQLite WAL storage engine and incremental transcript folding engine
for the Antigravity Token & AI Behavior Tracker plugin (inspired by dsh-context).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

DEFAULT_PRICEBOOK = {
    "default": {
        "input_per_1m": 1.25,
        "cache_read_per_1m": 0.125,
        "output_per_1m": 5.0,
    },
    "gemini-4-argon": {
        "input_per_1m": 1.25,
        "cache_read_per_1m": 0.125,
        "output_per_1m": 5.0,
    },
    "gemini-4-hydrogen": {
        "input_per_1m": 1.25,
        "cache_read_per_1m": 0.125,
        "output_per_1m": 5.0,
    },
    "gemini-2.5-pro": {
        "input_per_1m": 1.25,
        "cache_read_per_1m": 0.125,
        "output_per_1m": 10.0,
    },
    "gemini-2.5-flash": {
        "input_per_1m": 0.30,
        "cache_read_per_1m": 0.03,
        "output_per_1m": 2.50,
    },
    "claude": {
        "input_per_1m": 3.00,
        "cache_read_per_1m": 0.30,
        "output_per_1m": 15.0,
    },
    "deepseek": {
        "input_per_1m": 0.27,
        "cache_read_per_1m": 0.07,
        "output_per_1m": 1.10,
    },
}

DEFAULT_SETTINGS = {
    "context_warn_threshold": 120000,
    "context_window_limit": 200000,
    "enable_context_warn_hook": True,
    "show_cost_estimates": True,
    "pricebook": DEFAULT_PRICEBOOK,
}

USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
SWARM_ROLE_RE = re.compile(r"recorded on the board as #\d+ for @([a-zA-Z0-9_-]+)")
SENDER_UUID_RE = re.compile(r"sender=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
CREATED_AT_RE = re.compile(r"Created At:\s*([0-9T:\.\-\+Z]+)")
COMPLETED_AT_RE = re.compile(r"Completed At:\s*([0-9T:\.\-\+Z]+)")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _unquote_arg(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    s = str(val).strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        try:
            parsed = json.loads(s)
            if isinstance(parsed, str):
                return parsed
        except Exception:
            return s[1:-1]
    return s


def _parse_iso_dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    ts = ts.strip()
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _estimate_tokens_from_chars(char_count: int) -> int:
    if char_count <= 0:
        return 0
    return max(1, int(round(char_count / 3.8)))


def _path_to_file_uri(path_str: str) -> str:
    if not path_str:
        return ""
    clean = _unquote_arg(path_str).replace("\\", "/")
    if clean.startswith("file://"):
        return clean
    if re.match(r"^[a-zA-Z]:/", clean):
        return f"file:///{clean}"
    if clean.startswith("/"):
        return f"file://{clean}"
    return f"file:///{clean}"


class TrackerEngine:
    """SQLite WAL storage engine and incremental transcript folder."""

    def __init__(self, data_dir: str | Path | None = None):
        if data_dir:
            self.data_dir = Path(data_dir).expanduser().resolve()
        else:
            env_dir = os.environ.get("PLUGIN_DATA")
            if env_dir and not env_dir.startswith("${"):
                self.data_dir = Path(env_dir).expanduser().resolve()
            else:
                self.data_dir = (
                    Path.home()
                    / ".gemini"
                    / "antigravity"
                    / "token-behavior-tracker"
                    / "data"
                ).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "tracker.db"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=OFF;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    parent_conversation_id TEXT DEFAULT '',
                    agent_role TEXT DEFAULT 'main',
                    workspace_path TEXT DEFAULT '',
                    primary_model TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT '',
                    total_invocations INT DEFAULT 0,
                    total_steps INT DEFAULT 0,
                    total_tool_calls INT DEFAULT 0,
                    total_tool_errors INT DEFAULT 0,
                    user_turns INT DEFAULT 0,
                    total_input_tokens INT DEFAULT 0,
                    total_cache_read_tokens INT DEFAULT 0,
                    total_output_tokens INT DEFAULT 0,
                    peak_context_tokens INT DEFAULT 0,
                    baseline_system_tokens INT DEFAULT 0,
                    comp_system_tokens INT DEFAULT 0,
                    comp_user_tokens INT DEFAULT 0,
                    comp_tool_tokens INT DEFAULT 0,
                    comp_output_tokens INT DEFAULT 0,
                    comp_thinking_tokens INT DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0.0,
                    estimated_cache_saved_usd REAL DEFAULT 0.0,
                    transcript_path TEXT DEFAULT '',
                    last_scanned_offset INT DEFAULT 0,
                    last_scanned_mtime REAL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS invocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    step_index INT NOT NULL,
                    created_at TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    input_tokens INT DEFAULT 0,
                    cache_read_tokens INT DEFAULT 0,
                    output_tokens INT DEFAULT 0,
                    prompt_total_tokens INT DEFAULT 0,
                    cache_hit_ratio REAL DEFAULT 0.0,
                    cache_drop_flag INT DEFAULT 0,
                    thinking_chars INT DEFAULT 0,
                    response_chars INT DEFAULT 0,
                    tool_call_count INT DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0.0,
                    byte_offset INT DEFAULT 0,
                    byte_length INT DEFAULT 0,
                    UNIQUE(conversation_id, step_index)
                );

                CREATE TABLE IF NOT EXISTS behavior_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    step_index INT NOT NULL,
                    event_type TEXT NOT NULL,
                    tool_name TEXT DEFAULT '',
                    target_resource TEXT DEFAULT '',
                    arg_bytes INT DEFAULT 0,
                    result_bytes INT DEFAULT 0,
                    duration_ms INT DEFAULT 0,
                    is_error INT DEFAULT 0,
                    error_message TEXT DEFAULT '',
                    thinking_preview TEXT DEFAULT '',
                    byte_offset INT DEFAULT 0,
                    byte_length INT DEFAULT 0,
                    created_at TEXT DEFAULT '',
                    UNIQUE(conversation_id, step_index, event_type, tool_name, target_resource)
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_inv_conv ON invocations(conversation_id, step_index);
                CREATE INDEX IF NOT EXISTS idx_inv_created ON invocations(created_at);
                CREATE INDEX IF NOT EXISTS idx_beh_conv ON behavior_events(conversation_id, step_index);
                CREATE INDEX IF NOT EXISTS idx_beh_tool ON behavior_events(event_type, tool_name);
                CREATE INDEX IF NOT EXISTS idx_conv_parent ON conversations(parent_conversation_id);
                CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
                """
            )
            for k, v in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, json.dumps(v, ensure_ascii=False)),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Settings & Pricing Helpers
    # ------------------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        result = dict(DEFAULT_SETTINGS)
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
                for row in rows:
                    try:
                        result[row["key"]] = json.loads(row["value"])
                    except Exception:
                        result[row["key"]] = row["value"]
        except Exception:
            pass
        if not isinstance(result.get("pricebook"), dict):
            result["pricebook"] = dict(DEFAULT_PRICEBOOK)
        else:
            merged_pb = dict(DEFAULT_PRICEBOOK)
            merged_pb.update(result["pricebook"])
            result["pricebook"] = merged_pb
        return result

    def update_settings(self, new_settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(new_settings, dict):
            return self.get_settings()
        current = self.get_settings()
        with self._connect() as conn:
            for k, v in new_settings.items():
                if k == "pricebook" and isinstance(v, dict):
                    pb = dict(current.get("pricebook", DEFAULT_PRICEBOOK))
                    pb.update(v)
                    v = pb
                elif k in ("context_warn_threshold", "context_window_limit"):
                    try:
                        v = int(v)
                    except Exception:
                        continue
                elif k in ("enable_context_warn_hook", "show_cost_estimates"):
                    v = bool(v)
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (k, json.dumps(v, ensure_ascii=False)),
                )
            conn.commit()
        return self.get_settings()

    def _get_rates_for_model(
        self, model: str, pricebook: dict[str, Any]
    ) -> dict[str, float]:
        default_rate = pricebook.get(
            "default",
            {"input_per_1m": 1.25, "cache_read_per_1m": 0.125, "output_per_1m": 5.0},
        )
        if not model:
            return default_rate
        m_low = model.lower()
        if m_low in pricebook and isinstance(pricebook[m_low], dict):
            return pricebook[m_low]
        for prefix, rates in pricebook.items():
            if prefix != "default" and m_low.startswith(prefix.lower()) and isinstance(rates, dict):
                return rates
        for prefix, rates in pricebook.items():
            if prefix != "default" and prefix.lower() in m_low and isinstance(rates, dict):
                return rates
        return default_rate

    def _compute_cost(
        self,
        model: str,
        input_tokens: int,
        cache_read_tokens: int,
        output_tokens: int,
        pricebook: dict[str, Any],
    ) -> tuple[float, float]:
        rates = self._get_rates_for_model(model, pricebook)
        in_rate = float(rates.get("input_per_1m", 1.25))
        cache_rate = float(rates.get("cache_read_per_1m", 0.125))
        out_rate = float(rates.get("output_per_1m", 5.0))
        cost = (
            (input_tokens / 1_000_000.0) * in_rate
            + (cache_read_tokens / 1_000_000.0) * cache_rate
            + (output_tokens / 1_000_000.0) * out_rate
        )
        saved = (cache_read_tokens / 1_000_000.0) * max(0.0, in_rate - cache_rate)
        return round(cost, 6), round(saved, 6)

    # ------------------------------------------------------------------
    # Tool Call Target Resource Extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_target_resource(tool_name: str, args: dict[str, Any]) -> str:
        if not isinstance(args, dict):
            return ""
        for key in ("Path", "path", " TargetFile", "file_path", "ScriptPath"):
            k = key.strip()
            if k in args and args[k]:
                return _unquote_arg(args[k])[:260]
        if "CommandLine" in args and args["CommandLine"]:
            return _unquote_arg(args["CommandLine"])[:260]
        if "command" in args and args["command"]:
            return _unquote_arg(args["command"])[:260]
        if "query" in args and args["query"]:
            return _unquote_arg(args["query"])[:260]
        if "queries" in args and args["queries"]:
            return _unquote_arg(args["queries"])[:260]
        if tool_name == "swarm":
            act = _unquote_arg(args.get("action", ""))
            nm = _unquote_arg(args.get("name", "")) or _unquote_arg(args.get("title", ""))
            return f"{act}:{nm}".strip(":")[:260]
        if tool_name in ("invoke_subagent", "define_subagent", "send_message"):
            for k in ("name", "subagent_name", "agent_name", "conversation_id", "recipient"):
                if k in args and args[k]:
                    return _unquote_arg(args[k])[:260]
        if tool_name == "update_focus" and "focus" in args:
            return _unquote_arg(args["focus"])[:200]
        if tool_name == "read_resource" and "Uri" in args:
            return _unquote_arg(args["Uri"])[:260]
        for k, v in args.items():
            if v is not None and k not in ("WaitMsBeforeAsync", "BypassSandbox"):
                s = _unquote_arg(v)
                if s:
                    return f"{k}={s}"[:200]
        return ""

    @staticmethod
    def _parse_generic_duration_and_error(content: str) -> tuple[int, int, str]:
        if not content:
            return 0, 0, ""
        duration_ms = 0
        c_matches = CREATED_AT_RE.findall(content)
        d_matches = COMPLETED_AT_RE.findall(content)
        if c_matches and d_matches:
            dt1 = _parse_iso_dt(c_matches[0])
            dt2 = _parse_iso_dt(d_matches[-1])
            if dt1 and dt2:
                diff = (dt2 - dt1).total_seconds() * 1000.0
                if 0 <= diff < 86_400_000:
                    duration_ms = int(round(diff))

        is_error = 0
        error_msg = ""
        low = content.lower()
        if "command exited with code " in low and "command exited with code 0" not in low:
            is_error = 1
            m = re.search(r"(Command exited with code \d+[^\n]*)", content)
            error_msg = m.group(1).strip() if m else "Non-zero exit code"
        elif "unauthorizedaccessexception" in low or "access is denied" in low:
            is_error = 1
            error_msg = "Access is denied / PermissionError"
        elif content.startswith("Error:") or "\nError:" in content:
            is_error = 1
            for line in content.splitlines():
                if line.strip().startswith("Error:"):
                    error_msg = line.strip()[:240]
                    break
        return duration_ms, is_error, error_msg

    # ------------------------------------------------------------------
    # Transcript Folding & Synchronization
    # ------------------------------------------------------------------

    def sync_transcript(
        self,
        transcript_path: str | Path,
        conversation_id: str | None = None,
        workspace_paths: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        t_path = Path(transcript_path).expanduser().resolve()
        try:
            if not t_path.exists() or not t_path.is_file():
                return {"status": "missing", "transcript_path": str(t_path)}
        except PermissionError:
            return {"status": "permission_denied", "transcript_path": str(t_path)}

        if not conversation_id:
            # Expect <appData>/brain/<conv-id>/.system_generated/logs/transcript.jsonl
            parts = t_path.parts
            for i, p in enumerate(parts):
                if p.lower() == "brain" and i + 1 < len(parts):
                    conversation_id = parts[i + 1]
                    break
            if not conversation_id:
                conversation_id = t_path.parent.name

        stat = t_path.stat()
        file_size = stat.st_size
        file_mtime = stat.st_mtime

        settings = self.get_settings()
        pricebook = settings.get("pricebook", DEFAULT_PRICEBOOK)

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()

            if (
                existing
                and not force
                and existing["last_scanned_offset"] == file_size
                and abs(float(existing["last_scanned_mtime"] or 0.0) - file_mtime) < 0.001
            ):
                return {
                    "status": "up_to_date",
                    "conversation_id": conversation_id,
                    "peak_context_tokens": existing["peak_context_tokens"],
                    "latest_context_tokens": existing["peak_context_tokens"],
                }

            # Decide incremental vs full scan
            start_offset = 0
            is_incremental = False
            if (
                existing
                and not force
                and 0 < existing["last_scanned_offset"] <= file_size
            ):
                start_offset = int(existing["last_scanned_offset"])
                is_incremental = True

            if not is_incremental and existing:
                conn.execute(
                    "DELETE FROM invocations WHERE conversation_id = ?",
                    (conversation_id,),
                )
                conn.execute(
                    "DELETE FROM behavior_events WHERE conversation_id = ? AND event_type != 'HOOK_EVENT'",
                    (conversation_id,),
                )

            # Check if transcript_full.jsonl exists for exact byte sizes of truncated steps
            full_path = t_path.parent / "transcript_full.jsonl"
            try:
                has_full = full_path.exists()
            except PermissionError:
                has_full = False

            # Get previous invocation prompt_total_tokens for cache_drop_flag
            prev_prompt_tokens = 0
            if is_incremental:
                last_inv = conn.execute(
                    "SELECT prompt_total_tokens FROM invocations WHERE conversation_id = ? ORDER BY step_index DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if last_inv:
                    prev_prompt_tokens = int(last_inv["prompt_total_tokens"] or 0)

            title = existing["title"] if (existing and is_incremental) else ""
            parent_cid = (
                existing["parent_conversation_id"]
                if (existing and existing["parent_conversation_id"])
                else ""
            )
            agent_role = (
                existing["agent_role"]
                if (existing and existing["agent_role"] and existing["agent_role"] != "main")
                else "main"
            )
            workspace_path = (
                existing["workspace_path"]
                if (existing and existing["workspace_path"])
                else (workspace_paths[0] if workspace_paths else "")
            )
            created_at = existing["created_at"] if (existing and is_incremental) else ""
            updated_at = existing["updated_at"] if (existing and is_incremental) else ""

            pending_tools: list[dict[str, Any]] = []
            discovered_children: list[tuple[str, str]] = []  # (child_conv_id, role_name)
            first_user_chars = 0

            with open(t_path, "rb") as f:
                f.seek(start_offset)
                current_offset = start_offset
                while True:
                    raw_line = f.readline()
                    if not raw_line:
                        break
                    line_len = len(raw_line)
                    line_offset = current_offset
                    current_offset += line_len

                    line_str = raw_line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        step = json.loads(line_str)
                    except Exception:
                        continue

                    step_idx = int(step.get("step_index", 0))
                    step_type = str(step.get("type", ""))
                    step_created = str(step.get("created_at", ""))
                    content = str(step.get("content", "") or "")

                    if step_created:
                        if not created_at or step_created < created_at:
                            created_at = step_created
                        if not updated_at or step_created > updated_at:
                            updated_at = step_created

                    # Try to infer workspace_path from file paths if not set
                    if not workspace_path and "d:\\antigravity_workplace" in line_str.lower():
                        workspace_path = "d:\\antigravity_workplace"

                    # Detect Swarm worker role & parent from initial SYSTEM_MESSAGE
                    if step_type == "SYSTEM_MESSAGE" and step_idx <= 2:
                        m_role = SWARM_ROLE_RE.search(content)
                        if m_role:
                            agent_role = m_role.group(1)
                        m_sender = SENDER_UUID_RE.search(content)
                        if m_sender and m_sender.group(1) != conversation_id:
                            if not parent_cid and ("Your assignment (recorded on the board" in content or "[swarm]" in content):
                                parent_cid = m_sender.group(1)
                        if not title and "Your assignment" in content:
                            for ln in content.splitlines():
                                ln_s = ln.strip()
                                if ln_s.startswith("You own ") or ln_s.startswith("You are ") or ln_s.startswith("Research "):
                                    title = f"[{agent_role}] {ln_s}"[:100]
                                    break

                    elif step_type == "USER_INPUT":
                        m_req = USER_REQ_RE.search(content)
                        clean_user = m_req.group(1).strip() if m_req else content.strip()
                        if first_user_chars == 0:
                            first_user_chars = len(clean_user)
                        if not title and clean_user:
                            first_line = " ".join(clean_user.splitlines()).strip()
                            title = first_line[:100]

                        conn.execute(
                            """
                            INSERT OR REPLACE INTO behavior_events (
                                conversation_id, step_index, event_type, tool_name, target_resource,
                                arg_bytes, result_bytes, duration_ms, is_error, error_message,
                                thinking_preview, byte_offset, byte_length, created_at
                            ) VALUES (?, ?, 'USER_INPUT', 'user_prompt', ?, ?, 0, 0, 0, '', ?, ?, ?, ?)
                            """,
                            (
                                conversation_id,
                                step_idx,
                                clean_user[:120],
                                len(clean_user.encode("utf-8", errors="replace")),
                                clean_user[:200],
                                line_offset,
                                line_len,
                                step_created,
                            ),
                        )

                    elif step_type == "PLANNER_RESPONSE":
                        model = str(step.get("model", "") or "unknown")
                        in_tok = int(step.get("input_tokens", 0) or 0)
                        cache_tok = int(step.get("cache_read_tokens", 0) or 0)
                        out_tok = int(step.get("output_tokens", 0) or 0)
                        prompt_tot = in_tok + cache_tok
                        hit_ratio = (
                            round(cache_tok / prompt_tot, 4) if prompt_tot > 0 else 0.0
                        )
                        drop_flag = (
                            1
                            if (
                                prev_prompt_tokens > 4000
                                and cache_tok < 0.5 * prev_prompt_tokens
                            )
                            else 0
                        )
                        prev_prompt_tokens = prompt_tot

                        thinking_str = str(step.get("thinking", "") or "")
                        tool_calls = step.get("tool_calls") or []
                        if not isinstance(tool_calls, list):
                            tool_calls = []

                        cost_usd, _ = self._compute_cost(
                            model, in_tok, cache_tok, out_tok, pricebook
                        )

                        conn.execute(
                            """
                            INSERT OR REPLACE INTO invocations (
                                conversation_id, step_index, created_at, model,
                                input_tokens, cache_read_tokens, output_tokens, prompt_total_tokens,
                                cache_hit_ratio, cache_drop_flag, thinking_chars, response_chars,
                                tool_call_count, estimated_cost_usd, byte_offset, byte_length
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                conversation_id,
                                step_idx,
                                step_created,
                                model,
                                in_tok,
                                cache_tok,
                                out_tok,
                                prompt_tot,
                                hit_ratio,
                                drop_flag,
                                len(thinking_str),
                                len(content),
                                len(tool_calls),
                                cost_usd,
                                line_offset,
                                line_len,
                            ),
                        )

                        pending_tools = []
                        t_preview = " ".join(thinking_str.strip().split())[:200]
                        for idx_tc, tc in enumerate(tool_calls):
                            if not isinstance(tc, dict):
                                continue
                            t_name = str(tc.get("name", "unknown"))
                            t_args = tc.get("args") or {}
                            if not isinstance(t_args, dict):
                                t_args = {}
                            target_res = self._extract_target_resource(t_name, t_args)
                            # Disambiguate multiple identical tool calls in the same step
                            if any(
                                pt["tool_name"] == t_name and pt["target_resource"] == target_res
                                for pt in pending_tools
                            ):
                                target_res = f"{target_res} #{idx_tc + 1}".strip()

                            arg_bytes = len(
                                json.dumps(t_args, ensure_ascii=False).encode(
                                    "utf-8", errors="replace"
                                )
                            )
                            if t_name == "swarm" and _unquote_arg(t_args.get("action")) in (
                                "add",
                                "title",
                                "kill",
                            ):
                                if agent_role == "main":
                                    agent_role = "coordinator"

                            conn.execute(
                                """
                                INSERT OR REPLACE INTO behavior_events (
                                    conversation_id, step_index, event_type, tool_name, target_resource,
                                    arg_bytes, result_bytes, duration_ms, is_error, error_message,
                                    thinking_preview, byte_offset, byte_length, created_at
                                ) VALUES (?, ?, 'TOOL_CALL', ?, ?, ?, 0, 0, 0, '', ?, ?, ?, ?)
                                """,
                                (
                                    conversation_id,
                                    step_idx,
                                    t_name,
                                    target_res,
                                    arg_bytes,
                                    t_preview,
                                    line_offset,
                                    line_len,
                                    step_created,
                                ),
                            )
                            pending_tools.append(
                                {
                                    "step_index": step_idx,
                                    "tool_name": t_name,
                                    "target_resource": target_res,
                                    "args": t_args,
                                }
                            )

                    elif step_type == "GENERIC":
                        result_bytes = len(content.encode("utf-8", errors="replace"))
                        if step.get("truncated_fields") and line_len > result_bytes:
                            result_bytes = line_len
                        dur_ms, is_err, err_msg = self._parse_generic_duration_and_error(
                            content
                        )

                        if pending_tools:
                            pt = pending_tools.pop(0)
                            conn.execute(
                                """
                                UPDATE behavior_events
                                SET result_bytes = ?, duration_ms = ?, is_error = ?, error_message = ?
                                WHERE conversation_id = ? AND step_index = ? AND event_type = 'TOOL_CALL'
                                  AND tool_name = ? AND target_resource = ?
                                """,
                                (
                                    result_bytes,
                                    dur_ms,
                                    is_err,
                                    err_msg,
                                    conversation_id,
                                    pt["step_index"],
                                    pt["tool_name"],
                                    pt["target_resource"],
                                ),
                            )
                            # Check for child conversation creation in swarm / invoke_subagent result
                            if pt["tool_name"] in ("swarm", "invoke_subagent"):
                                for m_json in re.finditer(r"\{[^{}]*\"conversation_id\"\s*:\s*\"([0-9a-fA-F\-]{36})\"[^{}]*\}", content):
                                    try:
                                        obj = json.loads(m_json.group(0))
                                        c_id = obj.get("conversation_id")
                                        c_name = obj.get("name") or _unquote_arg(
                                            pt["args"].get("name", "subagent")
                                        )
                                        if c_id and c_id != conversation_id:
                                            discovered_children.append((c_id, str(c_name)))
                                    except Exception:
                                        c_id = m_json.group(1)
                                        c_name = _unquote_arg(
                                            pt["args"].get("name", "worker")
                                        )
                                        if c_id and c_id != conversation_id:
                                            discovered_children.append((c_id, str(c_name)))

                    elif step_type == "ERROR_MESSAGE":
                        err_text = str(step.get("error", "") or content)[:240]
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO behavior_events (
                                conversation_id, step_index, event_type, tool_name, target_resource,
                                arg_bytes, result_bytes, duration_ms, is_error, error_message,
                                thinking_preview, byte_offset, byte_length, created_at
                            ) VALUES (?, ?, 'STOP', 'stream_error', 'ERROR_MESSAGE', 0, ?, 0, 1, ?, ?, ?, ?, ?)
                            """,
                            (
                                conversation_id,
                                step_idx,
                                len(content),
                                err_text,
                                err_text[:200],
                                line_offset,
                                line_len,
                                step_created,
                            ),
                        )

            # Recompute conversation aggregates from SQLite tables
            inv_agg = conn.execute(
                """
                SELECT
                    COUNT(*) AS inv_count,
                    COALESCE(MAX(step_index), 0) AS max_step,
                    COALESCE(SUM(input_tokens), 0) AS sum_in,
                    COALESCE(SUM(cache_read_tokens), 0) AS sum_cache,
                    COALESCE(SUM(output_tokens), 0) AS sum_out,
                    COALESCE(MAX(prompt_total_tokens), 0) AS peak_ctx,
                    COALESCE(SUM(thinking_chars), 0) AS sum_think_chars,
                    COALESCE(SUM(response_chars), 0) AS sum_resp_chars,
                    COALESCE(SUM(estimated_cost_usd), 0.0) AS sum_cost
                FROM invocations
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()

            first_inv = conn.execute(
                "SELECT prompt_total_tokens, model FROM invocations WHERE conversation_id = ? ORDER BY step_index ASC LIMIT 1",
                (conversation_id,),
            ).fetchone()

            top_model_row = conn.execute(
                "SELECT model, COUNT(*) AS cnt FROM invocations WHERE conversation_id = ? AND model != '' GROUP BY model ORDER BY cnt DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            primary_model = (
                top_model_row["model"]
                if top_model_row
                else (first_inv["model"] if first_inv else "")
            )

            beh_agg = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN event_type = 'TOOL_CALL' THEN 1 ELSE 0 END), 0) AS tool_calls,
                    COALESCE(SUM(CASE WHEN event_type = 'TOOL_CALL' AND is_error = 1 THEN 1 ELSE 0 END), 0) AS tool_errors,
                    COALESCE(SUM(CASE WHEN event_type = 'USER_INPUT' THEN 1 ELSE 0 END), 0) AS user_turns,
                    COALESCE(SUM(CASE WHEN event_type = 'USER_INPUT' THEN arg_bytes ELSE 0 END), 0) AS user_bytes,
                    COALESCE(SUM(CASE WHEN event_type = 'TOOL_CALL' THEN arg_bytes + result_bytes ELSE 0 END), 0) AS tool_bytes,
                    COALESCE(MAX(step_index), 0) AS max_beh_step
                FROM behavior_events
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()

            total_invocations = int(inv_agg["inv_count"] or 0)
            total_steps = max(int(inv_agg["max_step"] or 0), int(beh_agg["max_beh_step"] or 0)) + (
                1 if total_invocations > 0 else 0
            )
            total_tool_calls = int(beh_agg["tool_calls"] or 0)
            total_tool_errors = int(beh_agg["tool_errors"] or 0)
            user_turns = int(beh_agg["user_turns"] or 0)

            total_in = int(inv_agg["sum_in"] or 0)
            total_cache = int(inv_agg["sum_cache"] or 0)
            total_out = int(inv_agg["sum_out"] or 0)
            peak_ctx = int(inv_agg["peak_ctx"] or 0)

            # Baseline system tokens from Turn 0
            turn0_prompt = int(first_inv["prompt_total_tokens"] or 0) if first_inv else 0
            est_first_user = _estimate_tokens_from_chars(first_user_chars)
            baseline_sys = max(0, turn0_prompt - est_first_user) if turn0_prompt > 0 else 0
            if baseline_sys == 0 and turn0_prompt > 0:
                baseline_sys = int(turn0_prompt * 0.85)

            # 5-category context composition attribution
            think_chars = int(inv_agg["sum_think_chars"] or 0)
            resp_chars = int(inv_agg["sum_resp_chars"] or 0)
            est_think_tok = min(
                total_out,
                _estimate_tokens_from_chars(think_chars),
            )
            comp_thinking_tokens = est_think_tok
            comp_output_tokens = max(0, total_out - comp_thinking_tokens)

            comp_system_tokens = baseline_sys
            comp_user_tokens = max(
                _estimate_tokens_from_chars(int(beh_agg["user_bytes"] or 0)),
                user_turns * 45,
            )
            comp_tool_tokens = max(
                _estimate_tokens_from_chars(int(beh_agg["tool_bytes"] or 0)),
                max(0, peak_ctx - comp_system_tokens - comp_user_tokens),
            )

            _, est_cache_saved_usd = self._compute_cost(
                primary_model, total_in, total_cache, total_out, pricebook
            )
            est_cost_usd = round(float(inv_agg["sum_cost"] or 0.0), 6)

            if not title:
                title = f"Session {conversation_id[:8]}"
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if not created_at:
                created_at = datetime.fromtimestamp(
                    file_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            if not updated_at:
                updated_at = now_iso

            # Check if swarm plan.json exists for this conversation
            plan_path = t_path.parent.parent / "swarm" / "plan.json"
            try:
                has_plan = plan_path.exists()
            except PermissionError:
                has_plan = False
            if has_plan and agent_role == "main":
                agent_role = "coordinator"

            conn.execute(
                """
                INSERT OR REPLACE INTO conversations (
                    conversation_id, title, parent_conversation_id, agent_role,
                    workspace_path, primary_model, created_at, updated_at,
                    total_invocations, total_steps, total_tool_calls, total_tool_errors, user_turns,
                    total_input_tokens, total_cache_read_tokens, total_output_tokens, peak_context_tokens,
                    baseline_system_tokens, comp_system_tokens, comp_user_tokens, comp_tool_tokens,
                    comp_output_tokens, comp_thinking_tokens, estimated_cost_usd, estimated_cache_saved_usd,
                    transcript_path, last_scanned_offset, last_scanned_mtime
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    title,
                    parent_cid,
                    agent_role,
                    workspace_path,
                    primary_model,
                    created_at,
                    updated_at,
                    total_invocations,
                    total_steps,
                    total_tool_calls,
                    total_tool_errors,
                    user_turns,
                    total_in,
                    total_cache,
                    total_out,
                    peak_ctx,
                    baseline_sys,
                    comp_system_tokens,
                    comp_user_tokens,
                    comp_tool_tokens,
                    comp_output_tokens,
                    comp_thinking_tokens,
                    est_cost_usd,
                    est_cache_saved_usd,
                    str(t_path),
                    file_size,
                    file_mtime,
                ),
            )

            # Link discovered children
            for child_id, role_name in discovered_children:
                conn.execute(
                    """
                    UPDATE conversations
                    SET parent_conversation_id = ?,
                        agent_role = CASE WHEN agent_role IN ('', 'main') THEN ? ELSE agent_role END
                    WHERE conversation_id = ?
                    """,
                    (conversation_id, role_name, child_id),
                )

            conn.commit()

            latest_inv = conn.execute(
                "SELECT prompt_total_tokens FROM invocations WHERE conversation_id = ? ORDER BY step_index DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            latest_ctx = int(latest_inv["prompt_total_tokens"] or 0) if latest_inv else 0

            return {
                "status": "synced",
                "conversation_id": conversation_id,
                "incremental": is_incremental,
                "total_invocations": total_invocations,
                "peak_context_tokens": peak_ctx,
                "latest_context_tokens": latest_ctx,
                "discovered_children": len(discovered_children),
            }

    def sync_all_brains(
        self,
        force: bool = False,
        extra_brain_roots: list[str | Path] | None = None,
    ) -> dict[str, Any]:
        home_gemini = Path.home() / ".gemini"
        product_dirs = ["antigravity", "antigravity-cli", "antigravity-ide"]
        transcript_files: dict[str, Path] = {}

        brain_roots: list[Path] = [home_gemini / prod / "brain" for prod in product_dirs]
        env_extra = os.environ.get("TRACKER_EXTRA_BRAINS", "").strip()
        if env_extra:
            for p_str in env_extra.split(os.pathsep):
                if p_str.strip():
                    brain_roots.append(Path(p_str.strip()).expanduser().resolve())
        if extra_brain_roots:
            for p in extra_brain_roots:
                brain_roots.append(Path(p).expanduser().resolve())

        for brain_root in brain_roots:
            try:
                if not brain_root.exists() or not brain_root.is_dir():
                    continue
            except PermissionError:
                continue
            try:
                for conv_dir in brain_root.iterdir():
                    if not conv_dir.is_dir():
                        continue
                    t_file = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
                    try:
                        if t_file.exists():
                            transcript_files[conv_dir.name] = t_file
                    except PermissionError:
                        continue
            except PermissionError:
                continue

        # Also include any transcript_path already recorded in SQLite
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT conversation_id, transcript_path FROM conversations WHERE transcript_path != ''"
            ).fetchall()
            for r in rows:
                cid = r["conversation_id"]
                tp = Path(r["transcript_path"])
                try:
                    if cid not in transcript_files and tp.exists():
                        transcript_files[cid] = tp
                except PermissionError:
                    continue

        synced_count = 0
        up_to_date_count = 0
        errors = 0

        for cid, t_file in transcript_files.items():
            try:
                res = self.sync_transcript(t_file, conversation_id=cid, force=force)
                if res.get("status") == "synced":
                    synced_count += 1
                elif res.get("status") == "up_to_date":
                    up_to_date_count += 1
            except Exception:
                errors += 1

        # Cross-link parent-child relationships from all swarm/invoke_subagent results
        self._reconcile_parent_child_links()

        return {
            "total_discovered": len(transcript_files),
            "synced": synced_count,
            "up_to_date": up_to_date_count,
            "errors": errors,
        }

    @staticmethod
    def _pick_system_title(meta: dict[str, Any] | None) -> str:
        if not meta:
            return ""
        for field in ("title", "preview"):
            val = str(meta.get(field) or "").strip()
            if val and val not in ("---", "-", "--"):
                return " ".join(val.splitlines()).strip()[:120]
        return ""

    @staticmethod
    def _parse_workspace_from_uris(uri_field: str | None) -> str:
        if not uri_field:
            return ""
        raw = str(uri_field).strip()
        if not raw:
            return ""
        candidates: list[str] = []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    candidates = [str(x).strip() for x in parsed if x]
            except Exception:
                pass
        if not candidates:
            candidates = [
                part.strip()
                for part in re.split(r"[\s,;]+", raw)
                if part.strip()
            ]
        for uri in candidates:
            if uri.startswith("file:///"):
                p = unquote(uri[len("file:///"):])
                if re.match(r"^[a-zA-Z]:/", p):
                    return p.replace("/", "\\")
                return "/" + p.lstrip("/")
            if uri.startswith("file://"):
                return unquote(uri[len("file://"):])
        return ""

    def _load_system_conversation_metadata(self) -> dict[str, dict[str, Any]]:
        home_gemini = Path.home() / ".gemini"
        product_dirs = ("antigravity", "antigravity-cli", "antigravity-ide")
        meta_map: dict[str, dict[str, Any]] = {}

        for prod in product_dirs:
            db_path = home_gemini / prod / "conversation_summaries.db"
            try:
                if not db_path.exists() or not db_path.is_file():
                    continue
                conn = sqlite3.connect(
                    f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0
                )
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(
                        """
                        SELECT conversation_id, title, preview, agent_name,
                               parent_conversation_id, nesting_depth,
                               workspace_uris, last_modified_time
                        FROM conversation_summaries
                        """
                    ).fetchall()
                finally:
                    conn.close()
            except Exception:
                continue

            for r in rows:
                cid = str(r["conversation_id"] or "").strip()
                if not cid:
                    continue
                cur = meta_map.get(cid)
                title_val = str(r["title"] or "").strip()
                preview_val = str(r["preview"] or "").strip()
                agent_val = str(r["agent_name"] or "").strip()
                parent_val = str(r["parent_conversation_id"] or "").strip()
                ws_val = str(r["workspace_uris"] or "").strip()
                mtime_val = str(r["last_modified_time"] or "").strip()
                depth_val = int(r["nesting_depth"] or 0)

                if cur is None:
                    meta_map[cid] = {
                        "conversation_id": cid,
                        "title": title_val,
                        "preview": preview_val,
                        "agent_name": agent_val,
                        "parent_conversation_id": parent_val,
                        "nesting_depth": depth_val,
                        "workspace_uris": ws_val,
                        "last_modified_time": mtime_val,
                    }
                else:
                    if not cur["title"] and title_val:
                        cur["title"] = title_val
                    if not cur["preview"] and preview_val:
                        cur["preview"] = preview_val
                    if not cur["agent_name"] and agent_val:
                        cur["agent_name"] = agent_val
                    if not cur["parent_conversation_id"] and parent_val:
                        cur["parent_conversation_id"] = parent_val
                    if not cur["workspace_uris"] and ws_val:
                        cur["workspace_uris"] = ws_val
                    if not cur["last_modified_time"] and mtime_val:
                        cur["last_modified_time"] = mtime_val
                    if not cur["nesting_depth"] and depth_val:
                        cur["nesting_depth"] = depth_val

        return meta_map

    def _reconcile_parent_child_links(self) -> None:
        sys_meta = self._load_system_conversation_metadata()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._connect() as conn:
            convs = conn.execute(
                "SELECT conversation_id, transcript_path, agent_role, parent_conversation_id, workspace_path FROM conversations"
            ).fetchall()
            conv_by_id = {r["conversation_id"]: dict(r) for r in convs}
            known_ids = set(conv_by_id.keys())

            # Collect parent-child links: child_id -> (parent_id, role_name, is_swarm_worker)
            link_map: dict[str, tuple[str, str, bool]] = {}
            swarm_parents: set[str] = set()

            # 1) Authoritative links from Antigravity conversation_summaries.db
            for cid, meta in sys_meta.items():
                pid = str(meta.get("parent_conversation_id") or "").strip()
                role_name = str(meta.get("agent_name") or "").strip()
                if pid and pid != cid:
                    link_map[cid] = (pid, role_name, False)

            # 2) Fallback/enrich from <conv_dir>/.system_generated/subagents/*.json
            for parent_id, r in conv_by_id.items():
                tp_str = r.get("transcript_path") or ""
                if not tp_str:
                    continue
                conv_dir = Path(tp_str).parent.parent.parent
                sub_dir = conv_dir / ".system_generated" / "subagents"
                try:
                    if sub_dir.exists() and sub_dir.is_dir():
                        for jf in sub_dir.glob("*.json"):
                            try:
                                data = json.loads(
                                    jf.read_text(encoding="utf-8", errors="replace")
                                )
                            except Exception:
                                continue
                            if not isinstance(data, dict):
                                continue
                            child_id = str(data.get("conversationId") or "").strip()
                            if not child_id or child_id == parent_id:
                                continue
                            desc = data.get("subagentDescriptor") or {}
                            raw_role = str(desc.get("role") or "").strip()
                            type_name = str(desc.get("typeName") or "").strip()
                            is_swarm = False
                            m_sw = re.match(r"^Swarm Worker \(([^)]+)\)$", raw_role)
                            if m_sw:
                                role_clean = m_sw.group(1).strip()
                                is_swarm = True
                                swarm_parents.add(parent_id)
                            else:
                                role_clean = raw_role or type_name
                            existing_link = link_map.get(child_id)
                            if existing_link:
                                e_pid, e_role, e_sw = existing_link
                                link_map[child_id] = (
                                    e_pid or parent_id,
                                    e_role or role_clean,
                                    e_sw or is_swarm,
                                )
                            else:
                                link_map[child_id] = (parent_id, role_clean, is_swarm)
                except (PermissionError, OSError):
                    pass

                # Check if swarm plan.json exists
                plan_file = conv_dir / ".system_generated" / "swarm" / "plan.json"
                try:
                    if plan_file.exists():
                        swarm_parents.add(parent_id)
                except (PermissionError, OSError):
                    pass

            # 3) Auto-create lightweight parent placeholder rows if missing from conversations
            missing_parents: set[str] = set()
            for cid in list(known_ids):
                pid = (
                    link_map[cid][0]
                    if cid in link_map
                    else str(conv_by_id[cid].get("parent_conversation_id") or "").strip()
                )
                if pid and pid not in known_ids and pid in sys_meta:
                    missing_parents.add(pid)

            for pid in missing_parents:
                p_meta = sys_meta[pid]
                p_title = self._pick_system_title(p_meta) or f"Session {pid[:8]}"
                p_ws = self._parse_workspace_from_uris(p_meta.get("workspace_uris", ""))
                p_time = str(p_meta.get("last_modified_time") or now_iso)
                p_role = "coordinator" if pid in swarm_parents else "main"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO conversations (
                        conversation_id, title, parent_conversation_id, agent_role,
                        workspace_path, primary_model, created_at, updated_at,
                        total_invocations, total_steps, total_tool_calls, total_tool_errors, user_turns,
                        total_input_tokens, total_cache_read_tokens, total_output_tokens, peak_context_tokens,
                        baseline_system_tokens, comp_system_tokens, comp_user_tokens, comp_tool_tokens,
                        comp_output_tokens, comp_thinking_tokens, estimated_cost_usd, estimated_cache_saved_usd,
                        transcript_path, last_scanned_offset, last_scanned_mtime
                    ) VALUES (?, ?, '', ?, ?, '', ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, '', 0, 0.0)
                    """,
                    (pid, p_title, p_role, p_ws, p_time, p_time),
                )
                known_ids.add(pid)
                conv_by_id[pid] = {
                    "conversation_id": pid,
                    "transcript_path": "",
                    "agent_role": p_role,
                    "parent_conversation_id": "",
                    "workspace_path": p_ws,
                }

            # 4) Apply parent-child links from sys_meta and subagents/*.json
            for child_id, (parent_id, role_name, is_swarm) in link_map.items():
                if child_id in known_ids and parent_id in known_ids and child_id != parent_id:
                    conn.execute(
                        """
                        UPDATE conversations
                        SET parent_conversation_id = ?,
                            agent_role = CASE
                                WHEN ? != '' THEN ?
                                WHEN agent_role IN ('', 'main') THEN 'subagent'
                                ELSE agent_role
                            END
                        WHERE conversation_id = ?
                        """,
                        (parent_id, role_name, role_name, child_id),
                    )
                    if is_swarm:
                        swarm_parents.add(parent_id)

            # 5) Existing Swarm transcript regex fallback
            for parent_id, r in conv_by_id.items():
                tp_str = r.get("transcript_path") or ""
                if not tp_str:
                    continue
                tp = Path(tp_str)
                try:
                    if not tp.exists():
                        continue
                    raw_text = tp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                found_any = False
                for m in re.finditer(
                    r'\\"status\\":\\"added\\",\\"name\\":\\"([^"\\]+)\\"[^}]*?\\"conversation_id\\":\\"([0-9a-fA-F\-]{36})\\"',
                    raw_text,
                ):
                    w_name, child_id = m.group(1), m.group(2)
                    if child_id in known_ids and child_id != parent_id:
                        found_any = True
                        conn.execute(
                            """
                            UPDATE conversations
                            SET parent_conversation_id = ?,
                                agent_role = ?
                            WHERE conversation_id = ?
                            """,
                            (parent_id, w_name, child_id),
                        )

                for m in re.finditer(
                    r'"status"\s*:\s*"added"\s*,\s*"name"\s*:\s*"([^"]+)"[^}]*?"conversation_id"\s*:\s*"([0-9a-fA-F\-]{36})"',
                    raw_text,
                ):
                    w_name, child_id = m.group(1), m.group(2)
                    if child_id in known_ids and child_id != parent_id:
                        found_any = True
                        conn.execute(
                            """
                            UPDATE conversations
                            SET parent_conversation_id = ?,
                                agent_role = ?
                            WHERE conversation_id = ?
                            """,
                            (parent_id, w_name, child_id),
                        )

                if found_any:
                    swarm_parents.add(parent_id)

            # Ensure Swarm parents have 'coordinator' role, while non-Swarm parents keep 'main'
            for pid in swarm_parents:
                if pid in conv_by_id and conv_by_id[pid].get("agent_role") in ("", "main"):
                    conn.execute(
                        "UPDATE conversations SET agent_role = 'coordinator' WHERE conversation_id = ?",
                        (pid,),
                    )

            # 6) Apply system-assigned titles and workspace paths across all known conversations
            for cid in known_ids:
                meta = sys_meta.get(cid)
                if not meta:
                    continue
                sys_title = self._pick_system_title(meta)
                if sys_title:
                    conn.execute(
                        "UPDATE conversations SET title = ? WHERE conversation_id = ?",
                        (sys_title, cid),
                    )
                cur_ws = str(conv_by_id.get(cid, {}).get("workspace_path") or "").strip()
                if not cur_ws:
                    sys_ws = self._parse_workspace_from_uris(meta.get("workspace_uris", ""))
                    if sys_ws:
                        conn.execute(
                            "UPDATE conversations SET workspace_path = ? WHERE conversation_id = ?",
                            (sys_ws, cid),
                        )

            conn.commit()

    # ------------------------------------------------------------------
    # Hook Collector Handler
    # ------------------------------------------------------------------

    def record_hook_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"exceeded_warn_threshold": False, "latest_context_tokens": 0}

        conv_id = str(payload.get("conversationId") or payload.get("conversation_id") or "")
        hook_event = str(payload.get("hookEventName") or payload.get("hook_event_name") or "HOOK_EVENT")
        transcript_path = payload.get("transcriptPath") or payload.get("transcript_path")
        workspace_paths = payload.get("workspacePaths") or payload.get("workspace_roots") or []
        if not isinstance(workspace_paths, list):
            workspace_paths = []
        step_idx = int(payload.get("stepIdx", payload.get("invocationNum", 0)) or 0)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        sync_res: dict[str, Any] = {}
        try:
            tp_exists = bool(transcript_path and Path(str(transcript_path)).exists())
        except PermissionError:
            tp_exists = False
        if tp_exists:
            sync_res = self.sync_transcript(
                transcript_path,
                conversation_id=conv_id or None,
                workspace_paths=[str(p) for p in workspace_paths],
            )
            if not conv_id:
                conv_id = str(sync_res.get("conversation_id", ""))

        if conv_id:
            with self._connect() as conn:
                if hook_event == "PostToolUse":
                    tc = payload.get("toolCall") or {}
                    t_name = str(tc.get("name", "unknown"))
                    t_args = tc.get("args") or {}
                    target = self._extract_target_resource(
                        t_name, t_args if isinstance(t_args, dict) else {}
                    )
                    err_str = str(payload.get("error", "") or "")
                    if err_str:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO behavior_events (
                                conversation_id, step_index, event_type, tool_name, target_resource,
                                arg_bytes, result_bytes, duration_ms, is_error, error_message,
                                thinking_preview, byte_offset, byte_length, created_at
                            ) VALUES (?, ?, 'HOOK_EVENT', ?, ?, 0, 0, 0, 1, ?, ?, 0, 0, ?)
                            """,
                            (
                                conv_id,
                                step_idx,
                                t_name,
                                target,
                                err_str[:240],
                                f"PostToolUse error on {t_name}: {err_str[:160]}",
                                now_iso,
                            ),
                        )
                        conn.execute(
                            """
                            UPDATE conversations
                            SET total_tool_errors = total_tool_errors + 1
                            WHERE conversation_id = ?
                            """,
                            (conv_id,),
                        )
                elif hook_event == "Stop":
                    reason = str(payload.get("terminationReason", "NO_TOOL_CALL") or "NO_TOOL_CALL")
                    err_str = str(payload.get("error", "") or "")
                    is_err = 1 if (err_str or reason in ("ERROR", "MAX_TOKEN_BUDGET_EXCEEDED")) else 0
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO behavior_events (
                            conversation_id, step_index, event_type, tool_name, target_resource,
                            arg_bytes, result_bytes, duration_ms, is_error, error_message,
                            thinking_preview, byte_offset, byte_length, created_at
                        ) VALUES (?, ?, 'STOP', 'agent_stop', ?, 0, 0, 0, ?, ?, ?, 0, 0, ?)
                        """,
                        (
                            conv_id,
                            step_idx,
                            reason,
                            is_err,
                            err_str[:240],
                            f"Agent stopped ({reason})",
                            now_iso,
                        ),
                    )
                elif hook_event == "SessionStart":
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO behavior_events (
                            conversation_id, step_index, event_type, tool_name, target_resource,
                            arg_bytes, result_bytes, duration_ms, is_error, error_message,
                            thinking_preview, byte_offset, byte_length, created_at
                        ) VALUES (?, 0, 'HOOK_EVENT', 'session_start', ?, 0, 0, 0, 0, '', 'Session started', 0, 0, ?)
                        """,
                        (
                            conv_id,
                            str(payload.get("modelName", "auto")),
                            now_iso,
                        ),
                    )
                conn.commit()

        settings = self.get_settings()
        warn_threshold = int(settings.get("context_warn_threshold", 120000) or 120000)
        enable_warn = bool(settings.get("enable_context_warn_hook", True))

        latest_ctx = int(sync_res.get("latest_context_tokens", 0) or 0)
        peak_ctx = int(sync_res.get("peak_context_tokens", 0) or 0)
        if conv_id and latest_ctx == 0:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT peak_context_tokens FROM conversations WHERE conversation_id = ?",
                    (conv_id,),
                ).fetchone()
                if row:
                    peak_ctx = int(row["peak_context_tokens"] or 0)
                    latest_ctx = peak_ctx

        exceeded = enable_warn and (latest_ctx >= warn_threshold)
        return {
            "conversation_id": conv_id,
            "hook_event": hook_event,
            "latest_context_tokens": latest_ctx,
            "peak_context_tokens": peak_ctx,
            "context_warn_threshold": warn_threshold,
            "enable_context_warn_hook": enable_warn,
            "exceeded_warn_threshold": exceeded,
        }

    # ------------------------------------------------------------------
    # Analytics & Dashboard Queries
    # ------------------------------------------------------------------

    def get_global_overview(
        self,
        days: int = 7,
        search: str = "",
        include_children: bool = False,
    ) -> dict[str, Any]:
        settings = self.get_settings()
        days = max(1, int(days or 7))
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_date_str = cutoff_dt.strftime("%Y-%m-%d")

        with self._connect() as conn:
            all_convs = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM conversations ORDER BY updated_at DESC"
                ).fetchall()
            ]

            # Build parent -> children map
            children_by_parent: dict[str, list[dict[str, Any]]] = {}
            conv_by_id: dict[str, dict[str, Any]] = {}
            for c in all_convs:
                cid = c["conversation_id"]
                total_tok = (
                    int(c["total_input_tokens"] or 0)
                    + int(c["total_cache_read_tokens"] or 0)
                    + int(c["total_output_tokens"] or 0)
                )
                prompt_tok = int(c["total_input_tokens"] or 0) + int(
                    c["total_cache_read_tokens"] or 0
                )
                c["total_tokens"] = total_tok
                c["cache_hit_ratio"] = (
                    round(int(c["total_cache_read_tokens"] or 0) / prompt_tok, 4)
                    if prompt_tok > 0
                    else 0.0
                )
                conv_by_id[cid] = c
                pid = c.get("parent_conversation_id") or ""
                if pid:
                    children_by_parent.setdefault(pid, []).append(c)

            # Enrich conversations with fleet totals
            root_convs: list[dict[str, Any]] = []
            for c in all_convs:
                cid = c["conversation_id"]
                kids = children_by_parent.get(cid, [])
                kids_sorted = sorted(
                    kids, key=lambda x: x.get("created_at", ""), reverse=False
                )
                fleet_in = int(c["total_input_tokens"] or 0) + sum(
                    int(k["total_input_tokens"] or 0) for k in kids_sorted
                )
                fleet_cache = int(c["total_cache_read_tokens"] or 0) + sum(
                    int(k["total_cache_read_tokens"] or 0) for k in kids_sorted
                )
                fleet_out = int(c["total_output_tokens"] or 0) + sum(
                    int(k["total_output_tokens"] or 0) for k in kids_sorted
                )
                fleet_tot = fleet_in + fleet_cache + fleet_out
                fleet_tools = int(c["total_tool_calls"] or 0) + sum(
                    int(k["total_tool_calls"] or 0) for k in kids_sorted
                )
                fleet_cost = round(
                    float(c["estimated_cost_usd"] or 0.0)
                    + sum(float(k["estimated_cost_usd"] or 0.0) for k in kids_sorted),
                    6,
                )
                c["fleet_total_tokens"] = fleet_tot
                c["fleet_input_tokens"] = fleet_in
                c["fleet_cache_read_tokens"] = fleet_cache
                c["fleet_output_tokens"] = fleet_out
                c["fleet_tool_calls"] = fleet_tools
                c["fleet_cost_usd"] = fleet_cost
                c["child_count"] = len(kids_sorted)
                c["children"] = [
                    {
                        "conversation_id": k["conversation_id"],
                        "title": k["title"],
                        "agent_role": k["agent_role"],
                        "primary_model": k["primary_model"],
                        "total_tokens": k["total_tokens"],
                        "total_input_tokens": k["total_input_tokens"],
                        "total_cache_read_tokens": k["total_cache_read_tokens"],
                        "total_output_tokens": k["total_output_tokens"],
                        "cache_hit_ratio": k["cache_hit_ratio"],
                        "total_invocations": k["total_invocations"],
                        "total_tool_calls": k["total_tool_calls"],
                        "total_tool_errors": k["total_tool_errors"],
                        "peak_context_tokens": k["peak_context_tokens"],
                        "estimated_cost_usd": k["estimated_cost_usd"],
                        "created_at": k["created_at"],
                        "updated_at": k["updated_at"],
                    }
                    for k in kids_sorted
                ]

                pid = c.get("parent_conversation_id") or ""
                if not pid or pid not in conv_by_id:
                    root_convs.append(c)

            # Global KPIs across all conversations
            tot_in = sum(int(c["total_input_tokens"] or 0) for c in all_convs)
            tot_cache = sum(int(c["total_cache_read_tokens"] or 0) for c in all_convs)
            tot_out = sum(int(c["total_output_tokens"] or 0) for c in all_convs)
            tot_tokens = tot_in + tot_cache + tot_out
            tot_prompt = tot_in + tot_cache
            avg_cache_hit = (
                round(tot_cache / tot_prompt, 4) if tot_prompt > 0 else 0.0
            )
            tot_inv = sum(int(c["total_invocations"] or 0) for c in all_convs)
            tot_tools = sum(int(c["total_tool_calls"] or 0) for c in all_convs)
            tot_errs = sum(int(c["total_tool_errors"] or 0) for c in all_convs)
            peak_ctx = max(
                [int(c["peak_context_tokens"] or 0) for c in all_convs] or [0]
            )
            tot_cost = round(
                sum(float(c["estimated_cost_usd"] or 0.0) for c in all_convs), 4
            )
            tot_saved_usd = round(
                sum(float(c["estimated_cache_saved_usd"] or 0.0) for c in all_convs), 4
            )

            kpis = {
                "total_tokens": tot_tokens,
                "total_input_tokens": tot_in,
                "total_cache_read_tokens": tot_cache,
                "total_output_tokens": tot_out,
                "avg_cache_hit_ratio": avg_cache_hit,
                "cache_saved_tokens": tot_cache,
                "total_conversations": len(all_convs),
                "root_conversations": len(root_convs),
                "child_conversations": len(all_convs) - len(root_convs),
                "total_invocations": tot_inv,
                "total_tool_calls": tot_tools,
                "total_tool_errors": tot_errs,
                "peak_context_tokens": peak_ctx,
                "estimated_cost_usd": tot_cost,
                "estimated_cache_saved_usd": tot_saved_usd,
            }

            # Aggregate 5-category composition
            aggregate_composition = {
                "system_tokens": sum(int(c["comp_system_tokens"] or 0) for c in all_convs),
                "user_tokens": sum(int(c["comp_user_tokens"] or 0) for c in all_convs),
                "tool_tokens": sum(int(c["comp_tool_tokens"] or 0) for c in all_convs),
                "output_tokens": sum(int(c["comp_output_tokens"] or 0) for c in all_convs),
                "thinking_tokens": sum(
                    int(c["comp_thinking_tokens"] or 0) for c in all_convs
                ),
            }

            # Daily trend (fill all days in range)
            daily_map: dict[str, dict[str, Any]] = {}
            now_utc = datetime.now(timezone.utc)
            for d_idx in range(days - 1, -1, -1):
                d_str = (now_utc - timedelta(days=d_idx)).strftime("%Y-%m-%d")
                daily_map[d_str] = {
                    "date": d_str,
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "invocations": 0,
                    "tool_calls": 0,
                    "cost_usd": 0.0,
                }

            inv_daily_rows = conn.execute(
                """
                SELECT
                    SUBSTR(created_at, 1, 10) AS d,
                    SUM(input_tokens) AS s_in,
                    SUM(cache_read_tokens) AS s_cache,
                    SUM(output_tokens) AS s_out,
                    COUNT(*) AS s_inv,
                    SUM(tool_call_count) AS s_tc,
                    SUM(estimated_cost_usd) AS s_cost
                FROM invocations
                WHERE created_at != ''
                GROUP BY d
                ORDER BY d ASC
                """
            ).fetchall()

            for r in inv_daily_rows:
                d_str = r["d"]
                if not d_str or len(d_str) != 10:
                    continue
                if d_str not in daily_map and d_str >= cutoff_date_str:
                    daily_map[d_str] = {
                        "date": d_str,
                        "input_tokens": 0,
                        "cache_read_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "invocations": 0,
                        "tool_calls": 0,
                        "cost_usd": 0.0,
                    }
                if d_str in daily_map:
                    in_t = int(r["s_in"] or 0)
                    ca_t = int(r["s_cache"] or 0)
                    ou_t = int(r["s_out"] or 0)
                    daily_map[d_str]["input_tokens"] += in_t
                    daily_map[d_str]["cache_read_tokens"] += ca_t
                    daily_map[d_str]["output_tokens"] += ou_t
                    daily_map[d_str]["total_tokens"] += in_t + ca_t + ou_t
                    daily_map[d_str]["invocations"] += int(r["s_inv"] or 0)
                    daily_map[d_str]["tool_calls"] += int(r["s_tc"] or 0)
                    daily_map[d_str]["cost_usd"] = round(
                        daily_map[d_str]["cost_usd"] + float(r["s_cost"] or 0.0), 4
                    )

            daily_trend = [daily_map[k] for k in sorted(daily_map.keys())]

            # 7x24 Activity Heatmap (day_of_week: 0=Mon..6=Sun, hour: 0..23)
            heatmap_grid: dict[tuple[int, int], dict[str, int]] = {
                (d, h): {"day_of_week": d, "hour": h, "invocations": 0, "total_tokens": 0}
                for d in range(7)
                for h in range(24)
            }
            all_inv_times = conn.execute(
                "SELECT created_at, input_tokens + cache_read_tokens + output_tokens AS tot FROM invocations WHERE created_at != ''"
            ).fetchall()
            for r in all_inv_times:
                dt = _parse_iso_dt(r["created_at"])
                if dt:
                    local_dt = dt.astimezone()
                    key = (local_dt.weekday(), local_dt.hour)
                    heatmap_grid[key]["invocations"] += 1
                    heatmap_grid[key]["total_tokens"] += int(r["tot"] or 0)
            activity_heatmap = [
                heatmap_grid[(d, h)] for d in range(7) for h in range(24)
            ]

            # Model breakdown
            model_rows = conn.execute(
                """
                SELECT
                    CASE WHEN model = '' THEN 'unknown' ELSE model END AS m,
                    COUNT(*) AS inv_cnt,
                    SUM(input_tokens) AS s_in,
                    SUM(cache_read_tokens) AS s_cache,
                    SUM(output_tokens) AS s_out,
                    SUM(estimated_cost_usd) AS s_cost
                FROM invocations
                GROUP BY m
                ORDER BY (SUM(input_tokens) + SUM(cache_read_tokens) + SUM(output_tokens)) DESC
                """
            ).fetchall()
            model_breakdown = []
            for r in model_rows:
                m_in = int(r["s_in"] or 0)
                m_ca = int(r["s_cache"] or 0)
                m_ou = int(r["s_out"] or 0)
                m_pr = m_in + m_ca
                model_breakdown.append(
                    {
                        "model": r["m"],
                        "invocations": int(r["inv_cnt"] or 0),
                        "input_tokens": m_in,
                        "cache_read_tokens": m_ca,
                        "output_tokens": m_ou,
                        "total_tokens": m_in + m_ca + m_ou,
                        "cache_hit_ratio": round(m_ca / m_pr, 4) if m_pr > 0 else 0.0,
                        "cost_usd": round(float(r["s_cost"] or 0.0), 4),
                    }
                )

            # Tool breakdown
            tool_rows = conn.execute(
                """
                SELECT
                    tool_name,
                    COUNT(*) AS call_count,
                    SUM(CASE WHEN is_error = 1 THEN 1 ELSE 0 END) AS error_count,
                    AVG(duration_ms) AS avg_dur,
                    SUM(arg_bytes) AS s_arg,
                    SUM(result_bytes) AS s_res
                FROM behavior_events
                WHERE event_type = 'TOOL_CALL' AND tool_name != ''
                GROUP BY tool_name
                ORDER BY call_count DESC
                """
            ).fetchall()
            tool_breakdown = [
                {
                    "tool_name": r["tool_name"],
                    "call_count": int(r["call_count"] or 0),
                    "error_count": int(r["error_count"] or 0),
                    "avg_duration_ms": int(round(float(r["avg_dur"] or 0.0))),
                    "total_arg_bytes": int(r["s_arg"] or 0),
                    "total_result_bytes": int(r["s_res"] or 0),
                }
                for r in tool_rows
            ]

            # Filter conversations by search query
            target_list = all_convs if include_children else root_convs
            if search:
                q = search.strip().lower()
                filtered = []
                for c in target_list:
                    hay = f"{c['conversation_id']} {c['title']} {c['primary_model']} {c['agent_role']} {c['workspace_path']}".lower()
                    child_match = any(
                        q
                        in f"{k['conversation_id']} {k['title']} {k['agent_role']}".lower()
                        for k in c.get("children", [])
                    )
                    if q in hay or child_match:
                        filtered.append(c)
                target_list = filtered

            return {
                "kpis": kpis,
                "daily_trend": daily_trend,
                "activity_heatmap": activity_heatmap,
                "model_breakdown": model_breakdown,
                "tool_breakdown": tool_breakdown,
                "aggregate_composition": aggregate_composition,
                "conversations": target_list,
                "settings": settings,
            }

    def get_conversation_detail(
        self, conversation_id: str | None = None
    ) -> dict[str, Any]:
        settings = self.get_settings()
        warn_threshold = int(settings.get("context_warn_threshold", 120000) or 120000)
        window_limit = int(settings.get("context_window_limit", 200000) or 200000)

        with self._connect() as conn:
            conv_row = None
            if conversation_id:
                conv_row = conn.execute(
                    "SELECT * FROM conversations WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if not conv_row:
                    # Try prefix match
                    conv_row = conn.execute(
                        "SELECT * FROM conversations WHERE conversation_id LIKE ? ORDER BY updated_at DESC LIMIT 1",
                        (f"{conversation_id}%",),
                    ).fetchone()
            if not conv_row:
                # Prefer root conversations with activity first
                conv_row = conn.execute(
                    "SELECT * FROM conversations WHERE parent_conversation_id = '' ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
            if not conv_row:
                conv_row = conn.execute(
                    "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()

            if not conv_row:
                return {
                    "conversation": None,
                    "composition": {
                        "system_tokens": 0,
                        "user_tokens": 0,
                        "tool_tokens": 0,
                        "output_tokens": 0,
                        "thinking_tokens": 0,
                    },
                    "context_pressure": {
                        "current_context_tokens": 0,
                        "peak_context_tokens": 0,
                        "warn_threshold": warn_threshold,
                        "window_limit": window_limit,
                        "occupancy_ratio": 0.0,
                        "status": "normal",
                    },
                    "invocations": [],
                    "behavior_timeline": [],
                    "file_hotspots": [],
                    "command_hotspots": [],
                    "fleet": {
                        "root_conversation_id": "",
                        "fleet_total_tokens": 0,
                        "members": [],
                        "plan_json": None,
                    },
                    "settings": settings,
                }

            conv = dict(conv_row)
            cid = conv["conversation_id"]
            tot_in = int(conv["total_input_tokens"] or 0)
            tot_ca = int(conv["total_cache_read_tokens"] or 0)
            tot_ou = int(conv["total_output_tokens"] or 0)
            tot_pr = tot_in + tot_ca
            conv["total_tokens"] = tot_in + tot_ca + tot_ou
            conv["cache_hit_ratio"] = (
                round(tot_ca / tot_pr, 4) if tot_pr > 0 else 0.0
            )

            invocations = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM invocations WHERE conversation_id = ? ORDER BY step_index ASC",
                    (cid,),
                ).fetchall()
            ]
            current_ctx = (
                int(invocations[-1]["prompt_total_tokens"] or 0)
                if invocations
                else 0
            )
            peak_ctx = int(conv["peak_context_tokens"] or 0)
            occupancy = (
                round(current_ctx / window_limit, 4) if window_limit > 0 else 0.0
            )
            status = (
                "critical"
                if current_ctx >= window_limit
                else ("warning" if current_ctx >= warn_threshold else "healthy")
            )

            context_pressure = {
                "current_context_tokens": current_ctx,
                "peak_context_tokens": peak_ctx,
                "warn_threshold": warn_threshold,
                "window_limit": window_limit,
                "occupancy_ratio": occupancy,
                "status": status,
            }

            composition = {
                "system_tokens": int(conv["comp_system_tokens"] or 0),
                "user_tokens": int(conv["comp_user_tokens"] or 0),
                "tool_tokens": int(conv["comp_tool_tokens"] or 0),
                "output_tokens": int(conv["comp_output_tokens"] or 0),
                "thinking_tokens": int(conv["comp_thinking_tokens"] or 0),
            }

            behavior_timeline = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM behavior_events WHERE conversation_id = ? ORDER BY step_index ASC, id ASC",
                    (cid,),
                ).fetchall()
            ]

            # File hotspots (read_file, edit_file, write_file, view_file, etc.)
            file_rows = conn.execute(
                """
                SELECT
                    target_resource,
                    COUNT(*) AS access_count,
                    SUM(CASE WHEN tool_name IN ('edit_file', 'write_file') THEN 1 ELSE 0 END) AS write_count,
                    SUM(CASE WHEN tool_name NOT IN ('edit_file', 'write_file') THEN 1 ELSE 0 END) AS read_count,
                    SUM(result_bytes + arg_bytes) AS total_bytes
                FROM behavior_events
                WHERE conversation_id = ?
                  AND event_type = 'TOOL_CALL'
                  AND tool_name IN ('read_file', 'edit_file', 'write_file', 'view_file')
                  AND target_resource != ''
                GROUP BY target_resource
                ORDER BY access_count DESC, total_bytes DESC
                LIMIT 20
                """,
                (cid,),
            ).fetchall()

            file_hotspots = []
            for r in file_rows:
                raw_target = re.sub(r"\s+#\d+$", "", str(r["target_resource"])).strip()
                acc_cnt = int(r["access_count"] or 0)
                rd_cnt = int(r["read_count"] or 0)
                wr_cnt = int(r["write_count"] or 0)
                uri_str = _path_to_file_uri(raw_target)
                file_hotspots.append(
                    {
                        "path": raw_target,
                        "target_resource": raw_target,
                        "basename": Path(raw_target.replace("\\", "/")).name or raw_target,
                        "file_uri": uri_str,
                        "uri": uri_str,
                        "access_count": acc_cnt,
                        "count": acc_cnt,
                        "read_count": rd_cnt,
                        "reads": rd_cnt,
                        "write_count": wr_cnt,
                        "edits": wr_cnt,
                        "total_bytes": int(r["total_bytes"] or 0),
                    }
                )

            # Command hotspots
            cmd_rows = conn.execute(
                """
                SELECT
                    target_resource,
                    COUNT(*) AS exec_count,
                    SUM(CASE WHEN is_error = 1 THEN 1 ELSE 0 END) AS error_count,
                    AVG(duration_ms) AS avg_duration_ms,
                    SUM(result_bytes) AS total_result_bytes
                FROM behavior_events
                WHERE conversation_id = ?
                  AND event_type = 'TOOL_CALL'
                  AND tool_name = 'run_command'
                  AND target_resource != ''
                GROUP BY target_resource
                ORDER BY exec_count DESC, avg_duration_ms DESC
                LIMIT 15
                """,
                (cid,),
            ).fetchall()

            command_hotspots = [
                {
                    "command": re.sub(r"\s+#\d+$", "", str(r["target_resource"])).strip(),
                    "target_resource": re.sub(r"\s+#\d+$", "", str(r["target_resource"])).strip(),
                    "exec_count": int(r["exec_count"] or 0),
                    "count": int(r["exec_count"] or 0),
                    "error_count": int(r["error_count"] or 0),
                    "errors": int(r["error_count"] or 0),
                    "avg_duration_ms": int(round(float(r["avg_duration_ms"] or 0.0))),
                    "total_result_bytes": int(r["total_result_bytes"] or 0),
                }
                for r in cmd_rows
            ]

            # Build Fleet hierarchy (find root coordinator/parent + all siblings/children)
            root_id = conv["parent_conversation_id"] or cid
            root_row = conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?",
                (root_id,),
            ).fetchone()
            if not root_row:
                root_id = cid
                root_row = conv_row

            child_rows = conn.execute(
                "SELECT * FROM conversations WHERE parent_conversation_id = ? ORDER BY created_at ASC",
                (root_id,),
            ).fetchall()

            fleet_members_raw = [dict(root_row)] + [dict(r) for r in child_rows]
            conv["parent_title"] = (
                str(root_row["title"] or "")
                if conv.get("parent_conversation_id") and root_row
                else ""
            )
            fleet_total_tokens = sum(
                int(m["total_input_tokens"] or 0)
                + int(m["total_cache_read_tokens"] or 0)
                + int(m["total_output_tokens"] or 0)
                for m in fleet_members_raw
            )

            fleet_members = []
            for m in fleet_members_raw:
                m_in = int(m["total_input_tokens"] or 0)
                m_ca = int(m["total_cache_read_tokens"] or 0)
                m_ou = int(m["total_output_tokens"] or 0)
                m_tot = m_in + m_ca + m_ou
                m_pr = m_in + m_ca
                fleet_members.append(
                    {
                        "conversation_id": m["conversation_id"],
                        "parent_conversation_id": m.get("parent_conversation_id") or "",
                        "title": m["title"],
                        "agent_role": m["agent_role"]
                        or ("coordinator" if m["conversation_id"] == root_id and child_rows else "main"),
                        "is_root": m["conversation_id"] == root_id,
                        "is_current": m["conversation_id"] == cid,
                        "primary_model": m["primary_model"],
                        "total_tokens": m_tot,
                        "token_share": (
                            round(m_tot / fleet_total_tokens, 4)
                            if fleet_total_tokens > 0
                            else 0.0
                        ),
                        "total_input_tokens": m_in,
                        "total_cache_read_tokens": m_ca,
                        "total_output_tokens": m_ou,
                        "cache_hit_ratio": round(m_ca / m_pr, 4) if m_pr > 0 else 0.0,
                        "peak_context_tokens": int(m["peak_context_tokens"] or 0),
                        "total_invocations": int(m["total_invocations"] or 0),
                        "total_tool_calls": int(m["total_tool_calls"] or 0),
                        "total_tool_errors": int(m["total_tool_errors"] or 0),
                        "estimated_cost_usd": float(m["estimated_cost_usd"] or 0.0),
                        "created_at": m["created_at"],
                        "updated_at": m["updated_at"],
                    }
                )

            plan_json = None
            root_tp = root_row["transcript_path"] if root_row else ""
            if root_tp:
                plan_file = Path(root_tp).parent.parent / "swarm" / "plan.json"
                try:
                    if plan_file.exists():
                        plan_json = json.loads(
                            plan_file.read_text(encoding="utf-8", errors="replace")
                        )
                except Exception:
                    plan_json = None

            fleet = {
                "root_conversation_id": root_id,
                "fleet_total_tokens": fleet_total_tokens,
                "member_count": len(fleet_members),
                "members": fleet_members,
                "plan_json": plan_json,
            }

            return {
                "conversation": conv,
                "composition": composition,
                "context_pressure": context_pressure,
                "invocations": invocations,
                "behavior_timeline": behavior_timeline,
                "file_hotspots": file_hotspots,
                "command_hotspots": command_hotspots,
                "fleet": fleet,
                "settings": settings,
            }

    # ------------------------------------------------------------------
    # Context Browser On-Demand Raw Step Reader
    # ------------------------------------------------------------------

    def get_step_raw_payload(
        self, conversation_id: str, step_index: int
    ) -> dict[str, Any]:
        step_index = int(step_index)
        with self._connect() as conn:
            conv = conn.execute(
                "SELECT transcript_path FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if not conv or not conv["transcript_path"]:
                return {
                    "error": f"Conversation {conversation_id} not found",
                    "conversation_id": conversation_id,
                    "step_index": step_index,
                }

            t_path = Path(conv["transcript_path"])
            if not t_path.exists():
                return {
                    "error": f"Transcript file missing: {t_path}",
                    "conversation_id": conversation_id,
                    "step_index": step_index,
                }

            inv = conn.execute(
                "SELECT byte_offset, byte_length FROM invocations WHERE conversation_id = ? AND step_index = ?",
                (conversation_id, step_index),
            ).fetchone()
            beh = conn.execute(
                "SELECT byte_offset, byte_length FROM behavior_events WHERE conversation_id = ? AND step_index = ? LIMIT 1",
                (conversation_id, step_index),
            ).fetchone()

        target_step: dict[str, Any] | None = None
        tool_results: list[dict[str, Any]] = []

        # 1. Try fast direct byte-offset read first
        offset_row = inv or beh
        if offset_row and int(offset_row["byte_length"] or 0) > 0:
            try:
                with open(t_path, "rb") as f:
                    f.seek(int(offset_row["byte_offset"]))
                    raw_bytes = f.read(int(offset_row["byte_length"]))
                    candidate = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                    if int(candidate.get("step_index", -1)) == step_index:
                        target_step = candidate
                        # Read immediate following GENERIC tool output steps
                        for _ in range(12):
                            next_line = f.readline()
                            if not next_line:
                                break
                            next_obj = json.loads(
                                next_line.decode("utf-8", errors="replace")
                            )
                            if next_obj.get("type") == "GENERIC":
                                tool_results.append(
                                    {
                                        "step_index": next_obj.get("step_index"),
                                        "created_at": next_obj.get("created_at", ""),
                                        "content": str(next_obj.get("content", ""))[:15000],
                                        "truncated_fields": next_obj.get(
                                            "truncated_fields", []
                                        ),
                                    }
                                )
                            else:
                                break
            except Exception:
                target_step = None

        # 2. Fallback linear scan if offset changed or not indexed
        if target_step is None:
            try:
                with open(t_path, "r", encoding="utf-8", errors="replace") as f:
                    found = False
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        s_idx = int(obj.get("step_index", -1))
                        if s_idx == step_index:
                            target_step = obj
                            found = True
                        elif found:
                            if obj.get("type") == "GENERIC":
                                tool_results.append(
                                    {
                                        "step_index": s_idx,
                                        "created_at": obj.get("created_at", ""),
                                        "content": str(obj.get("content", ""))[:15000],
                                        "truncated_fields": obj.get(
                                            "truncated_fields", []
                                        ),
                                    }
                                )
                            else:
                                break
            except Exception as e:
                return {
                    "error": str(e),
                    "conversation_id": conversation_id,
                    "step_index": step_index,
                }

        if not target_step:
            return {
                "error": f"Step {step_index} not found in transcript",
                "conversation_id": conversation_id,
                "step_index": step_index,
            }

        # Check transcript_full.jsonl if target_step had truncated_fields
        full_path = t_path.parent / "transcript_full.jsonl"
        try:
            full_exists = full_path.exists()
        except PermissionError:
            full_exists = False
        if target_step.get("truncated_fields") and full_exists:
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as ff:
                    for line in ff:
                        if f'"step_index":{step_index},' in line or f'"step_index": {step_index},' in line:
                            full_obj = json.loads(line)
                            if int(full_obj.get("step_index", -1)) == step_index:
                                target_step = full_obj
                                break
            except Exception:
                pass

        in_tok = int(target_step.get("input_tokens", 0) or 0)
        ca_tok = int(target_step.get("cache_read_tokens", 0) or 0)
        ou_tok = int(target_step.get("output_tokens", 0) or 0)
        pr_tok = in_tok + ca_tok

        return {
            "conversation_id": conversation_id,
            "step_index": step_index,
            "transcript_path": str(t_path),
            "type": target_step.get("type", ""),
            "source": target_step.get("source", ""),
            "status": target_step.get("status", ""),
            "created_at": target_step.get("created_at", ""),
            "model": target_step.get("model", ""),
            "token_headers": {
                "model": target_step.get("model", ""),
                "input_tokens": in_tok,
                "cache_read_tokens": ca_tok,
                "output_tokens": ou_tok,
                "prompt_total_tokens": pr_tok,
                "cache_hit_ratio": round(ca_tok / pr_tok, 4) if pr_tok > 0 else 0.0,
            },
            "thinking": str(target_step.get("thinking", "") or ""),
            "content": str(target_step.get("content", "") or "")[:25000],
            "tool_calls": target_step.get("tool_calls") or [],
            "tool_results": tool_results,
            "truncated_fields": target_step.get("truncated_fields") or [],
        }

    # ------------------------------------------------------------------
    # Report Export (Markdown / JSON / CSV)
    # ------------------------------------------------------------------

    def export_report(
        self, conversation_id: str | None = None, fmt: str = "markdown"
    ) -> str:
        fmt_low = (fmt or "markdown").strip().lower()
        if conversation_id:
            data = self.get_conversation_detail(conversation_id)
            if fmt_low == "json":
                return json.dumps(data, ensure_ascii=False, indent=2)
            if fmt_low == "csv":
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(
                    [
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
                        "thinking_chars",
                        "estimated_cost_usd",
                    ]
                )
                for inv in data.get("invocations", []):
                    writer.writerow(
                        [
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
                            inv.get("thinking_chars"),
                            inv.get("estimated_cost_usd"),
                        ]
                    )
                return buf.getvalue()

            # Markdown format for single conversation
            c = data.get("conversation") or {}
            cp = data.get("context_pressure") or {}
            comp = data.get("composition") or {}
            fleet = data.get("fleet") or {}
            lines = [
                f"# Token & AI 行为分析报告 — `{c.get('title', 'Unknown')}`",
                "",
                f"- **会话 ID**: `{c.get('conversation_id', '')}`",
                f"- **角色 / 代理类型**: `{c.get('agent_role', 'main')}`",
                f"- **主力模型**: `{c.get('primary_model', 'unknown')}`",
                f"- **创建时间**: `{c.get('created_at', '')}` | **最后更新**: `{c.get('updated_at', '')}`",
                "",
                "## 1. Token 与缓存核心指标",
                "",
                "| 指标 | 数值 | 说明 |",
                "| :--- | :--- | :--- |",
                f"| **总消耗 Tokens** | `{c.get('total_tokens', 0):,}` | Input + Cache Read + Output |",
                f"| **未命中新增输入 (`input_tokens`)** | `{c.get('total_input_tokens', 0):,}` | 实际新增计费/计算的 Prompt Token |",
                f"| **前缀缓存命中 (`cache_read_tokens`)** | `{c.get('total_cache_read_tokens', 0):,}` | 命中缓存复用的 Prompt Token |",
                f"| **模型输出 (`output_tokens`)** | `{c.get('total_output_tokens', 0):,}` | 含思考过程与工具调用输出 |",
                f"| **Prompt 缓存命中率** | `{c.get('cache_hit_ratio', 0.0) * 100:.1f}%` | 缓存节省效率 |",
                f"| **当前 / 峰值上下文水位** | `{cp.get('current_context_tokens', 0):,}` / `{cp.get('peak_context_tokens', 0):,}` | 预警线 `{cp.get('warn_threshold', 120000):,}` ({cp.get('occupancy_ratio', 0.0) * 100:.1f}%) |",
                f"| **等效 API 成本 / 缓存节省** | `${c.get('estimated_cost_usd', 0.0):.4f}` / 省 `${c.get('estimated_cache_saved_usd', 0.0):.4f}` | 按配置费率表折算 |",
                "",
                "## 2. 上下文构成拆解 (5-Category Attribution)",
                "",
                f"- **System & Tools 基线开销**: `{comp.get('system_tokens', 0):,}` tokens",
                f"- **用户输入 (`User Input`)**: `{comp.get('user_tokens', 0):,}` tokens",
                f"- **工具返回与文件注入 (`Tool Outputs`)**: `{comp.get('tool_tokens', 0):,}` tokens",
                f"- **模型回复正文 (`Model Output`)**: `{comp.get('output_tokens', 0):,}` tokens",
                f"- **深度思考 (`Thinking`)**: `{comp.get('thinking_tokens', 0):,}` tokens",
                "",
            ]
            if fleet.get("member_count", 0) > 1:
                lines.extend(
                    [
                        f"## 3. 多 Agent 舰队协同 (`Fleet Total`: `{fleet.get('fleet_total_tokens', 0):,}` tokens)",
                        "",
                        "| Agent 角色 | 会话 ID | 模型 | 总 Tokens | 占比 | 缓存命中率 | 工具调用 |",
                        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
                    ]
                )
                for m in fleet.get("members", []):
                    lines.append(
                        f"| `{m['agent_role']}` | `{m['conversation_id'][:8]}` | `{m['primary_model']}` | `{m['total_tokens']:,}` | `{m['token_share']*100:.1f}%` | `{m['cache_hit_ratio']*100:.1f}%` | `{m['total_tool_calls']}` |"
                    )
                lines.append("")

            lines.extend(
                [
                    "## 4. 热点文件与高频命令",
                    "",
                ]
            )
            for fh in data.get("file_hotspots", [])[:10]:
                lines.append(
                    f"- **文件** `{fh['basename']}` (`{fh['access_count']}` 次访问: 读 {fh['read_count']} / 写 {fh['write_count']}, `{fh['total_bytes']:,}` bytes)"
                )
            for ch in data.get("command_hotspots", [])[:8]:
                lines.append(
                    f"- **命令** `{ch['command'][:80]}` (`{ch['exec_count']}` 次, 平均耗时 `{ch['avg_duration_ms']}ms`)"
                )
            return "\n".join(lines)

        # Global overview export
        overview = self.get_global_overview(days=30, include_children=True)
        if fmt_low == "json":
            return json.dumps(overview, ensure_ascii=False, indent=2)
        if fmt_low == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(
                [
                    "conversation_id",
                    "parent_conversation_id",
                    "agent_role",
                    "title",
                    "primary_model",
                    "total_tokens",
                    "fleet_total_tokens",
                    "total_input_tokens",
                    "total_cache_read_tokens",
                    "total_output_tokens",
                    "cache_hit_ratio",
                    "peak_context_tokens",
                    "total_invocations",
                    "total_tool_calls",
                    "total_tool_errors",
                    "estimated_cost_usd",
                    "created_at",
                    "updated_at",
                ]
            )
            for c in overview.get("conversations", []):
                writer.writerow(
                    [
                        c.get("conversation_id"),
                        c.get("parent_conversation_id"),
                        c.get("agent_role"),
                        c.get("title"),
                        c.get("primary_model"),
                        c.get("total_tokens"),
                        c.get("fleet_total_tokens"),
                        c.get("total_input_tokens"),
                        c.get("total_cache_read_tokens"),
                        c.get("total_output_tokens"),
                        c.get("cache_hit_ratio"),
                        c.get("peak_context_tokens"),
                        c.get("total_invocations"),
                        c.get("total_tool_calls"),
                        c.get("total_tool_errors"),
                        c.get("estimated_cost_usd"),
                        c.get("created_at"),
                        c.get("updated_at"),
                    ]
                )
            return buf.getvalue()

        k = overview.get("kpis") or {}
        lines = [
            "# 全局 Token 用量与 AI 行为统计总览 (All Recorded Conversations)",
            "",
            "## 1. 全局核心 KPI",
            "",
            "| 指标 | 数值 |",
            "| :--- | :--- |",
            f"| **累计记录会话总数** | `{k.get('total_conversations', 0)}` (主会话 `{k.get('root_conversations', 0)}` / 子代理 `{k.get('child_conversations', 0)}`) |",
            f"| **累计总消耗 Tokens** | `{k.get('total_tokens', 0):,}` |",
            f"| **新增输入 (`input_tokens`)** | `{k.get('total_input_tokens', 0):,}` |",
            f"| **缓存命中 (`cache_read_tokens`)** | `{k.get('total_cache_read_tokens', 0):,}` |",
            f"| **模型输出 (`output_tokens`)** | `{k.get('total_output_tokens', 0):,}` |",
            f"| **平均 Prompt 缓存命中率** | `{k.get('avg_cache_hit_ratio', 0.0) * 100:.1f}%` |",
            f"| **历史最高单会话上下文峰值** | `{k.get('peak_context_tokens', 0):,}` tokens |",
            f"| **累计模型调用轮次 (`Invocations`)** | `{k.get('total_invocations', 0):,}` |",
            f"| **累计 AI 工具调用 / 报错数** | `{k.get('total_tool_calls', 0):,}` / `{k.get('total_tool_errors', 0):,}` |",
            f"| **等效 API 总成本 / 缓存累计省钱** | `${k.get('estimated_cost_usd', 0.0):.4f}` / 省 `${k.get('estimated_cache_saved_usd', 0.0):.4f}` |",
            "",
            "## 2. 模型消耗分布",
            "",
            "| 模型名称 | 调用轮次 | 总 Tokens | 缓存命中率 | 等效成本 |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for m in overview.get("model_breakdown", []):
            lines.append(
                f"| `{m['model']}` | `{m['invocations']}` | `{m['total_tokens']:,}` | `{m['cache_hit_ratio']*100:.1f}%` | `${m['cost_usd']:.4f}` |"
            )

        lines.extend(
            [
                "",
                "## 3. AI 工具调用排行榜 (Top 15)",
                "",
                "| 工具名称 | 调用次数 | 失败次数 | 平均耗时 (ms) | 累计返回字节 |",
                "| :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for t in overview.get("tool_breakdown", [])[:15]:
            lines.append(
                f"| `{t['tool_name']}` | `{t['call_count']}` | `{t['error_count']}` | `{t['avg_duration_ms']}ms` | `{t['total_result_bytes']:,}` |"
            )

        lines.extend(
            [
                "",
                "## 4. 会话列表 (Top 15)",
                "",
                "| 会话 ID | 角色 | 标题 | 自身 Tokens | 舰队总 Tokens | 缓存命中率 | 工具调用 | 更新时间 |",
                "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ]
        )
        for c in overview.get("conversations", [])[:15]:
            lines.append(
                f"| `{c['conversation_id'][:8]}` | `{c['agent_role']}` | {c['title'][:36]} | `{c['total_tokens']:,}` | `{c['fleet_total_tokens']:,}` | `{c['cache_hit_ratio']*100:.1f}%` | `{c['total_tool_calls']}` | `{c['updated_at']}` |"
            )
        return "\n".join(lines)


if __name__ == "__main__":
    engine = TrackerEngine()
    sync_info = engine.sync_all_brains()
    print(json.dumps(sync_info, ensure_ascii=False, indent=2))
