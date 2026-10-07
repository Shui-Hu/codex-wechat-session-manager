# SPDX-License-Identifier: AGPL-3.0-only
"""One app-server shared by the session picker and the existing WeChat bridge."""
from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import urllib.error

import bridge_runtime as bridge

DEFAULT_CONFIG = Path(os.environ.get('CODEX_WECHAT_CONFIG', str(Path.home() / '.codex-wechat-session-manager' / 'config.json'))).expanduser()
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class UserError(RuntimeError):
    pass


class InstanceGuard:
    def __init__(self, path):
        self.handle = None
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            self.kernel = kernel
            key = hashlib.sha256(str(Path(path).resolve()).lower().encode()).hexdigest()[:24]
            self.handle = kernel.CreateMutexW(None, False, 'Local\\CodexWeChatManager-' + key)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == 183:
                self.close()
                raise UserError('会话管家已经打开，请使用原来的窗口。')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def writer_status(thread_id, owned=False):
    if owned:
        return '本程序已接管'
    if os.name != 'nt':
        return '待接管'
    path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'thread-writer-locks' / (thread_id + '.lock')
    if not path.exists():
        return '待接管'
    import msvcrt
    try:
        with path.open('r+b') as stream:
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return '其他进程占用'
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return '待接管'
    except OSError:
        return '占用情况未知'


def discover_codex(configured):
    candidate = Path(configured)
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(configured)
    if resolved:
        return resolved
    root = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))) / 'OpenAI' / 'Codex' / 'bin'
    choices = list(root.glob('*/codex.exe'))
    if not choices:
        raise UserError('找不到 Codex 程序，请先安装或启动一次 Codex 桌面端。')
    return str(max(choices, key=lambda p: p.stat().st_mtime))


class ManagedClient(bridge.AppServerCodexClient):
    def close(self):
        # Wake a pending metadata request so closing the window does not leave
        # the UI executor waiting for the full RPC timeout.
        self.messages.put({'id': self.next_id - 1, 'error': {
            'code': -32000, 'message': '当前会话后台已关闭。'}})
        super().close()

    def _start(self):
        if self.proc and self.proc.poll() is None:
            return
        self.messages = queue.Queue()
        self.resumed_threads.clear()
        self.proc = subprocess.Popen(
            [self.command, 'app-server', '--stdio'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', bufsize=1, creationflags=NO_WINDOW)
        threading.Thread(target=self._reader, args=(self.proc.stdout, 'stdout'), daemon=True).start()
        threading.Thread(target=self._reader, args=(self.proc.stderr, 'stderr'), daemon=True).start()
        self._request('initialize', {
            'clientInfo': {'name': 'codex-wechat-manager', 'version': '1.0.0'},
            'capabilities': {'experimentalApi': True}}, timeout=20)
        self._send({'method': 'initialized', 'params': {}})


def legacy_processes(config_path):
    script = "$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='codex.exe'\" | Select-Object ProcessId,ParentProcessId,Name,CommandLine) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            creationflags=NO_WINDOW, timeout=15)
    if result.returncode:
        raise UserError('无法检查旧助手进程。请先关闭旧微信助手窗口，再启动本程序中的微信连接。')
    records = json.loads(result.stdout or '[]')
    if isinstance(records, dict):
        records = [records]
    config_text = str(Path(config_path).resolve()).lower()
    parents = [r for r in records if r['Name'].lower() in ('python.exe', 'pythonw.exe')
               and 'codex_wechat.py' in (r.get('CommandLine') or '').lower()
               and ' run ' in (r.get('CommandLine') or '').lower()
               and config_text in (r.get('CommandLine') or '').lower()]
    parent_ids = {r['ProcessId'] for r in parents}
    children = [r for r in records if r['ParentProcessId'] in parent_ids
                and r['Name'].lower() == 'codex.exe'
                and 'app-server' in (r.get('CommandLine') or '')]
    return parents + children


def stop_legacy(config_path):
    # Recheck identities in one PowerShell invocation; never terminate the desktop app-server.
    quoted = str(Path(config_path).resolve()).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop'; $cfgPath='" + quoted + "'; "
        "$all=@(Get-CimInstance Win32_Process); "
        "$old=@($all | Where-Object {($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') "
        r"-and $_.CommandLine -match 'codex_wechat\.py.+\srun\s' "
        "-and $_.CommandLine.IndexOf($cfgPath,[StringComparison]::OrdinalIgnoreCase) -ge 0}); "
        "$children=@($all | Where-Object {$_.Name -eq 'codex.exe' -and "
        "$old.ProcessId -contains $_.ParentProcessId -and $_.CommandLine -match 'app-server'}); "
        "foreach($p in $old){Stop-Process -Id $p.ProcessId -ErrorAction SilentlyContinue}; "
        "foreach($p in $children){Stop-Process -Id $p.ProcessId -ErrorAction SilentlyContinue}; "
        "@{stopped=$old.Count} | ConvertTo-Json -Compress")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW, timeout=15)
    if result.returncode:
        raise UserError('关闭旧助手失败，请手动关闭原来的微信助手窗口。')
    return json.loads(result.stdout)['stopped']


