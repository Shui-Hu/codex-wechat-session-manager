# SPDX-License-Identifier: AGPL-3.0-only
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex WeChat bridge runtime.

Adapted from BigPizzaV3/CodexPlusPlus tools/codex-wechat/codex_wechat.py.
Local GUI integration, persistent session safeguards and file transfer changes: 2026-10-07.
See NOTICE.md and LICENSE for provenance and redistribution terms.

This is a small iLink long-poll bridge:
WeChat iLink -> codex app-server/exec -> WeChat iLink.
"""

from __future__ import annotations

import argparse
import base64
import atexit
import hashlib
import json
import os
import queue
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.1.3"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (1 << 8) | 3)

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
ILINK_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
LOG_FILE: Path | None = None


@dataclass
class CodexConfig:
    backend: str = "app-server"
    command: str = "codex"
    app_server_command: str = ""
    workspace: str = "."
    model: str = ""
    profile: str = ""
    sandbox: str = "read-only"
    approval_policy: str = "never"
    timeout_seconds: int = 300
    additional_directories: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    prompt_prefix: str = "回复使用中文，格式适合微信阅读。"


@dataclass
class BridgeConfig:
    base_url: str = ILINK_BASE_URL
    token: str = ""
    account_id: str = ""
    thread_id: str = ""
    allow_all: bool = False
    allow_user_ids: set[str] = field(default_factory=set)
    require_prefix: bool = True
    prefix: str = "/codex"
    allow_id_command_for_unauthorized: bool = True
    poll_timeout_seconds: int = 35
    retry_delay_seconds: int = 2
    backoff_delay_seconds: int = 30
    max_consecutive_failures: int = 3
    max_reply_chars: int = 1800
    max_upload_bytes: int = 80 * 1024 * 1024
    max_download_bytes: int = 100 * 1024 * 1024
    download_dir: str = ""
    enable_typing: bool = True
    state_dir: Path = field(
        default_factory=lambda: Path.home() / ".codex-wechat-session-manager" / "state"
    )
    codex: CodexConfig = field(default_factory=CodexConfig)


def log(message: str) -> None:
    line = f"{time.strftime('[%Y-%m-%d %H:%M:%S]')} {message}"
    print(line, flush=True)
    if LOG_FILE:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8-sig") as f:
                f.write(line + "\n")
        except Exception:
            pass


def load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def load_config(path: Path) -> BridgeConfig:
    raw = load_json_file(path)
    codex_raw = raw.get("codex") or {}
    if not isinstance(codex_raw, dict):
        raise ValueError("codex config must be an object")

    cfg = BridgeConfig(
        base_url=str(raw.get("base_url") or ILINK_BASE_URL),
        token=str(raw.get("token") or ""),
        account_id=str(raw.get("account_id") or ""),
        thread_id=str(raw.get("thread_id") or ""),
        allow_all=bool(raw.get("allow_all", False)),
        allow_user_ids=set(str(x) for x in raw.get("allow_user_ids") or []),
        require_prefix=bool(raw.get("require_prefix", True)),
        prefix=str(raw.get("prefix") or "/codex"),
        allow_id_command_for_unauthorized=bool(
            raw.get("allow_id_command_for_unauthorized", True)
        ),
        poll_timeout_seconds=int(raw.get("poll_timeout_seconds", 35)),
        retry_delay_seconds=int(raw.get("retry_delay_seconds", 2)),
        backoff_delay_seconds=int(raw.get("backoff_delay_seconds", 30)),
        max_consecutive_failures=int(raw.get("max_consecutive_failures", 3)),
        max_reply_chars=int(raw.get("max_reply_chars", 1800)),
        max_upload_bytes=int(raw.get("max_upload_bytes", 80 * 1024 * 1024)),
        max_download_bytes=int(raw.get("max_download_bytes", 100 * 1024 * 1024)),
        download_dir=str(raw.get("download_dir") or ""),
        enable_typing=bool(raw.get("enable_typing", True)),
        state_dir=Path(str(raw.get("state_dir") or "")) if raw.get("state_dir") else Path.home() / ".codex-wechat-session-manager" / "state",
        codex=CodexConfig(
            backend=str(codex_raw.get("backend") or "app-server"),
            command=str(codex_raw.get("command") or "codex"),
            app_server_command=str(codex_raw.get("app_server_command") or ""),
            workspace=str(codex_raw.get("workspace") or "."),
            model=str(codex_raw.get("model") or ""),
            profile=str(codex_raw.get("profile") or ""),
            sandbox=str(codex_raw.get("sandbox") or "read-only"),
            approval_policy=str(codex_raw.get("approval_policy") or "never"),
            timeout_seconds=int(codex_raw.get("timeout_seconds", 300)),
            additional_directories=[str(x) for x in codex_raw.get("additional_directories") or []],
            extra_args=[str(x) for x in codex_raw.get("extra_args") or []],
            prompt_prefix=str(codex_raw.get("prompt_prefix") or CodexConfig().prompt_prefix),
        ),
    )

    cfg.token = os.environ.get("WEIXIN_TOKEN") or os.environ.get("ILINK_TOKEN") or cfg.token
    cfg.base_url = os.environ.get("WEIXIN_BASE_URL") or os.environ.get("ILINK_BASE_URL") or cfg.base_url
    cfg.account_id = os.environ.get("WEIXIN_ACCOUNT_ID") or cfg.account_id

    return cfg


def random_wechat_uin() -> str:
    val = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(val).encode("utf-8")).decode("ascii")


def ilink_headers(token: str, body: bytes | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if body is not None:
        headers["Content-Length"] = str(len(body))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_post(base_url: str, endpoint: str, payload: dict[str, Any], token: str, timeout: int) -> dict[str, Any]:
    if "base_info" not in payload:
        payload = {**payload, "base_info": {"channel_version": CHANNEL_VERSION}}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    request = urllib.request.Request(url, data=body, headers=ilink_headers(token, body), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def api_get(base_url: str, endpoint_with_query: str, timeout: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/" + endpoint_with_query.lstrip("/")
    request = urllib.request.Request(url, headers=ilink_headers("", None), method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def load_sync_buf(cfg: BridgeConfig) -> str:
    path = cfg.state_dir / "get_updates_buf.txt"
    try:
        return path.read_text("utf-8")
    except FileNotFoundError:
        return ""


def save_sync_buf(cfg: BridgeConfig, buf: str) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "get_updates_buf.txt").write_text(buf, "utf-8")


def extract_text(item_list: list[dict[str, Any]]) -> str:
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text", "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if not ref_item:
                return text
            ref_text = extract_text([ref_item])
            if ref_text:
                return f"[引用: {ref_text}]\n{text}"
            return text

    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            return (item.get("voice_item") or {}).get("text", "")

    return ""


def describe_non_text_items(item_list: list[dict[str, Any]]) -> list[str]:
    descriptions: list[str] = []
    for item in item_list:
        item_type = item.get("type")
        if item_type == ITEM_IMAGE:
            descriptions.append("[图片消息：当前 PoC 暂未下载图片]")
        elif item_type == ITEM_VIDEO:
            descriptions.append("[视频消息：当前 PoC 暂未下载视频]")
        elif item_type == ITEM_FILE or item.get("file_item"):
            file_item = item.get("file_item") or {}
            name = file_item.get("file_name") or "文件"
            descriptions.append(f"[文件消息：{name}]")
        elif item_type == ITEM_VOICE and not (item.get("voice_item") or {}).get("text"):
            descriptions.append("[语音消息：未包含转写文本]")
    return descriptions


def normalize_prompt(
    text: str,
    item_list: list[dict[str, Any]],
    extra_parts: list[str] | None = None,
) -> str:
    parts = [text.strip()] if text.strip() else []
    parts.extend(describe_non_text_items(item_list))
    parts.extend(extra_parts or [])
    return "\n".join(parts).strip()


SEND_FILE_MARKER_RE = re.compile(r"^\s*\[SEND_FILE:\s*(.+?)\]\s*$", re.MULTILINE)
SEND_FILE_COMMAND_RE = re.compile(r"^\s*/sendfile\s+(.+?)\s*$", re.IGNORECASE)
SCREENSHOT_REQUEST_RE = re.compile(
    r"(?:截(?:个|张)?(?:当前|电脑|桌面|屏幕)?图|截图|截屏|(?:桌面|屏幕)截图|screenshot|screen\s*shot)",
    re.IGNORECASE,
)
GUI_DOWNLOAD_REQUEST_RE = re.compile(
    r"(?:点击|点一下|打开微信|下载(?:这个|该|此)?文件|帮我下载)",
    re.IGNORECASE,
)


def extract_send_file_markers(text: str) -> tuple[str, list[str]]:
    paths = [match.group(1).strip().strip('"') for match in SEND_FILE_MARKER_RE.finditer(text)]
    cleaned = SEND_FILE_MARKER_RE.sub("", text)
    return cleaned.strip(), list(dict.fromkeys(path for path in paths if path))


def resolve_send_file_path(cfg: BridgeConfig, raw_path: str) -> Path:
    value = raw_path.strip().strip('"')
    candidate = Path(value)
    if not candidate.is_absolute():
        # Relative paths remain convenient for files in the configured
        # workspace; absolute paths may point anywhere on this computer.
        candidate = Path(cfg.codex.workspace) / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise FileNotFoundError(f"文件不存在：{candidate}")
    return candidate


def is_screenshot_request(text: str) -> bool:
    """Return whether a message asks the bridge to capture the desktop."""
    return bool(SCREENSHOT_REQUEST_RE.search(text.strip()))


def capture_desktop_screenshot(cfg: BridgeConfig) -> Path:
    """Capture all active Windows displays from the interactive bridge session."""
    try:
        from PIL import ImageGrab
    except ImportError as exc:
        raise RuntimeError("截图功能缺少 Pillow 依赖") from exc

    output_dir = Path(cfg.codex.workspace).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"桌面截图_{time.strftime('%Y%m%d_%H%M%S')}_微信.png"

    try:
        # all_screens matters when Windows is set to “Second screen only” or
        # when the active display has a non-zero virtual-screen origin.
        image = ImageGrab.grab(all_screens=True, include_layered_windows=True)
    except TypeError:
        # Keep compatibility with older Pillow versions.
        image = ImageGrab.grab()

    if image.width <= 0 or image.height <= 0:
        raise RuntimeError("截图返回了空图像")
    image.save(output_path, "PNG")
    log(f"已生成桌面截图 file={output_path} size={image.width}x{image.height}")
    return output_path


def is_authorized(cfg: BridgeConfig, user_id: str) -> bool:
    return cfg.allow_all or user_id in cfg.allow_user_ids


def strip_prefix(cfg: BridgeConfig, text: str) -> tuple[bool, str]:
    if not cfg.require_prefix:
        return True, text.strip()

    stripped = text.strip()
    prefix = cfg.prefix.strip()
    if not prefix:
        return True, stripped

    if stripped == prefix:
        return True, ""
    if stripped.startswith(prefix + " "):
        return True, stripped[len(prefix):].strip()
    if stripped.startswith(prefix + "\n"):
        return True, stripped[len(prefix):].strip()
    return False, stripped


def clean_codex_output(stdout: str, stderr: str) -> str:
    text = stdout.strip() or stderr.strip()
    text = ANSI_RE.sub("", text)
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def build_wechat_prompt(codex: CodexConfig, prompt: str) -> str:
    full_prompt = (
        "请直接回答这条微信消息，不要确认收到，不要要求用户再发送消息。"
            "如果用户明确要求把电脑上的文件发回微信，完成后必须在最终答复单独一行输出 "
            "[SEND_FILE: 文件路径]；文件路径可以是绝对路径或相对工作目录的路径。"
            "如果必须调用 view_image，detail 只能使用 high 或 original，不能使用 low。"
        f"微信消息：{prompt}"
    )
    if codex.prompt_prefix:
        full_prompt += "。" + codex.prompt_prefix.strip()
    return full_prompt


def resolve_additional_directories(codex: CodexConfig) -> list[Path]:
    workspace = Path(codex.workspace).expanduser().resolve()
    directories: list[Path] = []
    for raw in codex.additional_directories:
        value = str(raw).strip().strip('"')
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.resolve()
        if not candidate.is_dir():
            raise FileNotFoundError(f"额外可写目录不存在或不是文件夹：{candidate}")
        if candidate != workspace and candidate not in directories:
            directories.append(candidate)
    return directories


def run_codex(cfg: BridgeConfig, prompt: str) -> str:
    codex = cfg.codex
    full_prompt = build_wechat_prompt(codex, prompt)

    args = [
        codex.command,
        "exec",
        "--ask-for-approval",
        codex.approval_policy,
        "-C",
        codex.workspace,
        "--sandbox",
        codex.sandbox,
    ]
    if codex.model:
        args.extend(["--model", codex.model])
    if codex.profile:
        args.extend(["--profile", codex.profile])
    for directory in resolve_additional_directories(codex):
        args.extend(["--add-dir", str(directory)])
    args.extend(codex.extra_args)
    args.append(full_prompt)

    log("调用 codex exec")
    try:
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=codex.timeout_seconds,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"Codex 执行超时（>{codex.timeout_seconds}s）。"
    except FileNotFoundError:
        return f"找不到 Codex 命令：{codex.command}"

    output = clean_codex_output(proc.stdout, proc.stderr)
    if proc.returncode != 0:
        return output or f"Codex 执行失败，退出码 {proc.returncode}。"
    return output or "Codex 没有返回内容。"


def resolve_app_server_command(codex: CodexConfig) -> str:
    if codex.app_server_command:
        return codex.app_server_command

    command = codex.command
    lower = command.replace("\\", "/").lower()
    if lower.endswith("/codex.cmd"):
        npm_dir = Path(command).parent
        native = (
            npm_dir
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
            / "codex-win32-x64"
            / "vendor"
            / "x86_64-pc-windows-msvc"
            / "bin"
            / "codex.exe"
        )
        if native.exists():
            return str(native)
    return command


class AppServerCodexClient:
    def __init__(self, cfg: BridgeConfig):
        self.cfg = cfg
        self.command = resolve_app_server_command(cfg.codex)
        self.proc: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.next_id = 1
        self.thread_file = cfg.state_dir / "threads.json"
        self.threads: dict[str, str] = {}
        self.resumed_threads: set[str] = set()
        if self.thread_file.exists():
            try:
                stored = json.loads(self.thread_file.read_text("utf-8"))
                if isinstance(stored, dict):
                    self.threads = {str(k): str(v) for k, v in stored.items() if k and v}
            except Exception as exc:
                log(f"读取持久化 Codex 会话失败，将创建新会话：{exc}")
        self.lock = threading.Lock()

    def _save_threads(self) -> None:
        self.thread_file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.thread_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.threads, ensure_ascii=False, indent=2) + "\n", "utf-8")
        temp.replace(self.thread_file)

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _reader(self, stream: Any, name: str) -> None:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            if name == "stderr":
                log(f"app-server stderr: {line[:500]}")
                continue
            try:
                self.messages.put(json.loads(line))
            except Exception:
                log(f"app-server 输出无法解析：{line[:500]}")

    def _start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return

        args = [self.command, "app-server", "--stdio"]
        log(f"启动 codex app-server: {self.command}")
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        threading.Thread(target=self._reader, args=(self.proc.stdout, "stdout"), daemon=True).start()
        threading.Thread(target=self._reader, args=(self.proc.stderr, "stderr"), daemon=True).start()
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "codex-wechat", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
            timeout=20,
        )

    def _runtime_workspace_roots(self) -> list[str]:
        workspace = Path(self.cfg.codex.workspace).expanduser().resolve()
        roots = [workspace, *resolve_additional_directories(self.cfg.codex)]
        return [str(path) for path in dict.fromkeys(roots)]

    def _send(self, message: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("app-server 未启动")
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: Any, timeout: int | None = None) -> dict[str, Any]:
        req_id = self.next_id
        self.next_id += 1
        self._send({"id": req_id, "method": method, "params": params})

        deadline = time.time() + (timeout or self.cfg.codex.timeout_seconds)
        while time.time() < deadline:
            try:
                msg = self.messages.get(timeout=0.5)
            except queue.Empty:
                if self.proc and self.proc.poll() is not None:
                    raise RuntimeError(f"app-server 已退出，退出码 {self.proc.returncode}")
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result") or {}
            # 其它通知留给当前请求外的事件循环自然丢弃。
        raise TimeoutError(f"{method} 超时")

    def _ensure_thread(self, user_id: str) -> str:
        # An explicitly configured default must remain the conversation target.
        thread_id = self.cfg.thread_id.strip() or self.threads.get(user_id)
        if thread_id:
            try:
                if thread_id not in self.resumed_threads:
                    self._request(
                        "thread/resume",
                        {
                            "threadId": thread_id,
                            "runtimeWorkspaceRoots": self._runtime_workspace_roots(),
                        },
                        timeout=30,
                    )
                    self.resumed_threads.add(thread_id)
                if self.threads.get(user_id) != thread_id:
                    self.threads[user_id] = thread_id
                    self._save_threads()
                return thread_id
            except Exception as exc:
                if self.cfg.thread_id.strip():
                    log(f"恢复默认 Codex 会话 {thread_id} 失败，保留目标会话，本次不新建会话：{exc}")
                    raise
                # Codex++ drops a stale/busy mapping and creates a fresh
                # persistent thread in the configured work directory.
                log(f"恢复 Codex 会话 {thread_id} 失败，将在临时工作中新建会话：{exc}")
                self.threads.pop(user_id, None)
                self._save_threads()

        sandbox = {
            "read-only": "read-only",
            "workspace-write": "workspace-write",
            "danger-full-access": "danger-full-access",
        }.get(self.cfg.codex.sandbox, "read-only")
        params: dict[str, Any] = {
            "cwd": self.cfg.codex.workspace,
            "approvalPolicy": self.cfg.codex.approval_policy,
            "sandbox": sandbox,
            "ephemeral": False,
            "baseInstructions": "你是运行在微信里的 Codex 助手。全程中文，只把最终答复发给微信用户。",
            "runtimeWorkspaceRoots": self._runtime_workspace_roots(),
        }
        if self.cfg.codex.model:
            params["model"] = self.cfg.codex.model
        result = self._request("thread/start", params, timeout=60)
        thread_id = result["thread"]["id"]
        self.threads[user_id] = thread_id
        self._save_threads()
        return thread_id

    def warmup(self, user_id: str = "") -> None:
        """Start app-server before the first message without creating a throwaway thread."""
        with self.lock:
            self._start()

    def run(self, user_id: str, prompt: str) -> str:
        with self.lock:
            self._start()
            thread_id = self._ensure_thread(user_id)
            full_prompt = build_wechat_prompt(self.cfg.codex, prompt)
            result = self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": full_prompt, "text_elements": []}],
                    "approvalPolicy": self.cfg.codex.approval_policy,
                    "cwd": self.cfg.codex.workspace,
                },
                timeout=30,
            )
            turn_id = result["turn"]["id"]
            return self._collect_turn(thread_id, turn_id)

    def _collect_turn(self, thread_id: str, turn_id: str) -> str:
        deadline = time.time() + self.cfg.codex.timeout_seconds
        final_item_id = ""
        final_chunks: list[str] = []
        completed = False
        error = ""

        while time.time() < deadline and not completed:
            try:
                msg = self.messages.get(timeout=1)
            except queue.Empty:
                if self.proc and self.proc.poll() is not None:
                    raise RuntimeError(f"app-server 已退出，退出码 {self.proc.returncode}")
                continue

            method = msg.get("method")
            params = msg.get("params") or {}
            if params.get("threadId") != thread_id:
                continue

            if method == "item/started":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    final_item_id = str(item.get("id") or "")
            elif method == "item/agentMessage/delta":
                if final_item_id and params.get("turnId") == turn_id and params.get("itemId") == final_item_id:
                    final_chunks.append(str(params.get("delta") or ""))
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    text = str(item.get("text") or "")
                    if text:
                        final_chunks = [text]
            elif method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                turn = params.get("turn") or {}
                if turn.get("status") == "failed":
                    error = str((turn.get("error") or {}).get("message") or "turn failed")
                completed = True

        if not completed:
            raise TimeoutError("app-server turn 超时")
        if error:
            raise RuntimeError(error)

        reply = "".join(final_chunks).strip()
        return reply or "Codex 没有返回最终答复。"


class CodexRunner:
    def __init__(self, cfg: BridgeConfig):
        self.cfg = cfg
        self.app_server: AppServerCodexClient | None = None
        if cfg.codex.backend == "app-server":
            self.app_server = AppServerCodexClient(cfg)
            atexit.register(self.app_server.close)

    def warmup(self, user_id: str = "") -> None:
        if not self.app_server:
            return
        try:
            self.app_server.warmup(user_id)
            log("Codex app-server 预热完成，首次消息将复用或创建持久会话")
        except Exception as exc:
            log(f"Codex 预热未完成，首次消息时会自动重试：{exc}")

    def run(self, user_id: str, prompt: str) -> str:
        if self.app_server:
            try:
                log("调用 codex app-server")
                return self.app_server.run(user_id, prompt)
            except Exception as exc:
                if self.cfg.thread_id.strip():
                    log(f"默认会话调用失败，本次不回退 exec、不切换会话：{exc}")
                    if "already has an active writer" in str(exc):
                        return (
                            "默认会话正被另一个 Codex 进程占用，本次没有新建或切换会话。"
                            "请先完全退出占用它的 Codex 桌面端，再重试；仅切换聊天页面可能不会释放会话。"
                        )
                    return f"默认会话调用失败，本次没有新建或切换会话。错误：{exc}"
                log(f"app-server 调用失败，回退 exec：{exc}")
        return run_codex(self.cfg, prompt)


def split_reply(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > max_chars:
        cut = rest.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks


def encrypt_aes_ecb_pkcs7(data: bytes, key: bytes) -> bytes:
    """Encrypt a file using the iLink CDN's AES-128-ECB + PKCS#7 format."""
    if len(key) != 16:
        raise ValueError("iLink 文件加密 key 必须是 16 字节")
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError("当前 Python 缺少 cryptography，无法发送微信文件") from exc

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(data) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_aes_ecb_pkcs7(data: bytes, key: bytes) -> bytes:
    if len(key) != 16:
        raise ValueError("iLink 文件解密 key 必须是 16 字节")
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError("当前 Python 缺少 cryptography，无法下载微信文件") from exc

    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def parse_inbound_aes_keys(value: str) -> list[bytes]:
    value = value.strip()
    if len(value) == 32:
        try:
            return [bytes.fromhex(value)]
        except ValueError:
            pass
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("微信文件消息里的 aes_key 不是有效的 Base64") from exc

    keys: list[bytes] = []
    if len(decoded) == 16:
        keys.append(decoded)
    try:
        hex_text = decoded.decode("ascii")
        if len(hex_text) == 32:
            hex_key = bytes.fromhex(hex_text)
            if hex_key not in keys:
                keys.append(hex_key)
    except (UnicodeDecodeError, ValueError):
        pass
    if not keys:
        raise ValueError("微信文件消息里的 aes_key 长度不正确")
    return keys


