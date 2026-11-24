#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Deploy script for Token & AI Behavior Tracker plugin.

1. Copies the plugin bundle from `d:\antigravity_workplace\token-behavior-tracker`
   to `C:\Users\baiyu\.gemini\config\plugins\token-behavior-tracker`.
2. Triggers an initial `TrackerEngine.sync_all_brains()` so the SQLite WAL
   database is pre-populated with all existing conversation transcripts.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent
DEFAULT_TARGET_DIR = Path.home() / ".gemini" / "config" / "plugins" / "token-behavior-tracker"
DEFAULT_WORKSPACE_PLUGIN_DIR = SOURCE_DIR.parent / ".agents" / "plugins" / "token-behavior-tracker"
DEFAULT_DATA_DIR = Path.home() / ".gemini" / "antigravity" / "token-behavior-tracker" / "data"
FALLBACK_DATA_DIR = SOURCE_DIR / ".data"

BUNDLE_ITEMS = [
    "plugin.json",
    "mcp_config.json",
    "hooks.json",
    "README.md",
    "assets",
    "bin",
    "skills",
    "ui",
]


def _copy_bundle(dst_dir: Path) -> list[str]:
    dst_dir = dst_dir.resolve()
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for item_name in BUNDLE_ITEMS:
        src_path = SOURCE_DIR / item_name
        if not src_path.exists():
            continue
        dst_path = dst_dir / item_name
        if src_path.is_dir():
            if dst_path.exists():
                shutil.rmtree(dst_path)
            shutil.copytree(
                src_path,
                dst_path,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
        copied.append(item_name)
    return copied


def deploy_plugin(target_dir: Path, data_dir: Path, run_sync: bool = True) -> dict:
    """Copy plugin files to target_dir (and workspace .agents/plugins fallback) and optionally run initial brain sync."""
    installed_targets: list[str] = []
    copied: list[str] = []

    try:
        copied = _copy_bundle(target_dir)
        installed_targets.append(str(target_dir.resolve()))
    except PermissionError:
        pass

    # Also deploy to workspace .agents/plugins/token-behavior-tracker so workspace discovery works automatically
    try:
        ws_copied = _copy_bundle(DEFAULT_WORKSPACE_PLUGIN_DIR)
        if not copied:
            copied = ws_copied
        ws_str = str(DEFAULT_WORKSPACE_PLUGIN_DIR.resolve())
        if ws_str not in installed_targets:
            installed_targets.append(ws_str)
    except Exception:
        pass

    active_target = Path(installed_targets[0]) if installed_targets else SOURCE_DIR
    resolved_data_dir = data_dir
    sync_result = None
    if run_sync:
        bin_dir = active_target / "bin"
        if str(bin_dir) not in sys.path:
            sys.path.insert(0, str(bin_dir))
        try:
            from engine import TrackerEngine  # type: ignore

            try:
                resolved_data_dir.mkdir(parents=True, exist_ok=True)
                engine = TrackerEngine(data_dir=resolved_data_dir)
            except PermissionError:
                resolved_data_dir = FALLBACK_DATA_DIR
                resolved_data_dir.mkdir(parents=True, exist_ok=True)
                engine = TrackerEngine(data_dir=resolved_data_dir)
            sync_result = engine.sync_all_brains(force=False)
        except Exception as exc:
            sync_result = {"error": str(exc)}

    return {
        "source_dir": str(SOURCE_DIR),
        "target_dir": str(active_target),
        "installed_targets": installed_targets,
        "data_dir": str(resolved_data_dir),
        "copied_items": copied,
        "sync_result": sync_result,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy Token & AI Behavior Tracker plugin")
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=DEFAULT_TARGET_DIR,
        help="Target plugin directory (default: ~/.gemini/config/plugins/token-behavior-tracker)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("PLUGIN_DATA", str(DEFAULT_DATA_DIR))),
        help="Persistent data directory for SQLite DB",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip initial sync_all_brains() database population",
    )
    args = parser.parse_args()

    result = deploy_plugin(
        target_dir=args.target_dir,
        data_dir=args.data_dir,
        run_sync=not args.no_sync,
    )
    print(f"[deploy] Installed plugin to: {result['target_dir']}")
    print(f"[deploy] Copied items: {', '.join(result['copied_items'])}")
    if result["sync_result"] is not None:
        print(f"[deploy] Initial brain sync result: {result['sync_result']}")


if __name__ == "__main__":
    main()
