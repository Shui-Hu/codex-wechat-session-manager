# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from manager_core import DEFAULT_CONFIG, InstanceGuard, SessionService, UserError


class NewChatDialog(tk.Toplevel):
    def __init__(self, parent, workspace):
        super().__init__(parent)
        self.title('新建并接管会话')
        self.result = None
        self.resizable(False, False)
        self.transient(parent)
        frame = ttk.Frame(self, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='会话名称', font=('Microsoft YaHei UI', 11, 'bold')).grid(row=0, column=0, sticky='w')
        self.name = tk.StringVar(value='微信聊天 ' + datetime.now().strftime('%m-%d %H:%M'))
        ttk.Entry(frame, textvariable=self.name, width=52).grid(row=1, column=0, columnspan=2, pady=(8, 20), sticky='ew')
        ttk.Label(frame, text='工作目录', font=('Microsoft YaHei UI', 11, 'bold')).grid(row=2, column=0, sticky='w')
        self.workspace = tk.StringVar(value=workspace)
        ttk.Entry(frame, textvariable=self.workspace, width=52).grid(row=3, column=0, pady=(8, 20), sticky='ew')
        ttk.Button(frame, text='浏览…', command=self.browse).grid(row=3, column=1, padx=(10, 0), pady=(8, 20))
        ttk.Label(frame, text='创建一条独立会话，使用当前微信登录与权限配置。', foreground='#64748b').grid(row=4, column=0, columnspan=2, sticky='w')
        ttk.Button(frame, text='创建并接管', style='Accent.TButton', command=self.confirm).grid(row=5, column=0, columnspan=2, sticky='e', pady=(24, 0))
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self.bind('<Return>', lambda _: self.confirm())
        self.grab_set()
        self.after(30, self.center)

    def center(self):
        self.update_idletasks()
        x = self.master.winfo_x() + (self.master.winfo_width() - self.winfo_width()) // 2
        y = self.master.winfo_y() + (self.master.winfo_height() - self.winfo_height()) // 2
        self.geometry(f'+{max(x, 0)}+{max(y, 0)}')

    def browse(self):
        value = filedialog.askdirectory(parent=self, initialdir=self.workspace.get(), title='选择会话工作目录')
        if value:
            self.workspace.set(value)

    def confirm(self):
        if not self.name.get().strip() or not Path(self.workspace.get()).is_dir():
            messagebox.showerror('请检查输入', '请填写会话名称，并选择已经存在的工作目录。', parent=self)
            return
        self.result = (self.name.get().strip(), self.workspace.get())
        self.destroy()