def download_cdn_file(cfg: BridgeConfig, media: dict[str, Any]) -> bytes:
    full_url = str(
        media.get("full_url")
        or media.get("download_url")
        or media.get("url")
        or ""
    ).strip()
    if not full_url:
        encrypted_param = str(media.get("encrypt_query_param") or "").strip()
        if not encrypted_param:
            raise ValueError("文件消息缺少 CDN 下载参数")
        query = urllib.parse.urlencode({"encrypted_query_param": encrypted_param})
        full_url = f"{ILINK_CDN_BASE_URL.rstrip('/')}/download?{query}"

    request = urllib.request.Request(
        full_url,
        headers={"Accept": "application/octet-stream"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > cfg.max_download_bytes:
                raise ValueError("微信文件超过本地下载大小限制")
            ciphertext = resp.read(cfg.max_download_bytes + 1)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"微信 CDN 下载失败 HTTP {exc.code}: {body[:300]}") from exc

    if len(ciphertext) > cfg.max_download_bytes:
        raise ValueError("微信文件超过本地下载大小限制")
    last_error: Exception | None = None
    for key in parse_inbound_aes_keys(str(media.get("aes_key") or "")):
        try:
            return decrypt_aes_ecb_pkcs7(ciphertext, key)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"微信文件解密失败：{last_error}") from last_error


