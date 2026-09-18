#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenCode bridge: OpenAI-compatible API + web UI on localhost.

Any OpenAI client (e.g. Claude Code with OPENAI_BASE_URL) talks to this
bridge, and every answer comes from the model currently selected in the
OpenCode desktop app:

  - discovers the desktop's embedded opencode server itself
    (127.0.0.1 port from TCP table, per-launch password from process env),
  - reads the ACTIVE session's model live on every request,
  - runs the prompt in a dedicated bridge session with that model,
  - returns the assistant's text as OpenAI chat completion (stream or not).

Endpoints:
    GET  /                        web UI
    GET  /health
    GET  /status                  plain text: "status api:work" or
                                  "status api:no work (error: ...)"
    GET  /v1/models               "auto" + live desktop model entry
    POST /v1/chat/completions     OpenAI chat (stream supported).
                                  Ask model "auto": answers come from the
                                  model currently selected in OpenCode.
    POST /v1/messages             Anthropic Messages API for Claude Code
                                  (stream supported). Configure Claude Code:
                                    ANTHROPIC_BASE_URL=http://127.0.0.1:8000
                                    ANTHROPIC_API_KEY=any
                                    ANTHROPIC_MODEL=auto
                                  or ~/.claude/settings.json {"env": {...}}.
    GET  /bridge/status
    GET  /bridge/sessions
    POST /bridge/active   {"id": ...}
    POST /bridge/reset            fresh bridge session
    POST /bridge/test     {"text": ...}  one-shot via temp session