class ManagerApp:
    def __init__(self, root, config_path, preview=None):
        self.root = root
        self.preview = preview
        self.events = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='session-ui')
        self.jobs = {}
        self.counter = 0
        self.records = {}
        self.poll_state = 'stopped'
        self.busy = False
        self.last_refresh = 0.0
        self.closing = False
        self.guard = InstanceGuard(config_path)
        self.service = SessionService(config_path, lambda kind, data: self.events.put((kind, data)))
        self.build_ui()
        self.root.after(100, self.drain)
        self.root.after(150, self.refresh)
        self.root.after(30000, self.auto_refresh)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        if preview:
            self.preview_deadline = time.time() + 50
            self.root.after(1500, self.capture_preview)

    def build_ui(self):
        r = self.root
        r.title('Codex 微信会话管家 · 公开展示版')
        width = min(1220, r.winfo_screenwidth() - 80)
        height = min(820, r.winfo_screenheight() - 110)
        r.geometry(f'{width}x{height}+30+30')
        r.minsize(1080, 710)
        r.configure(background='#f3f6fa')
        try:
            asset = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'app.ico'
            if asset.exists():
                r.iconbitmap(str(asset))
        except tk.TclError:
            pass
        r.option_add('*Font', ('Microsoft YaHei UI', 10))
        s = ttk.Style(r)
        s.theme_use('clam')
        s.configure('TFrame', background='#f3f6fa')
        s.configure('White.TFrame', background='#ffffff')
        s.configure('TLabel', background='#f3f6fa', foreground='#172b43')
        s.configure('White.TLabel', background='#ffffff', foreground='#172b43')
        s.configure('TButton', padding=(14, 8), borderwidth=0, background='#e6edf5', foreground='#243c55')
        s.map('TButton', background=[('active', '#d5e2f0'), ('disabled', '#edf1f5')], foreground=[('disabled', '#94a3b8')])
        s.configure('Accent.TButton', background='#137c66', foreground='white')
        s.map('Accent.TButton', background=[('active', '#0f6b58'), ('disabled', '#c4d6d0')], foreground=[('disabled', '#6c837c')])
        s.configure('Treeview', rowheight=36, borderwidth=0, background='white', fieldbackground='white', foreground='#24384c')
        s.configure('Treeview.Heading', padding=(10, 11), background='#e9eff5', foreground='#3a5168', font=('Microsoft YaHei UI', 10, 'bold'))
        s.map('Treeview', background=[('selected', '#dceee9')], foreground=[('selected', '#153f35')])
        s.configure('TPanedwindow', background='#dce3eb')
        header = ttk.Frame(r, padding=(26, 20, 26, 16))
        header.pack(fill='x')
        ttk.Label(header, text='Codex 微信会话管家', font=('Microsoft YaHei UI', 21, 'bold')).pack(anchor='w')
        ttk.Label(header, text='选择会话 · 接管后台 · 在微信里继续聊', foreground='#64748b').pack(anchor='w', pady=(5, 0))
        toolbar = ttk.Frame(r, padding=(26, 0, 26, 14))
        toolbar.pack(fill='x')
        self.search = tk.StringVar()
        entry = ttk.Entry(toolbar, textvariable=self.search, width=44)
        entry.pack(side='left')
        ttk.Label(toolbar, text='搜索名称 / ID / 工作目录', foreground='#64748b').pack(side='left', padx=12)
        self.search.trace_add('write', lambda *_: self.render_list())
        self.refresh_button = ttk.Button(toolbar, text='刷新会话', command=self.refresh)
        self.refresh_button.pack(side='right')
        self.count_label = ttk.Label(toolbar, text='正在读取…', foreground='#64748b')
        self.count_label.pack(side='right', padx=16)
        main = ttk.Panedwindow(r, orient='horizontal')
        main.pack(fill='both', expand=True, padx=26)
        list_frame = ttk.Frame(main, style='White.TFrame')
        main.add(list_frame, weight=3)
        self.tree = ttk.Treeview(list_frame, columns=('name', 'status', 'updated'), show='headings', selectmode='browse')
        for column, title, width in [('name', '会话名称', 320), ('status', '接管状态', 126), ('updated', '最近更新', 118)]:
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=80, stretch=column == 'name')
        scroll = ttk.Scrollbar(list_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self.on_select)
        self.tree.tag_configure('pinned', font=('Microsoft YaHei UI', 10, 'bold'))
        details = ttk.Frame(main, style='White.TFrame', padding=20)
        main.add(details, weight=2)
        self.selected_name = tk.StringVar(value='选一条会话')
        ttk.Label(details, textvariable=self.selected_name, font=('Microsoft YaHei UI', 13, 'bold'),
                  style='White.TLabel', wraplength=370).pack(anchor='w')
        self.metadata = tk.StringVar(value='可以查看最近消息，再选择接管。')
        ttk.Label(details, textvariable=self.metadata, style='White.TLabel', wraplength=370, foreground='#64748b').pack(anchor='w', pady=(12, 18))
        buttons = ttk.Frame(details, style='White.TFrame')
        buttons.pack(fill='x')
        self.take_button = ttk.Button(buttons, text='接管选中会话', style='Accent.TButton', command=self.takeover)
        self.take_button.pack(side='left')
        self.new_button = ttk.Button(buttons, text='新建并接管', command=self.new_chat)
        self.new_button.pack(side='left', padx=(10, 0))
        ttk.Label(details, text='最近文字消息', style='White.TLabel', font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', pady=(22, 8))
        history_frame = ttk.Frame(details, style='White.TFrame')
        history_frame.pack(fill='both', expand=True)
        self.history = tk.Text(history_frame, wrap='word', background='#f8fafc', foreground='#334155',
                               relief='flat', padx=12, pady=10, font=('Microsoft YaHei UI', 9), state='disabled')
        hscroll = ttk.Scrollbar(history_frame, command=self.history.yview)
        self.history.configure(yscrollcommand=hscroll.set)
        self.history.pack(side='left', fill='both', expand=True)
        hscroll.pack(side='right', fill='y')
        connection = ttk.Frame(r, padding=(26, 16, 26, 12))
        connection.pack(fill='x')
        self.connection_text = tk.StringVar(value='连接未启动 · 请先接管或新建会话')
        ttk.Label(connection, textvariable=self.connection_text, font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left')
        self.start_button = ttk.Button(connection, text='启动连接', style='Accent.TButton', command=self.start)
        self.start_button.pack(side='right')
        self.stop_button = ttk.Button(connection, text='停止连接', command=self.stop)
        self.stop_button.pack(side='right', padx=8)
        self.release_button = ttk.Button(connection, text='取消接管', command=self.release)
        self.release_button.pack(side='right')
        log_frame = ttk.Frame(r, padding=(26, 0, 26, 8))
        log_frame.pack(fill='x')
        self.logs = tk.Text(log_frame, height=5, wrap='word', background='#172b43', foreground='#d8e7f2',
                            font=('Consolas', 9), relief='flat', padx=12, pady=10, state='disabled')
        self.logs.pack(fill='x')
        self.status = tk.StringVar(value='自动刷新每 30 秒。打开程序不会自动启动连接。')
        footer = ttk.Frame(r, padding=(26, 0, 26, 14))
        footer.pack(fill='x')
        ttk.Label(footer, textvariable=self.status, foreground='#64748b').pack(anchor='w')
        # Reserve controls/log space before allowing the main pane to expand.
        for widget in (main, connection, log_frame, footer):
            widget.pack_forget()
        footer.pack(side='bottom', fill='x')
        log_frame.pack(side='bottom', fill='x')
        connection.pack(side='bottom', fill='x')
        main.pack(side='top', fill='both', expand=True, padx=26)
        r.after(100, lambda: main.sashpos(0, int((r.winfo_width() - 52) * 0.53)))
        self.update_buttons()

    def set_text(self, widget, value):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.configure(state='disabled')

    def job(self, label, function, callback=None):
        if self.closing:
            return
        self.counter += 1
        token = self.counter
        self.jobs[token] = callback
        self.busy = True
        self.status.set(label)
        self.update_buttons()
        def worker():
            try:
                result = function()
                self.events.put(('job_done', (token, result, None)))
            except Exception as exc:
                self.events.put(('job_done', (token, None, str(exc))))
        self.pool.submit(worker)

    def drain(self):
        if self.closing:
            return
        try:
            for _ in range(150):
                kind, data = self.events.get_nowait()
                if kind == 'log':
                    self.logs.configure(state='normal')
                    self.logs.insert('end', data + '\n')
                    if int(self.logs.index('end-1c').split('.')[0]) > 600:
                        self.logs.delete('1.0', '200.0')
                    self.logs.see('end')
                    self.logs.configure(state='disabled')
                elif kind == 'job_done':
                    token, result, error = data
                    callback = self.jobs.pop(token, None)
                    self.busy = bool(self.jobs)
                    if error:
                        self.status.set('操作未完成：' + error.split('\n')[0])
                        if not self.preview:
                            messagebox.showerror('操作未完成', error, parent=self.root)
                    elif callback:
                        callback(result)
                    self.update_buttons()
                elif kind == 'selection':
                    self.connection_text.set('已接管：' + data['name'][:50] + ' · 连接未启动')
                    self.status.set('接管成功。点击“启动连接”开始转发。')
                elif kind == 'released':
                    self.connection_text.set('已取消接管 · 连接未启动')
                elif kind == 'poll':
                    self.poll_state = data
                    labels = {'running': '连接运行中：' + self.service.current_name[:50],
                              'stopping': '正在停止，等待当前消息处理完成…',
                              'stopped': '连接已停止 · 会话仍由本程序接管'}
                    self.connection_text.set(labels[data])
                    self.update_buttons()
                elif kind == 'processing':
                    self.status.set('正在处理微信消息…' if data else '已完成消息处理。')
        except queue.Empty:
            pass
        self.root.after(100, self.drain)

    def update_buttons(self):
        stopped = self.poll_state == 'stopped'
        selected = bool(self.tree.selection()) if hasattr(self, 'tree') else False
        active = not self.busy and stopped
        for button, enabled in [(self.take_button, active and selected), (self.new_button, active),
                                (self.release_button, active and bool(self.service.claimed_id)),
                                (self.start_button, active and bool(self.service.claimed_id)),
                                (self.stop_button, self.poll_state == 'running'),
                                (self.refresh_button, not self.busy)]:
            button.configure(state='normal' if enabled else 'disabled')

    def refresh(self):
        if self.busy or self.service.processing:
            return
        self.job('正在读取 Codex 会话…', self.service.list_threads, self.loaded)

    def loaded(self, records):
        self.records = {r['id']: r for r in records}
        self.last_refresh = time.time()
        self.render_list()
        self.status.set(f'已读取 {len(records)} 条会话。占用状态是本机锁检测，接管时会再次确认。')
        default = self.service.cfg.thread_id
        if not self.tree.selection() and self.tree.exists(default):
            self.tree.selection_set(default)
            self.tree.see(default)

    def render_list(self):
        if not hasattr(self, 'tree'):
            return
        previous = self.tree.selection()
        query = self.search.get().strip().casefold()
        self.tree.delete(*self.tree.get_children())
        count = 0
        for thread_id, record in self.records.items():
            title = record.get('name') or record.get('preview') or '未命名会话'
            haystack = ' '.join([title, thread_id, str(record.get('cwd') or '')]).casefold()
            if query and query not in haystack:
                continue
            timestamp = record.get('updatedAt')
            try:
                updated = datetime.fromtimestamp(float(timestamp)).strftime('%m-%d %H:%M') if timestamp else ''
            except (ValueError, OSError):
                updated = ''
            name = ('★ ' if record.get('isPinned') else '') + title.replace('\n', ' ')[:100]
            if thread_id == self.service.cfg.thread_id:
                name = '● ' + name
            status = '本程序已接管' if thread_id == self.service.claimed_id else record.get('managerStatus', '待接管')
            self.tree.insert('', 'end', iid=thread_id, values=(name, status, updated), tags=('pinned',) if record.get('isPinned') else ())
            count += 1
        self.count_label.configure(text=f'{count} / {len(self.records)} 条')
        if previous and self.tree.exists(previous[0]):
            self.tree.selection_set(previous)
        self.update_buttons()

    def on_select(self, _=None):
        choice = self.tree.selection()
        if not choice:
            return
        record = self.records[choice[0]]
        self.selected_name.set((record.get('name') or record.get('preview') or '未命名会话')[:150])
        self.metadata.set(f"{record.get('managerStatus', '待接管')}\n工作目录：{record.get('cwd') or '未记录'}\nID：{record['id']}")
        self.update_buttons()
        if self.busy or self.service.processing:
            self.set_text(self.history, '当前操作完成后，再点击此会话查看最近消息。')
            return
        thread_id = record['id']
        self.set_text(self.history, '正在读取最近消息…')
        def show(value):
            if self.tree.selection() == (thread_id,):
                self.set_text(self.history, value)
                self.status.set('历史预览完成，尚未接管。')
        self.job('正在读取最近消息…', lambda: self.service.inspect(thread_id), show)

    def takeover(self):
        choice = self.tree.selection()
        if choice:
            self.job('正在尝试接管，成功后才保存默认会话…', lambda: self.service.takeover(self.records[choice[0]]), lambda _: self.refresh())

    def new_chat(self):
        dialog = NewChatDialog(self.root, self.service.cfg.codex.workspace)
        self.root.wait_window(dialog)
        if dialog.result:
            name, workspace = dialog.result
            self.job('正在新建并接管…', lambda: self.service.create(name, workspace), lambda _: self.refresh())

    def release(self):
        self.job('正在取消接管…', self.service.release, lambda _: self.refresh())

    def start(self):
        self.job('正在启动连接…', self.service.start_polling)

    def stop(self):
        self.service.stop_polling()

    def auto_refresh(self):
        if not self.closing:
            self.refresh()
            self.root.after(30000, self.auto_refresh)

    def capture_preview(self):
        if self.busy and time.time() < self.preview_deadline:
            self.root.after(600, self.capture_preview)
            return
        try:
            from PIL import ImageGrab
            self.root.update_idletasks()
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            image = ImageGrab.grab(bbox=(x, y, x + self.root.winfo_width(), y + self.root.winfo_height()))
            image.save(self.preview)
        finally:
            self.close(force=True)

    def close(self, force=False):
        if not force and self.service.processing:
            if not messagebox.askyesno('关闭程序', '正在处理微信消息，关闭程序会中断这次响应。\n是否仍然关闭？', parent=self.root):
                return
        self.closing = True
        self.service.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.guard.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--preview', type=Path)
    args = parser.parse_args()
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        ManagerApp(root, args.config, args.preview)
    except Exception as exc:
        root.withdraw()
        messagebox.showerror('无法启动会话管家', str(exc), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