def save_inbound_file(cfg: BridgeConfig, file_name: str, data: bytes) -> Path:
    inbox = Path(cfg.download_dir).expanduser() if cfg.download_dir.strip() else Path(cfg.codex.workspace) / "微信收到的文件"
    if not inbox.is_absolute():
        inbox = Path(cfg.codex.workspace) / inbox
    inbox = inbox.resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file_name or "微信文件.bin").name
    if safe_name in {"", ".", ".."}:
        safe_name = "微信文件.bin"
    target = inbox / safe_name
    if target.exists():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        index = 1
        while target.exists():
            target = inbox / f"{Path(safe_name).stem}_{stamp}_{index}{Path(safe_name).suffix}"
            index += 1
    target.write_bytes(data)
    return target


def download_inbound_files(cfg: BridgeConfig, item_list: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    for item in item_list:
        if item.get("type") != ITEM_FILE and not item.get("file_item"):
            continue
        file_item = item.get("file_item") or {}
        name = str(file_item.get("file_name") or "微信文件")
        media = dict(file_item.get("media") or {})
        for key in ("full_url", "download_url", "url", "encrypt_query_param", "aes_key", "encrypt_type"):
            if not media.get(key):
                media[key] = file_item.get(key) or item.get(key) or ""
        try:
            data = download_cdn_file(cfg, media)
            target = save_inbound_file(cfg, name, data)
            log(f"收到文件已下载：{target}")
            notes.append(f"[收到文件：{name}，已保存到：{target}]")
        except Exception as exc:
            log(f"收到文件下载失败 name={name}：{exc}")
            notes.append(f"[文件下载失败：{name}，原因：{exc}]")
    return notes


def post_binary(url: str, data: bytes, timeout: int) -> tuple[Any, bytes]:
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"微信 CDN 上传失败 HTTP {exc.code}: {body[:300]}") from exc


