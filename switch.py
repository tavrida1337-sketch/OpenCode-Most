#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Switch Claude Code between agentrouter and the local OpenCode bridge.

Usage:
    python switch.py agentrouter   - Claude Code -> agentrouter.org
    python switch.py opencode      - Claude Code -> local bridge (OpenCode model)

Only rewrites the env block of ~/.claude/settings.json (backup to
settings.json.bak first). Everything else in the file is preserved.
No tokens are stored here: the agentrouter key is read from the current
settings file and kept as-is.
"""

import json
import os
import sys
import urllib.request

SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

OPENCODE_ENV = {
    # NOTE: ANTHROPIC_AUTH_TOKEN is deliberately NOT touched here so the
    # real agentrouter key survives the round-trip (the bridge ignores auth).
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
    "ANTHROPIC_MODEL": "OpenCode[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "OpenCode[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "OpenCode[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "OpenCode[1m]",
    "ANTHROPIC_CUSTOM_MODEL_OPTION": "OpenCode[1m]",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "OpenCode",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "OpenCode Zen via local bridge (1M context)",
}

AGENTROUTER_ENV = {
    "ANTHROPIC_BASE_URL": "https://agentrouter.org",
    "ANTHROPIC_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-opus-4-8",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
}

MANAGED_KEYS = list(OPENCODE_ENV) + ["ANTHROPIC_API_KEY"]


def bridge_up():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/status", timeout=5) as resp:
            return "work" in resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return False


def main(argv):
    if len(argv) < 2 or argv[1] not in ("agentrouter", "opencode"):
        print("usage: switch.py agentrouter|opencode")
        return 1
    profile = argv[1]
    try:
        with open(SETTINGS, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        print("cannot read settings.json: {}".format(exc))
        return 1

    try:
        with open(SETTINGS + ".bak", "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print("cannot write backup: {}".format(exc))
        return 1

    env = data.get("env", {}) or {}
    if not isinstance(env, dict):
        env = {}
    for key in MANAGED_KEYS:
        env.pop(key, None)

    if profile == "agentrouter":
        if not env.get("ANTHROPIC_AUTH_TOKEN", ""):
            print("no ANTHROPIC_AUTH_TOKEN in settings.json - "
                  "put your agentrouter key there first")
            return 1
        env.update(AGENTROUTER_ENV)
        # token stays untouched
        tail = ("Claude Code -> agentrouter (opus-5 / opus-4-8 / deepseek)\n"
                "restart Claude Code to apply")
    else:
        env.update(OPENCODE_ENV)
        tail = None  # printed after write (needs bridge check)

    try:
        data["env"] = env
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print("cannot write settings.json: {}".format(exc))
        return 1

    if tail is None:
        print("Claude Code -> OpenCode bridge (model OpenCode[1m])")
        print("bridge: {}".format("api work" if bridge_up() else
                                  "DOWN - run start.bat first"))
        print("restart Claude Code to apply")
    else:
        print(tail)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv))