Stdlib only. State file: bridge_state.json next to this script.
Run: bridge.bat (console) or start.bat (background + opens site).
"""

import base64
import ctypes
import http.client
import http.server
import json
import os
import random
import string
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "bridge_state.json")
LOG_PATH = os.path.join(BASE_DIR, "bridge.log")
BRIDGE_TITLE = "Bridge"


def log_event(event, detail=""):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("{} {} {}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"), event, str(detail)[:300]))
    except Exception:
        pass

# --------------------------------------------------------------------------
# discovery: server port (TCP table) + password (process env via PEB)
# --------------------------------------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
AF_INET = 2
MIB_TCP_STATE_LISTEN = 2

kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_size_t,
                                       ctypes.POINTER(ctypes.c_size_t)]
kernel32.ReadProcessMemory.restype = ctypes.c_bool
psapi.GetModuleFileNameExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_wchar_p, ctypes.c_ulong]
psapi.EnumProcesses.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong,
                                ctypes.POINTER(ctypes.c_ulong)]
psapi.EnumProcesses.restype = ctypes.c_bool


class _TcpRow(ctypes.Structure):
    _fields_ = [("state", ctypes.c_ulong),
                ("localAddr", ctypes.c_ulong),
                ("localPort", ctypes.c_ulong),
                ("remoteAddr", ctypes.c_ulong),
                ("remotePort", ctypes.c_ulong),
                ("owningPid", ctypes.c_ulong)]


class _PBI(ctypes.Structure):
    _fields_ = [("reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("reserved3", ctypes.c_void_p)]


ntdll.NtQueryInformationProcess.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.c_void_p, ctypes.c_ulong,
                                            ctypes.POINTER(ctypes.c_ulong)]
ntdll.NtQueryInformationProcess.restype = ctypes.c_long


def _exe_of(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        if psapi.GetModuleFileNameExW(handle, None, buf, 260):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def opencode_pids():
    arr = (ctypes.c_ulong * 4096)()
    needed = ctypes.c_ulong()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return []
    return [pid for pid in arr[:needed.value // 4]
            if pid and _exe_of(pid) == "opencode.exe"]


def _ntohs_port(net_order):
    return ((net_order & 0xFF) << 8) | ((net_order >> 8) & 0xFF)


def server_ports(wanted_pids):
    wanted = set(wanted_pids)
    size = ctypes.c_ulong(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, 5, 0)
    buf = ctypes.create_string_buffer(size.value or 65536)
    if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET, 5, 0) != 0:
        return []
    count = ctypes.c_ulong.from_buffer(buf, 0).value
    found, off = [], 4
    row_size = ctypes.sizeof(_TcpRow)
    for _ in range(count):
        row = _TcpRow.from_buffer(buf, off)
        off += row_size
        if row.state == MIB_TCP_STATE_LISTEN and row.localAddr == 0x0100007F \
                and row.owningPid in wanted:
            found.append((_ntohs_port(row.localPort), row.owningPid))
    return found


def _read_mem(handle, address, size):
    buf = ctypes.create_string_buffer(size)
    done = ctypes.c_size_t()
    if not kernel32.ReadProcessMemory(handle, ctypes.c_void_p(address),
                                      buf, size, ctypes.byref(done)):
        return None
    return buf.raw[:done.value]


def process_env(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return {}
    try:
        pbi = _PBI()
        if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(pbi),
                                           ctypes.sizeof(pbi), None) != 0:
            return {}
        raw = _read_mem(handle, pbi.PebBaseAddress + 0x20, 8)
        if not raw:
            return {}
        params = int.from_bytes(raw, "little")
        raw = _read_mem(handle, params + 0x80, 8)
        if not raw:
            return {}
        env_addr = int.from_bytes(raw, "little")
        chunks, total = [], 0
        while total < 262144:
            raw = _read_mem(handle, env_addr + total, 32768)
            if not raw:
                break
            chunks.append(raw)
            total += len(raw)
            if b"\x00\x00" in raw:
                break
        env = {}
        for entry in b"".join(chunks).decode("utf-16-le", errors="ignore").split("\x00"):
            if "=" in entry:
                key, _, value = entry.partition("=")
                if key and key not in env:
                    env[key] = value
        return env
    finally:
        kernel32.CloseHandle(handle)


def find_server_password(pids):
    for pid in pids:
        try:
            password = process_env(pid).get("OPENCODE_SERVER_PASSWORD", "")
        except Exception:
            continue
        if password:
            return password, pid
    return "", None


# --------------------------------------------------------------------------
# opencode server client (discovered server, Basic auth)
# --------------------------------------------------------------------------

class Upstream:
    def __init__(self):
        self.lock = threading.Lock()
        self.base_url = ""
        self.auth = ""

    def discover(self):
        pids = opencode_pids()
        if not pids:
            return "OpenCode.exe process not found"
        candidates = server_ports(pids)
        if not candidates:
            return "no 127.0.0.1 listener owned by OpenCode.exe"
        port, owner = candidates[0]
        password, _ = find_server_password([owner])
        if not password:
            password, _ = find_server_password(pids)
        if not password:
            return "OPENCODE_SERVER_PASSWORD not found in process env"
        token = base64.b64encode(("opencode:" + password).encode()).decode()
        with self.lock:
            self.base_url = "http://127.0.0.1:{}".format(port)
            self.auth = "Basic " + token
        return ""

    def _call(self, method, path, payload=None, timeout=30):
        with self.lock:
            base, auth = self.base_url, self.auth
        if not base:
            set_health(False, "not discovered")
            return 0, "not discovered"
        data, headers = None, {"Authorization": auth}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                set_health(True)
                return resp.status, resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                err = self.discover()  # password/port may have rotated; retry once
                if not err:
                    return self._call(method, path, payload, timeout)
                set_health(False, "unauthorized")
                return exc.code, ""
            if 500 <= exc.code < 600:
                set_health(False, "upstream HTTP {}".format(exc.code))
            else:
                set_health(True)
            try:
                return exc.code, exc.read().decode("utf-8", errors="ignore")
            except Exception:
                return exc.code, ""
        except Exception as exc:
            # network-level failure (TLS disconnect, refused, timeout, ...)
            set_health(False, str(exc) or type(exc).__name__)
            return 0, str(exc)

    def get(self, path, timeout=30):
        return self._call("GET", path, None, timeout)

    def post(self, path, payload, timeout=30):
        return self._call("POST", path, payload, timeout)

    def delete(self, path, timeout=30):
        return self._call("DELETE", path, None, timeout)


upstream = Upstream()


# --------------------------------------------------------------------------
# health tracking: what the status page and /status show
# --------------------------------------------------------------------------

health = {"ok": False, "error": "not checked yet", "at": 0}
health_lock = threading.Lock()


def set_health(ok, error=""):
    with health_lock:
        health["ok"] = bool(ok)
        health["error"] = "" if ok else (error or "unknown error")
        health["at"] = int(time.time())


def health_snapshot():
    with health_lock:
        return dict(health)


def health_prober(interval=30):
    """Background ping so /status stays fresh even when idle."""
    while True:
        time.sleep(interval)
        try:
            status, _ = upstream.get("/api/session", timeout=15)
            if status == 200:
                set_health(True)
            elif status == 401:
                set_health(False, "unauthorized")
            elif status != 0:
                set_health(True)  # reachable; errors are per-call business
        except Exception as exc:
            set_health(False, str(exc))


def api_list_sessions():
    status, body = upstream.get("/api/session")
    if status != 200:
        return None
    try:
        return json.loads(body).get("data", [])
    except Exception:
        return None


def api_session_info(session_id):
    status, body = upstream.get("/api/session/{}".format(session_id))
    if status != 200:
        return None
    try:
        return json.loads(body).get("data")
    except Exception:
        return None


def session_model_ref(session_id):
    """Live model of a session as {providerID, modelID} override (or {})."""
    info = api_session_info(session_id)
    model = (info or {}).get("model") or {}
    if model.get("providerID") and model.get("id"):
        return {"providerID": model["providerID"], "modelID": model["id"]}, model
    return {}, model


def session_sort_key(item):
    time_info = item.get("time", {}) or {}
    return (time_info.get("updated", 0) or 0, time_info.get("created", 0) or 0,
            item.get("id", ""))


# --------------------------------------------------------------------------
# bridge state + history handling
# --------------------------------------------------------------------------

state = {"active_session_id": "", "forced_model": "", "seen": [], "seen_for": ""}


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        state["active_session_id"] = data.get("active_session_id", "") or ""
        state["forced_model"] = data.get("forced_model", "") or ""
        # compat: old files had bridge_session_id
        state["seen"] = data.get("seen", []) or []
        state["seen_for"] = data.get("seen_for", "") or ""
    except Exception:
        pass


def save_state():
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"active_session_id": state["active_session_id"],
                       "forced_model": state.get("forced_model", ""),
                       "seen": state["seen"][-50:],
                       "seen_for": state["seen_for"]}, fh)
    except Exception:
        pass


def pick_active_session(sessions):
    """Session currently open in the desktop (saved choice, else latest).

    Requests now go DIRECTLY into this session — no separate Bridge
    session — so messages and file edits land in the folder you have
    open right now.
    """
    if not sessions:
        return ""
    if state["active_session_id"]:
        if any(s.get("id") == state["active_session_id"] for s in sessions):
            return state["active_session_id"]
        state["active_session_id"] = ""
    return sorted(sessions, key=session_sort_key)[-1]["id"]


def poll_answer(sid, user_msg_id, timeout=180):
    """Wait for the assistant reply linked to our user message.

    Backup for empty direct responses. Matches strictly by parentID so we
    never return somebody else's text. Falls back to reasoning text.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = upstream.get("/session/{}/message?limit=30".format(sid),
                                    timeout=30)
        if status == 200:
            try:
                items = json.loads(body)
            except Exception:
                items = []
            if isinstance(items, dict):
                items = items.get("data", [])
            for m in items or []:
                if not isinstance(m, dict):
                    continue
                info = m.get("info", {}) or {}
                if info.get("role") != "assistant":
                    continue
                if info.get("parentID") != user_msg_id:
                    continue
                parts = m.get("parts", []) or []
                text = "".join((p.get("text", "") or "") for p in parts
                               if isinstance(p, dict) and p.get("type") == "text")
                if text.strip():
                    return text.strip()
                # thinking-only so far — return reasoning as fallback
                reasoning = "".join((p.get("text", "") or "") for p in parts
                                    if isinstance(p, dict) and p.get("type") == "reasoning")
                if reasoning.strip():
                    return reasoning.strip()
        time.sleep(3)
    log_event("poll-timeout", "sid={} mid={}".format(sid, user_msg_id))
    return ""