def upload_file_to_wechat(
    cfg: BridgeConfig,
    to_user_id: str,
    file_path: Path,
) -> tuple[dict[str, Any], int]:
    data = file_path.read_bytes()
    raw_size = len(data)
    if raw_size > cfg.max_upload_bytes:
        limit_mb = cfg.max_upload_bytes / 1024 / 1024
        raise ValueError(f"文件超过当前限制（{limit_mb:.0f} MiB）：{file_path.name}")

    aes_key = secrets.token_bytes(16)
    ciphertext = encrypt_aes_ecb_pkcs7(data, aes_key)
    file_key = secrets.token_hex(16)
    upload_resp = api_post(
        cfg.base_url,
        EP_GET_UPLOAD_URL,
        {
            "filekey": file_key,
            "media_type": 3,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": hashlib.md5(data).hexdigest(),
            "filesize": len(ciphertext),
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
        },
        cfg.token,
        timeout=20,
    )
    if upload_resp.get("ret") not in (None, 0):
        raise RuntimeError(
            f"微信 getuploadurl 失败：ret={upload_resp.get('ret')} "
            f"{upload_resp.get('errmsg') or upload_resp.get('errstr') or ''}".strip()
        )

    upload_url = str(upload_resp.get("upload_full_url") or "").strip()
    if not upload_url:
        upload_param = str(upload_resp.get("upload_param") or "").strip()
        if not upload_param:
            raise RuntimeError("微信 getuploadurl 没有返回 upload_full_url/upload_param")
        query = urllib.parse.urlencode(
            {"encrypted_query_param": upload_param, "filekey": file_key}
        )
        upload_url = f"{ILINK_CDN_BASE_URL.rstrip('/')}/upload?{query}"

    headers, body = post_binary(upload_url, ciphertext, timeout=60)
    download_param = headers.get("x-encrypted-param")
    if not download_param:
        try:
            response_json = json.loads(body.decode("utf-8"))
        except Exception:
            response_json = {}
        download_param = (
            response_json.get("encrypt_query_param")
            or response_json.get("download_encrypted_query_param")
            or ""
        )
    if not download_param:
        raise RuntimeError("微信 CDN 上传成功但没有返回 x-encrypted-param")

    media = {
        "encrypt_query_param": str(download_param),
        # The FILE message format carries base64(hex(aes_key)), matching the
        # current iLink client implementation.  It is not base64(raw bytes).
        "aes_key": base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii"),
        "encrypt_type": 1,
    }
    return media, raw_size