class SessionService:
    def __init__(self, config_path, emit, client_factory=ManagedClient):
        self.config_path = Path(config_path).resolve()
        self.emit = emit
        self.client_factory = client_factory
        self.operation_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.poll_thread = None
        self.processing = False
        self.claimed_id = ''
        self.current_name = ''
        self.closed = False
        self.raw = read_json(self.config_path)
        self.cfg = bridge.load_config(self.config_path)
        self.cfg.codex.backend = 'app-server'
        self.cfg.codex.command = discover_codex(self.cfg.codex.command)
        self.cfg.codex.app_server_command = self.cfg.codex.command
        # The request body does not ask iLink to shorten its server-side wait.
        # A 10s client timeout therefore interrupts healthy long polling.
        self.poll_request_timeout = max(35, self.cfg.poll_timeout_seconds) + 10
        self.client = self.client_factory(self.cfg)
        self.runner = bridge.CodexRunner.__new__(bridge.CodexRunner)
        self.runner.cfg = self.cfg
        self.runner.app_server = self.client
        self.log_file = self.config_path.parent / 'state' / 'session-manager.log'
        bridge.log = self.log

    def log(self, message):
        text = str(message)
        for secret in (self.cfg.token, self.raw.get('token', '')):
            if secret:
                text = text.replace(secret, '[已隐藏]')
        line = time.strftime('[%H:%M:%S] ') + text
        self.emit('log', line)
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass

    def _rpc(self, method, params, timeout=30):
        with self.operation_lock, self.client.lock:
            self.client._start()
            return self.client._request(method, params, timeout=timeout)

    def list_threads(self):
        records, cursor = [], None
        while True:
            params = {'limit': 100, 'sortKey': 'updated_at', 'sortDirection': 'desc',
                      'sourceKinds': ['cli', 'vscode', 'exec', 'appServer', 'unknown'],
                      'archived': False, 'useStateDbOnly': True}
            if cursor:
                params['cursor'] = cursor
            page = self._rpc('thread/list', params)
            records.extend(page.get('data') or [])
            next_cursor = page.get('nextCursor')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        # Codex deliberately omits chats without their first user message from
        # thread/list. Keep a newly created default visible until that message.
        default_id = self.cfg.thread_id.strip()
        if default_id and default_id not in {r['id'] for r in records}:
            try:
                pending = self._rpc('thread/read', {'threadId': default_id, 'includeTurns': False})['thread']
                if not pending.get('ephemeral'):
                    records.append(pending)
            except Exception as exc:
                self.log('默认会话暂时无法读取：' + str(exc))
        # Desktop builds may store pin state in sidebar settings rather than isPinned.
        codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        try:
            pin_values = read_json(codex_home / '.codex-global-state.json').get('pinned-thread-ids')
        except (OSError, ValueError):
            pin_values = None
        pin_ids = set(pin_values or [])
        for record in records:
            section = record.get('section') or {}
            record['isPinned'] = bool(record.get('isPinned')) or record['id'] in pin_ids or (
                pin_values is None and str(section.get('name') or '').lower() == 'pinned')
            record['managerStatus'] = writer_status(record['id'], record['id'] == self.claimed_id)
        return sorted(records, key=lambda r: (not r.get('isPinned', False), -float(r.get('updatedAt') or 0)))

    def inspect(self, thread_id):
        # Reading history never resumes a thread or acquires its writer lock.
        result = self._rpc('thread/turns/list', {
            'threadId': thread_id, 'limit': 3, 'sortDirection': 'desc', 'itemsView': 'full'})
        blocks = []
        for turn in reversed(result.get('data') or []):
            for item in turn.get('items') or []:
                kind = item.get('type')
                if kind == 'userMessage':
                    content = item.get('content') or []
                    text = '\n'.join(str(p.get('text') or '') for p in content if p.get('type') == 'text')
                    if text:
                        blocks.append('用户：\n' + text[:2500])
                elif kind == 'agentMessage' and item.get('text'):
                    blocks.append('Codex：\n' + str(item['text'])[:4500])
        return '\n\n'.join(blocks) or '这条会话暂时没有可显示的文字消息。'

    def _require_stopped(self):
        if self.poll_thread and self.poll_thread.is_alive():
            raise UserError('请先停止微信连接，等当前消息处理完成后再切换会话。')

    def _reset_client(self):
        was_claimed = bool(self.claimed_id)
        self.client.close()
        self.claimed_id = ''
        self.client = self.client_factory(self.cfg)
        self.runner.app_server = self.client
        if was_claimed:
            self.emit('released', None)

    def _commit_selection(self, thread_id, name, workspace):
        # Compare with the startup snapshot to avoid overwriting a concurrent config edit.
        current_bytes = self.config_path.read_bytes()
        current = json.loads(current_bytes.decode('utf-8-sig'))
        if current != self.raw:
            raise UserError('助手配置被其他程序改过，请关闭并重新打开会话管家后再选择。')
        state = Path(self.cfg.state_dir)
        state.mkdir(parents=True, exist_ok=True)
        mapping_path = state / 'threads.json'
        old_mapping = mapping_path.read_bytes() if mapping_path.exists() else b'{}'
        mapping = json.loads(old_mapping.decode('utf-8-sig'))
        user_ids = set(current.get('allow_user_ids') or [])
        if current.get('login_user_id'):
            user_ids.add(current['login_user_id'])
        for user_id in user_ids:
            mapping[user_id] = thread_id
        updated = copy.deepcopy(current)
        updated['thread_id'] = thread_id
        updated['codex']['backend'] = 'app-server'
        updated['codex']['workspace'] = workspace
        updated['codex']['command'] = self.cfg.codex.command
        backup = self.config_path.parent / 'backups' / ('manager-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns()))
        backup.mkdir(parents=True)
        (backup / 'config.json').write_bytes(current_bytes)
        (backup / 'threads.json').write_bytes(old_mapping)
        temp_config = self.config_path.with_name('config.manager.tmp')
        temp_mapping = mapping_path.with_name('threads.manager.tmp')
        try:
            temp_config.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temp_mapping.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            if self.config_path.read_bytes() != current_bytes or (mapping_path.exists() and mapping_path.read_bytes() != old_mapping):
                raise UserError('配置或会话映射正在被其他程序修改，请先关闭旧助手。')
            temp_mapping.replace(mapping_path)
            try:
                temp_config.replace(self.config_path)
            except OSError:
                mapping_path.write_bytes(old_mapping)
                raise
        finally:
            for path in (temp_config, temp_mapping):
                path.unlink(missing_ok=True)
        self.raw = updated
        self.cfg.thread_id = thread_id
        self.cfg.codex.workspace = workspace
        self.client.threads = mapping
        self.client.resumed_threads.add(thread_id)
        self.claimed_id = thread_id
        self.current_name = name or thread_id
        self.log('已接管：' + self.current_name)
        self.emit('selection', {'id': thread_id, 'name': self.current_name, 'cwd': workspace})

    def takeover(self, record):
        with self.operation_lock:
            self._require_stopped()
            thread_id = record['id']
            workspace = str(record.get('cwd') or self.cfg.codex.workspace)
            if not Path(workspace).is_dir():
                raise UserError('这条会话的工作目录已不存在：' + workspace)
            roots_cfg = copy.copy(self.cfg.codex)
            roots_cfg.workspace = workspace
            roots = [workspace, *map(str, bridge.resolve_additional_directories(roots_cfg))]
            self._reset_client()
            try:
                self._rpc('thread/resume', {'threadId': thread_id, 'cwd': workspace,
                                           'runtimeWorkspaceRoots': roots, 'excludeTurns': True})
                self._commit_selection(thread_id, record.get('name') or record.get('preview'), workspace)
            except Exception as exc:
                self.client.close()
                if 'already has an active writer' in str(exc):
                    raise UserError('接管失败：这条会话正在被其他 Codex 进程占用。\n\n请完全退出占用它的 Codex 桌面端或旧助手，再点击接管。也可以点击“新建并接管”。\n\n本次没有新建会话，也没有修改默认会话。') from exc
                raise

    def create(self, name, workspace):
        with self.operation_lock:
            self._require_stopped()
            if not name.strip():
                raise UserError('请填写会话名称。')
            workspace = str(Path(workspace).expanduser().resolve())
            if not Path(workspace).is_dir():
                raise UserError('请选择已经存在的工作目录。')
            roots_cfg = copy.copy(self.cfg.codex)
            roots_cfg.workspace = workspace
            roots = [workspace, *map(str, bridge.resolve_additional_directories(roots_cfg))]
            self._reset_client()
            params = {'cwd': workspace, 'approvalPolicy': self.cfg.codex.approval_policy,
                      'sandbox': self.cfg.codex.sandbox, 'runtimeWorkspaceRoots': roots,
                      'ephemeral': False, 'serviceName': 'codex-wechat-manager',
                      'baseInstructions': '你是运行在微信里的 Codex 助手。全程中文，只把最终答复发给微信用户。'}
            if self.cfg.codex.model:
                params['model'] = self.cfg.codex.model
            result = self._rpc('thread/start', params)
            thread_id = result['thread']['id']
            try:
                self._rpc('thread/name/set', {'threadId': thread_id, 'name': name.strip()})
                self._commit_selection(thread_id, name.strip(), workspace)
            except Exception as exc:
                self.client.close()
                self.log('新会话已经创建，但设置未完成：' + thread_id)
                raise UserError('新会话已创建，但设置默认会话失败。原默认配置保留，请刷新列表后再次接管。\n' + str(exc)) from exc
            return thread_id

    def release(self):
        with self.operation_lock:
            self._require_stopped()
            self._reset_client()
            self.log('已释放本程序持有的会话；默认会话配置保留。')
            self.emit('released', None)

    def start_polling(self):
        self._require_stopped()
        if not self.claimed_id:
            raise UserError('请先成功接管一条会话，或新建并接管。')
        if legacy_processes(self.config_path):
            raise UserError('旧版微信助手仍在运行。请手动关闭原窗口后再启动连接，避免重复处理消息。')
        if not self.cfg.token:
            raise UserError('微信尚未登录。请按照 README 的登录步骤完成扫码，再重新打开本程序。')
        if not self.cfg.allow_all and not self.cfg.allow_user_ids:
            raise UserError('配置中没有获准使用助手的微信账号，请检查 config.json 中的 allow_user_ids 或 login_user_id。')
        self.stop_event.clear()
        self.poll_thread = threading.Thread(target=self._poll, daemon=True)
        self.poll_thread.start()

    def _poll(self):
        sync_buf = bridge.load_sync_buf(self.cfg)
        seen, failures = {}, 0
        self.emit('poll', 'running')
        self.log(f'微信连接已启动，收消息请求最多等待 {self.poll_request_timeout} 秒，目标：' + self.current_name)
        try:
            while not self.stop_event.is_set():
                try:
                    response = bridge.api_post(self.cfg.base_url, bridge.EP_GET_UPDATES,
                                               {'get_updates_buf': sync_buf}, self.cfg.token,
                                               timeout=self.poll_request_timeout)
                    if self.stop_event.is_set():
                        break
                    if response.get('ret') not in (None, 0) or response.get('errcode') not in (None, 0):
                        raise UserError(f"微信接口返回错误：ret={response.get('ret')}，errcode={response.get('errcode')}，{response.get('errmsg') or ''}")
                    if failures:
                        self.log('微信收消息连接已恢复。')
                    failures = 0
                    sync_buf = str(response.get('get_updates_buf') or sync_buf)
                    if sync_buf:
                        bridge.save_sync_buf(self.cfg, sync_buf)
                    for message in response.get('msgs') or []:
                        if not isinstance(message, dict):
                            continue
                        self.processing = True
                        self.emit('processing', True)
                        try:
                            bridge.process_message(self.cfg, self.runner, message, seen)
                        except Exception as exc:
                            self.log('处理消息失败：' + str(exc))
                        finally:
                            self.processing = False
                            self.emit('processing', False)
                            self.emit('message_done', None)
                except Exception as exc:
                    if self.stop_event.is_set():
                        break
                    failures += 1
                    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                    if isinstance(reason, TimeoutError):
                        self.log(f'微信收消息超时（已等待 {self.poll_request_timeout} 秒，连续 {failures} 次），正在重试。若持续出现，请检查网络或代理。')
                    elif isinstance(exc, urllib.error.HTTPError):
                        hint = '请检查登录是否有效。' if exc.code in (401, 403) else '正在重试。'
                        self.log(f'微信收消息请求失败：HTTP {exc.code}。{hint}')
                    else:
                        self.log('微信收消息失败：' + str(exc))
                    delay = self.cfg.backoff_delay_seconds if failures >= 3 else self.cfg.retry_delay_seconds
                    self.stop_event.wait(delay)
        finally:
            self.processing = False
            self.log('微信连接已停止。')
            self.emit('poll', 'stopped')

    def stop_polling(self):
        self.stop_event.set()
        self.emit('poll', 'stopping')

    def close(self):
        self.closed = True
        self.stop_event.set()
        self.client.close()