def normalize_messages(messages):
    """OpenAI messages -> [(role, text)]. Images dropped, tool calls labeled."""
    out = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        if role == "system":
            content = message.get("content", "")
            out.append(("system", content if isinstance(content, str) else ""))
            continue
        if role == "tool":
            content = message.get("content", "")
            out.append(("tool", "[Tool {}]\n{}".format(
                message.get("name", "tool"),
                content if isinstance(content, str) else "")))
            continue
        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            function = (tool_calls[0].get("function", {}) or {})
            out.append(("assistant", "[tool_call {} {}]".format(
                function.get("name", "?"), function.get("arguments", "{}"))))
            continue
        content = message.get("content", "")
        if isinstance(content, list):
            text = "".join(p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text")
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        out.append(("assistant" if role == "assistant" else "user", text))
    return out


def anthropic_to_normalized(data):
    """Anthropic Messages API request -> (system_str, [(role, text)]).

    Tool definitions can't execute client-side here (the opencode agent runs
    its own tools server-side), so tool_use/tool_result blocks become labeled
    text and tool schemas are summarized for context.
    """
    system_parts = []

    def block_text(block):
        if not isinstance(block, dict):
            return ""
        btype = block.get("type", "")
        if btype == "text":
            return block.get("text", "") or ""
        if btype == "image":
            return "[image omitted]"
        if btype == "tool_use":
            return "[tool_call {} {}]".format(
                block.get("name", "?"), json.dumps(block.get("input", {}),
                                                   ensure_ascii=False))
        if btype == "tool_result":
            content = block.get("content", "")
            if isinstance(content, list):
                content = "".join(block_text(p) for p in content)
            return "[tool result]\n{}".format(content or "")
        return ""

    system = data.get("system", "")
    if isinstance(system, list):
        system = "".join(block_text(b) for b in system)
    if isinstance(system, str) and system.strip():
        system_parts.append(system.strip())

    tools = data.get("tools", []) or []
    if tools:
        names = [t.get("name", "?") for t in tools if isinstance(t, dict)]
        system_parts.append(
            "The client offered these tools: {}. You cannot call them "
            "directly — execute the task with your own tools and return "
            "the final result as text.".format(", ".join(names)))

    norm = []
    for message in data.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, list):
            text = "".join(block_text(b) for b in content)
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        norm.append(("assistant" if role == "assistant" else "user", text))
    return "\n\n".join(system_parts), norm