def send_file(
    cfg: BridgeConfig,
    to_user_id: str,
    file_path: Path,
    context_token: str = "",
    caption: str = "",
) -> None:
    media, raw_size = upload_file_to_wechat(cfg, to_user_id, file_path)
    if caption.strip():
        send_text(cfg, to_user_id, caption.strip(), context_token)
    msg: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": "codex-claw-" + secrets.token_hex(8),
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [
            {
                "type": ITEM_FILE,
                "file_item": {
                    "media": media,
                    "file_name": file_path.name,
                    "len": str(raw_size),
                },
            }
        ],
    }
    if context_token:
        msg["context_token"] = context_token
    api_post(cfg.base_url, EP_SEND_MESSAGE, {"msg": msg}, cfg.token, timeout=30)


def send_text(cfg: BridgeConfig, to_user_id: str, text: str, context_token: str = "") -> None:
    for chunk in split_reply(text, cfg.max_reply_chars):
        msg = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": "codex-claw-" + secrets.token_hex(8),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": chunk}}],
        }
        if context_token:
            msg["context_token"] = context_token
        api_post(cfg.base_url, EP_SEND_MESSAGE, {"msg": msg}, cfg.token, timeout=15)


def get_typing_ticket(cfg: BridgeConfig, user_id: str, context_token: str) -> str:
    payload: dict[str, Any] = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    resp = api_post(cfg.base_url, EP_GET_CONFIG, payload, cfg.token, timeout=10)
    return str(resp.get("typing_ticket") or "")


