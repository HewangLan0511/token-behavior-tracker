#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lifecycle Hook Collector for the Antigravity Token & AI Behavior Tracker plugin.
Reads hook JSON from stdin, folds the latest transcript lines into SQLite,
and emits an optional non-intrusive user_markdown warning on PostInvocation
when context tokens cross the configured context_warn_threshold.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    try:
        raw_input = sys.stdin.read()
        if not raw_input or not raw_input.strip():
            sys.stdout.write("{}\n")
            sys.stdout.flush()
            return

        payload = json.loads(raw_input)
        if not isinstance(payload, dict):
            sys.stdout.write("{}\n")
            sys.stdout.flush()
            return

        from engine import TrackerEngine

        engine = TrackerEngine()
        result = engine.record_hook_event(payload)

        hook_event = str(payload.get("hookEventName", "") or "")
        if hook_event == "PostInvocation" and result.get("exceeded_warn_threshold"):
            latest_ctx = int(result.get("latest_context_tokens", 0) or 0)
            warn_threshold = int(result.get("context_warn_threshold", 120000) or 120000)
            banner = (
                f"⚠️ **上下文水位提醒**：当前会话上下文已达 **{latest_ctx:,} tokens**"
                f"（超过预警线 {warn_threshold:,} tokens），建议使用 `/compact` 压缩上下文以保持最佳响应速度与缓存效率。"
            )
            response = {
                "injectSteps": [
                    {
                        "systemMessage": {
                            "systemMessage": "",
                            "metadata": {
                                "user_markdown": banner,
                            },
                        }
                    }
                ],
                "terminationBehavior": "",
            }
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return

        sys.stdout.write("{}\n")
        sys.stdout.flush()
    except Exception:
        # Never block the agent loop on a hook error
        sys.stdout.write("{}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