def build_send_text(new_items, full_fallback):
    """New user-bound text. Labeled in fallback mode so nothing is lost."""
    if full_fallback:
        chunks = []
        for role, text in new_items:
            if role == "system" or not text.strip():
                continue
            chunks.append("[{}]\n{}".format(role, text) if role != "user" else text)
        return "\n\n".join(chunks).strip()
    chunks = [text for role, text in new_items
              if role in ("user", "tool") and text.strip()]
    return "\n\n".join(chunks).strip()


def make_message_id():
    return "msg_" + "".join(random.choice(string.ascii_letters + string.digits)
                            for _ in range(26))


def run_turn(active_sid, messages):
    """Forward one request DIRECTLY into the currently open session.

    No separate Bridge session — prompt lands in the folder you have open,
    so its tool calls and file edits land there too.
    """
    norm = normalize_messages(messages)
    # seen is per-session: switching folders resets the diff window
    if state["seen_for"] != active_sid:
        state["seen"] = []
        state["seen_for"] = active_sid
    seen = state["seen"]
    if norm[:len(seen)] == seen and len(norm) > len(seen):
        send_text = build_send_text(norm[len(seen):], False)
    else:
        send_text = build_send_text(norm, True)
    if not send_text:
        return False, "no new user text to send"
    system = "\n\n".join(text for role, text in norm
                          if role == "system" and text.strip())
    mid = make_message_id()
    payload = {"messageID": mid, "parts": [{"type": "text", "text": send_text}]}
    if system:
        payload["system"] = system
    # Модель: если выбрана на сайте — форсируем её, иначе не ставим
    # (сервер возьмёт ту что внизу Build · ... в опенкоде).
    forced = state.get("forced_model", "")
    if forced and forced not in ("auto", ""):
        try:
            provider, _, model_id = forced.partition("/")
            if provider and model_id:
                payload["model"] = {"providerID": provider, "modelID": model_id}
            else:
                payload["model"] = {"providerID": "opencode", "modelID": forced}
        except Exception:
            pass
    started = time.time()
    status, body = upstream.post("/session/{}/message".format(active_sid),
                                 payload, timeout=600)
    if status == 0 and time.time() - started < 10:
        log_event("retry", body[:150])
        time.sleep(2)
        status, body = upstream.post("/session/{}/message".format(active_sid),
                                     payload, timeout=600)
    if status != 200:
        log_event("turn-fail", "{} {} sid={}".format(status, body[:200], active_sid))
        return False, "upstream {} {}".format(status, body[:300])
    try:
        data = json.loads(body)
    except Exception:
        return False, "bad upstream response"
    texts = [p.get("text", "") or "" for p in data.get("parts", []) or []
             if isinstance(p, dict) and p.get("type") == "text"]
    state["seen"] = norm
    state["seen_for"] = active_sid
    save_state()
    answer = "".join(texts).strip()
    if not answer:
        log_event("turn-empty-polling", "sid={}".format(active_sid))
        answer = poll_answer(active_sid, mid, timeout=180)
    if not answer:
        log_event("turn-empty", "sid={} model={}".format(
            active_sid, payload.get("model", {})))
        return False, "upstream returned no answer — model is thinking or unavailable, try again or switch model"
    return True, answer