def send_typing(cfg: BridgeConfig, user_id: str, ticket: str, status: int) -> None:
    if not ticket:
        return
    api_post(
        cfg.base_url,
        EP_SEND_TYPING,
        {"ilink_user_id": user_id, "typing_ticket": ticket, "status": status},
        cfg.token,
        timeout=10,
    )


def handle_command(cfg: BridgeConfig, user_id: str, text: str) -> str:
    command = text.strip()
    if command in {"/id", "/whoami", f"{cfg.prefix} id", f"{cfg.prefix} /id"}:
        return f"你的 iLink user_id：{user_id}"
    if command in {"/ping", f"{cfg.prefix} ping"}:
        return "pong"
    if command in {"/help", f"{cfg.prefix} help"}:
        return (
            "Codex 微信助手 PoC\n"
            f"- 发送 `{cfg.prefix} 你的问题` 调用 Codex\n"
            "- 发送 `/sendfile 文件路径` 把电脑上的文件发到微信\n"
            "- 发送 `/id` 查看你的 iLink user_id\n"
            "- 默认只响应 allow_user_ids 中的用户"
        )
    return ""


def process_message(
    cfg: BridgeConfig,
    runner: CodexRunner,
    msg: dict[str, Any],
    seen: dict[str, float],
) -> None:
    if msg.get("message_type", 1) != 1:
        return

    user_id = str(msg.get("from_user_id") or "")
    if not user_id or user_id == cfg.account_id:
        return

    msg_id = str(msg.get("message_id") or "")
    now = time.time()
    for key, ts in list(seen.items()):
        if now - ts > 300:
            del seen[key]
    if msg_id:
        if msg_id in seen:
            return
        seen[msg_id] = now

    context_token = str(msg.get("context_token") or "")
    item_list = msg.get("item_list") or []
    if not isinstance(item_list, list):
        item_list = []

    item_summary: list[str] = []
    for item in item_list:
        if not isinstance(item, dict):
            continue
        payloads = ",".join(
            key
            for key in ("text_item", "file_item", "image_item", "voice_item", "video_item")
            if item.get(key) is not None
        )
        item_summary.append(f"type={item.get('type')} payload={payloads or '-'}")
    log(f"收到消息结构 user={user_id[:12]} items={' ; '.join(item_summary) or '-'}")

    text = extract_text(item_list).strip()
    command_reply = handle_command(cfg, user_id, text)
    if command_reply and (is_authorized(cfg, user_id) or cfg.allow_id_command_for_unauthorized):
        send_text(cfg, user_id, command_reply, context_token)
        return

    if not is_authorized(cfg, user_id):
        log(f"忽略未授权用户：{user_id[:12]}")
        return

    accepted, stripped = strip_prefix(cfg, text)
    if not accepted:
        return

    download_notes = download_inbound_files(cfg, item_list)
    has_inbound_file = any(
        isinstance(item, dict)
        and (item.get("type") == ITEM_FILE or item.get("file_item"))
        for item in item_list
    )

    if download_notes and not stripped:
        send_text(cfg, user_id, "\n".join(download_notes), context_token)
        return

    if not has_inbound_file and GUI_DOWNLOAD_REQUEST_RE.search(stripped):
        send_text(
            cfg,
            user_id,
            "我不能点击你电脑上的微信窗口下载文件。请把 Word、Excel 或其他文件直接作为附件发送给这个微信助手，我会自动保存到配置中的下载目录。",
            context_token,
        )
        return

    direct_file_match = SEND_FILE_COMMAND_RE.match(stripped)
    if direct_file_match:
        try:
            file_path = resolve_send_file_path(cfg, direct_file_match.group(1))
            log(f"发送文件 user={user_id[:12]} file={file_path}")
            send_file(cfg, user_id, file_path, context_token, f"已发送：{file_path.name}")
        except Exception as exc:
            log(f"发送文件失败：{exc}")
            send_text(cfg, user_id, f"文件发送失败：{exc}", context_token)
        return

    if is_screenshot_request(stripped):
        try:
            log(f"直接截图 user={user_id[:12]}")
            file_path = capture_desktop_screenshot(cfg)
            send_file(cfg, user_id, file_path, context_token, f"已发送：{file_path.name}")
        except Exception as exc:
            log(f"直接截图失败：{exc}")
            send_text(cfg, user_id, f"截图失败：{exc}", context_token)
        return

    prompt = normalize_prompt(stripped, item_list, extra_parts=download_notes)
    if not prompt:
        send_text(cfg, user_id, "没有可处理的文本内容。", context_token)
        return

    log(f"收到消息 user={user_id[:12]} len={len(prompt)}")
    typing_ticket = ""
    if cfg.enable_typing:
        try:
            typing_ticket = get_typing_ticket(cfg, user_id, context_token)
            send_typing(cfg, user_id, typing_ticket, 1)
        except Exception as exc:
            log(f"发送 typing 失败：{exc}")

    try:
        reply = runner.run(user_id, prompt)
        reply_text, file_markers = extract_send_file_markers(reply)
        if not file_markers:
            send_text(cfg, user_id, reply_text or reply, context_token)
        else:
            if reply_text:
                send_text(cfg, user_id, reply_text, context_token)
            for raw_path in file_markers:
                try:
                    file_path = resolve_send_file_path(cfg, raw_path)
                    log(f"Codex 请求发送文件 user={user_id[:12]} file={file_path}")
                    send_file(cfg, user_id, file_path, context_token)
                except Exception as exc:
                    log(f"Codex 请求发送文件失败：{exc}")
                    send_text(cfg, user_id, f"文件发送失败：{exc}", context_token)
    finally:
        if cfg.enable_typing and typing_ticket:
            try:
                send_typing(cfg, user_id, typing_ticket, 0)
            except Exception:
                pass


