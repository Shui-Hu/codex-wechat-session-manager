import json
import shutil
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk

base = Path(__file__).resolve().parent.parent
source = base / 'src'
sys.path.insert(0, str(source))
import manager_core as core
import session_manager as gui

codex = os.environ.get('CODEX_TEST_COMMAND') or shutil.which('codex')
if not codex:
    raise SystemExit('Set CODEX_TEST_COMMAND to the installed codex executable first.')
(base / 'work').mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(dir=base / 'work') as directory:
    workspace = Path(directory).resolve()
    assert workspace.is_relative_to((base / 'work').resolve())
    codex_home = workspace / 'codex-home'
    codex_home.mkdir()
    os.environ['CODEX_HOME'] = str(codex_home)
    config = workspace / 'config.json'
    config.write_text(json.dumps({'thread_id': '', 'token': 'FAKE_TOKEN_FOR_TEST',
        'login_user_id': 'test-user', 'allow_user_ids': ['test-user'],
        'state_dir': str(workspace / 'state'),
        'codex': {'command': codex, 'workspace': str(workspace), 'sandbox': 'read-only',
                  'approval_policy': 'never', 'backend': 'app-server'}}), encoding='utf-8')
    root = tk.Tk()
    app = gui.ManagerApp(root, config)
    errors = []
    gui.messagebox.showerror = lambda _title, text, **_kwargs: errors.append(text)
    def pump_until(predicate, seconds=30):
        deadline = time.time() + seconds
        while time.time() < deadline:
            root.update()
            if errors:
                raise AssertionError(errors)
            if predicate():
                return
            time.sleep(0.04)
        raise AssertionError('UI operation timed out: ' + app.status.get())
    try:
        pump_until(lambda: app.last_refresh > 0 and not app.busy)
        def fill_dialog():
            for child in root.winfo_children():
                if isinstance(child, gui.NewChatDialog):
                    child.name.set('会话管家集成验收')
                    child.confirm()
                    return
            root.after(30, fill_dialog)
        root.after(80, fill_dialog)
        app.new_chat()
        pump_until(lambda: bool(app.service.claimed_id) and not app.busy and bool(app.records))
        created_id = app.service.claimed_id
        assert app.tree.exists(created_id)
        assert str(app.start_button['state']) == 'normal'
        assert '会话管家集成验收' in app.connection_text.get()
        # Exercise Start/Stop with a mocked empty iLink inbox; no network or sends.
        core.legacy_processes = lambda _: []
        def empty_inbox(*_args, **_kwargs):
            time.sleep(0.08)
            return {'ret': 0, 'get_updates_buf': 'test-cursor', 'msgs': []}
        core.bridge.api_post = empty_inbox
        app.start()
        pump_until(lambda: app.poll_state == 'running' and not app.busy)
        assert str(app.take_button['state']) == 'disabled'
        assert str(app.stop_button['state']) == 'normal'
        app.stop()
        pump_until(lambda: app.poll_state == 'stopped' and not app.service.poll_thread.is_alive())
        app.release()
        pump_until(lambda: not app.service.claimed_id and not app.busy)
        app.tree.selection_set(created_id)
        root.update()
        pump_until(lambda: not app.busy)
        app.takeover()
        pump_until(lambda: app.service.claimed_id == created_id and not app.busy)
        assert app.service.runner.app_server is app.service.client
        print(json.dumps({'real_window_buttons_checked': True,
                          'new_dialog_create_and_list_checked': True,
                          'start_stop_checked_with_mock_inbox': True,
                          'release_and_resume_same_thread_checked': True,
                          'shared_backend_checked': True,
                          'real_wechat_messages_sent': 0,
                          'real_model_turns_started': 0}))
    finally:
        app.close(force=True)