# --------------------------------------------------------------------------
# HTTP: OpenAI routes + bridge UI api + static index.html
# --------------------------------------------------------------------------

def _dump(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def make_chunk(request_id, model, delta, finish_reason=None):
    return {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def live_model_entry():
    """Current desktop-selected model as OpenAI model id (or default)."""
    sessions = api_list_sessions() or []
    active = pick_active_session(sessions)
    if not active:
        return "opencode-chat", {}, ""
    override, model = session_model_ref(active)
    if override:
        return "{}/{}".format(override["providerID"], override["modelID"]), override, active
    return "opencode-chat", {}, active


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Bridge/1.0"

    def log_message(self, *args):
        pass

    def _send(self, body, status=200, content_type="application/json"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, obj, status=200):
        self._send(_dump(obj), status)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if not length:
            self._raw_body = b""
            return {}
        try:
            raw = self.rfile.read(length)
            self._raw_body = raw
            return json.loads(raw.decode("utf-8"))
        except Exception:
            self._raw_body = b""
            return None

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            try:
                with open(os.path.join(BASE_DIR, "index.html"), "rb") as fh:
                    self._send(fh.read(), 200, "text/html; charset=utf-8")
            except Exception:
                self._send_json({"error": "index.html missing"}, 500)
        elif path == "/health":
            self._send_json({"status": "ok"})
        elif path == "/status":
            # Plain-text status page: "status api:work" or the error.
            snap = health_snapshot()
            if snap["ok"]:
                self._send("status api:work", 200, "text/plain; charset=utf-8")
            else:
                self._send("status api:no work (error: {})".format(
                    snap["error"] or "unknown"), 200, "text/plain; charset=utf-8")
        elif path == "/v1/models":
            # "auto" is the main id: answers always come from the model
            # currently selected in OpenCode. The live id is listed too and
            # also accepted explicitly. Extra Anthropic fields included so
            # both OpenAI and Anthropic clients parse the same response.
            model_id, _, _ = live_model_entry()
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            def _entry(mid, label):
                return {"id": mid, "object": "model", "created": 0,
                        "owned_by": "opencode", "display_name": label,
                        "type": "model", "created_at": now_iso}

            entries = [_entry("OpenCode", "OpenCode"),
                       _entry("OpenCode[1m]", "OpenCode[1m]"),
                       _entry("auto", "auto")]
            if model_id not in ("opencode-chat", ""):
                entries.append(_entry(model_id, model_id))
            self._send_json({"object": "list", "data": entries})
        elif path == "/bridge/models":
            # catalogue for the site dropdown — только OpenCode Zen (как на скрине),
            # т.е. provider opencode + бесплатно. Так список 7 штук, а не 135.
            status, body = upstream.get("/api/model", timeout=15)
            if status != 200:
                self._send_json({"models": []})
                return
            try:
                items = json.loads(body)
                if isinstance(items, dict):
                    items = items.get("data", [])
            except Exception:
                items = []
            out = []
            for m in items or []:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                if m.get("providerID") != "opencode":
                    continue
                # free check: cost [{input:0, output:0}]
                cost = m.get("cost", [])
                tiers = cost if isinstance(cost, list) else [cost]
                is_free = False
                if tiers:
                    try:
                        is_free = all(float(t.get("input", 1) or 0) == 0 and float(t.get("output", 1) or 0) == 0
                                      for t in tiers if isinstance(t, dict))
                    except Exception:
                        is_free = False
                if not is_free:
                    continue
                out.append({"id": m.get("id", ""), "name": m.get("name", m.get("id", "")),
                            "providerID": m.get("providerID", "")})
            out.sort(key=lambda x: x["name"].lower())
            self._send_json({"models": out})
        elif path == "/bridge/status":
            sessions = api_list_sessions()
            active = pick_active_session(sessions or []) if sessions else ""
            info, model = {}, {}
            if active:
                _, model = session_model_ref(active)
                for s in sessions or []:
                    if s.get("id") == active:
                        info = {"id": active, "title": s.get("title", ""),
                                "directory": s.get("directory", "")}
                        break
            snap = health_snapshot()
            self._send_json({
                "server_url": upstream.base_url, "server_ok": sessions is not None,
                "api": "work" if snap["ok"] else "no work",
                "api_error": snap["error"],
                "active_session": info, "active_model": model,
                "forced_model": state.get("forced_model", ""),
                "base_url": "http://127.0.0.1:{}/v1".format(BRIDGE_PORT),
            })
        elif path == "/bridge/sessions":
            sessions = api_list_sessions() or []
            active = pick_active_session(sessions)
            out = []
            for s in sorted(sessions, key=session_sort_key, reverse=True)[:50]:
                _, model = session_model_ref(s.get("id", ""))
                out.append({"id": s.get("id", ""), "title": s.get("title", ""),
                            "directory": s.get("directory", ""),
                            "model": model,
                            "updated": ((s.get("time", {}) or {}).get("updated", 0)),
                            "active": s.get("id") == active})
            self._send_json({"sessions": out})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        data = self._read_json()
        if data is None:
            self._send_json({"error": "invalid json"}, 400)
            return
        try:
            if path == "/v1/chat/completions":
                self.handle_chat(data)
            elif path == "/v1/messages":
                self.handle_anthropic(data)
            elif path == "/bridge/model":
                # {model: "opencode/big-pickle"} or {"model": "auto"} — форсирует модель для следующих запросов
                model = (data.get("model", "") or "").strip()
                state["forced_model"] = model
                # также сразу обновляем модель активной сессии в опенкоде
                if model and model != "auto" and state.get("active_session_id"):
                    try:
                        provider, _, model_id = model.partition("/")
                        if not model_id:
                            provider, model_id = "opencode", model
                        else:
                            if "/" not in model:
                                provider, model_id = "opencode", model
                        upstream.post("/api/session/{}/model".format(state["active_session_id"]),
                                      {"providerID": provider, "modelID": model_id}, timeout=15)
                    except Exception:
                        pass
                save_state()
                self._send_json({"ok": True, "forced_model": state["forced_model"]})
            elif path == "/bridge/active":
                sessions = api_list_sessions() or []
                if any(s.get("id") == data.get("id") for s in sessions):
                    state["active_session_id"] = data.get("id")
                    save_state()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "unknown session"}, 404)
            elif path == "/bridge/reset":
                state["seen"] = []
                state["seen_for"] = ""
                save_state()
                self._send_json({"ok": True})
            elif path == "/bridge/test":
                text = (data.get("text", "") or "").strip()
                if not text:
                    self._send_json({"error": "empty text"}, 400)
                    return
                sessions = api_list_sessions()
                if sessions is None:
                    self._send_json({"error": "upstream unreachable"}, 502)
                    return
                active = pick_active_session(sessions)
                if not active:
                    self._send_json({"error": "no session"}, 502)
                    return
                ok, answer = run_turn(active, [{"role": "user", "content": text}])
                self._send_json({"answer": answer}, 200 if ok else 502)
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            return
        except Exception:
            import traceback
    def handle_anthropic(self, data):
        """POST /v1/messages — Anthropic Messages API for Claude Code.

        Accepts model/max_tokens/system/messages/tools (+stream), runs the
        prompt on the desktop-selected model via run_turn(), and answers in
        Anthropic format. The opencode agent executes tools server-side, so
        the reply is always final text (no tool_use round-trips).
        """
        requested = data.get("model", "auto") or "auto"
        # Any requested name (OpenCode / auto / anything else) is answered
        # by the model currently selected in OpenCode.
        system, norm = anthropic_to_normalized(data)
        sessions = api_list_sessions()
        if sessions is None:
            self._send_json({"type": "error",
                             "error": {"type": "api_error",
                                       "message": "upstream unreachable"}}, 502)
            return
        active = pick_active_session(sessions)
        if not active:
            self._send_json({"type": "error",
                             "error": {"type": "api_error",
                                       "message": "no session in desktop"}}, 502)
            return
        history = [{"role": r, "content": t} for r, t in norm]
        if system:
            history = [{"role": "system", "content": system}] + history
        ok, answer = run_turn(active, history)
        if not ok:
            self._send_json({"type": "error",
                             "error": {"type": "api_error", "message": answer}}, 502)
            return
        msg_id = "msg_" + str(uuid.uuid4())[:16]
        if data.get("stream"):
            chunks = []

            def ev(name, payload):
                chunks.append("event: {}\ndata: {}\n\n".format(name, _dump(payload)))

            ev("message_start", {"type": "message_start",
                                 "message": {"id": msg_id, "type": "message",
                                             "role": "assistant", "model": requested,
                                             "content": [], "stop_reason": None,
                                             "stop_sequence": None,
                                             "usage": {"input_tokens": 0,
                                                       "output_tokens": 0}}})
            ev("content_block_start", {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "text", "text": ""}})
            for offset in range(0, len(answer), 64):
                ev("content_block_delta",
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta",
                              "text": answer[offset:offset + 64]}})
            ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            ev("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": "end_turn",
                                           "stop_sequence": None},
                                 "usage": {"output_tokens": 0}})
            ev("message_stop", {"type": "message_stop"})
            self._send("".join(chunks).encode("utf-8"), 200, "text/event-stream")
            return
        self._send_json({
            "id": msg_id, "type": "message", "role": "assistant",
            "model": requested,
            "content": [{"type": "text", "text": answer}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })

    def handle_chat(self, data):
        messages = data.get("messages", []) or []
        # The answer always comes from the model currently selected in
        # OpenCode (run_turn reads it live per request). The response reports
        # that live id so clients see what really answered.
        model_id, _, _ = live_model_entry()
        model = model_id
        sessions = api_list_sessions()
        if sessions is None:
            self._send_json({"error": {"message": "upstream unreachable",
                                       "type": "upstream_error"}}, 502)
            return
        active = pick_active_session(sessions)
        if not active:
            self._send_json({"error": {"message": "no session in desktop",
                                       "type": "upstream_error"}}, 502)
            return
        ok, answer = run_turn(active, messages)
        request_id = "chatcmpl-" + str(uuid.uuid4())[:16]
        created = int(time.time())
        if not ok:
            self._send_json({"error": {"message": answer,
                                       "type": "upstream_error"}}, 502)
            return
        if data.get("stream"):
            events = [({"role": "assistant"}, None)]
            for offset in range(0, len(answer), 64):
                events.append(({"content": answer[offset:offset + 64]}, None))
            events.append(({}, "stop"))
            body = "".join("data: " + _dump(make_chunk(request_id, model, delta, fin))
                            + "\n\n" for delta, fin in events) + "data: [DONE]\n\n"
            self._send(body.encode("utf-8"), 200, "text/event-stream")
            return
        self._send_json({
            "id": request_id, "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })


BRIDGE_PORT = 8000


def main(argv):
    global BRIDGE_PORT
    port = 8000
    env_port = os.environ.get("BRIDGE_PORT", "")
    if env_port:
        try:
            port = int(env_port) & 0xFFFF
        except ValueError:
            pass
    if len(argv) >= 2 and argv[1].isdigit():
        port = int(argv[1]) & 0xFFFF
    BRIDGE_PORT = port
    load_state()
    error = upstream.discover()
    if error:
        print("upstream discovery failed: " + error, file=sys.stderr)
        return 1
    print("upstream: " + upstream.base_url, flush=True)
    sessions = api_list_sessions()
    if sessions is None:
        print("upstream unreachable", file=sys.stderr)
        return 1
    active = pick_active_session(sessions)
    print("active session: " + (active or "(none)") + " (requests go here)", flush=True)
    set_health(True)
    print("listening: http://127.0.0.1:{}/  api: http://127.0.0.1:{}/v1".format(
        port, port), flush=True)
    try:
        with open(os.path.join(BASE_DIR, "bridge.pid"), "w") as fh:
            fh.write(str(os.getpid()))
    except Exception:
        pass
    prober = threading.Thread(target=health_prober, kwargs={"interval": 30},
                              daemon=True)
    prober.start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(os.path.join(BASE_DIR, "bridge.pid"))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv))