def poll_loop(cfg: BridgeConfig) -> None:
    global LOG_FILE
    LOG_FILE = cfg.state_dir / "codex-wechat.log"
    if not cfg.token:
        raise SystemExit("缺少 token：请在配置里填写 token，或设置 WEIXIN_TOKEN / ILINK_TOKEN。")
    if not cfg.allow_all and not cfg.allow_user_ids:
        log("未配置 allow_user_ids，除 /id 外不会处理任何用户消息。")

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    sync_buf = load_sync_buf(cfg)
    seen: dict[str, float] = {}
    failures = 0
    runner = CodexRunner(cfg)
    warmup_user_id = next(iter(cfg.allow_user_ids), "")
    runner.warmup(warmup_user_id)

    log(f"启动 Codex 微信助手，base={cfg.base_url}，backend={cfg.codex.backend}")
    while True:
        try:
            resp = api_post(
                cfg.base_url,
                EP_GET_UPDATES,
                {"get_updates_buf": sync_buf},
                cfg.token,
                timeout=cfg.poll_timeout_seconds + 5,
            )
            ret = resp.get("ret")
            errcode = resp.get("errcode")
            if (ret not in (None, 0)) or (errcode not in (None, 0)):
                failures += 1
                log(f"getupdates 返回错误 ret={ret} errcode={errcode} errmsg={resp.get('errmsg', '')}")
                time.sleep(cfg.backoff_delay_seconds if failures >= cfg.max_consecutive_failures else cfg.retry_delay_seconds)
                if failures >= cfg.max_consecutive_failures:
                    failures = 0
                continue

            failures = 0
            sync_buf = str(resp.get("get_updates_buf") or sync_buf)
            if sync_buf:
                save_sync_buf(cfg, sync_buf)

            msgs = resp.get("msgs") or []
            if msgs:
                log(f"收到 {len(msgs)} 条 iLink 消息")
            for msg in msgs:
                if isinstance(msg, dict):
                    try:
                        process_message(cfg, runner, msg, seen)
                    except Exception as exc:
                        log(f"处理消息失败：{exc}")

        except KeyboardInterrupt:
            log("收到中断，退出。")
            return
        except urllib.error.URLError as exc:
            failures += 1
            log(f"iLink 网络错误：{exc}")
            time.sleep(cfg.backoff_delay_seconds if failures >= cfg.max_consecutive_failures else cfg.retry_delay_seconds)
            if failures >= cfg.max_consecutive_failures:
                failures = 0
        except Exception as exc:
            failures += 1
            log(f"轮询异常：{exc}")
            time.sleep(cfg.backoff_delay_seconds if failures >= cfg.max_consecutive_failures else cfg.retry_delay_seconds)
            if failures >= cfg.max_consecutive_failures:
                failures = 0


def qr_login(output: Path, bot_type: str, timeout_seconds: int) -> None:
    resp = api_get(ILINK_BASE_URL, f"{EP_GET_BOT_QR}?bot_type={urllib.parse.quote(bot_type)}", timeout=35)
    qrcode = str(resp.get("qrcode") or "")
    qrcode_url = str(resp.get("qrcode_img_content") or "")
    if not qrcode:
        raise SystemExit("获取二维码失败：响应里没有 qrcode。")

    print("请用微信扫描下面的二维码链接：")
    print(qrcode_url)
    print()

    deadline = time.time() + timeout_seconds
    current_base = ILINK_BASE_URL
    while time.time() < deadline:
        status_resp = api_get(
            current_base,
            f"{EP_GET_QR_STATUS}?qrcode={urllib.parse.quote(qrcode)}",
            timeout=35,
        )
        status = str(status_resp.get("status") or "wait")
        if status == "wait":
            print(".", end="", flush=True)
        elif status == "scaned":
            print("\n已扫码，请在微信中确认。")
        elif status == "scaned_but_redirect":
            redirect_host = str(status_resp.get("redirect_host") or "")
            if redirect_host:
                current_base = "https://" + redirect_host
                print(f"\n切换 iLink base_url：{current_base}")
        elif status == "confirmed":
            token = str(status_resp.get("bot_token") or "")
            account_id = str(status_resp.get("ilink_bot_id") or "")
            base_url = str(status_resp.get("baseurl") or current_base or ILINK_BASE_URL)
            user_id = str(status_resp.get("ilink_user_id") or "")
            if not token or not account_id:
                raise SystemExit("扫码成功但没有拿到 bot_token/account_id。")

            config = load_json_file(output) if output.exists() else {}
            config.update({"base_url": base_url, "token": token, "account_id": account_id})
            if user_id:
                config["login_user_id"] = user_id
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", "utf-8")
            print(f"\n登录成功，配置已写入：{output}")
            print("注意：该文件包含微信 iLink token，不要提交到 Git。")
            return
        elif status == "expired":
            raise SystemExit("\n二维码已过期，请重新运行 login。")
        time.sleep(1)

    raise SystemExit("\n登录超时。")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Codex WeChat Claw PoC")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="启动 iLink -> codex app-server/exec -> iLink 转发")
    run_parser.add_argument("--config", required=True, type=Path, help="配置文件路径")

    login_parser = sub.add_parser("login", help="扫码获取 iLink token 并写入配置")
    login_parser.add_argument("--output", required=True, type=Path, help="要写入的本地配置文件")
    login_parser.add_argument("--bot-type", default="3", help="iLink bot_type，默认 3")
    login_parser.add_argument("--timeout", default=480, type=int, help="扫码超时时间，秒")

    args = parser.parse_args(argv)
    if args.command == "run":
        poll_loop(load_config(args.config))
        return 0
    if args.command == "login":
        qr_login(args.output, args.bot_type, args.timeout)
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
