import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / 'src'
sys.path.insert(0, str(SRC))
import manager_core as core


class FakeClient:
    def __init__(self, cfg):
        import threading
        self.cfg = cfg
        self.lock = threading.Lock()
        self.threads = {}
        self.resumed_threads = set()
        self.closed = False

    def close(self):
        self.closed = True


class ManagerTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.root = Path(self.temp.name)
        assert self.root.resolve().is_relative_to((BASE / 'work').resolve())
        self.addCleanup(self.temp.cleanup)
        (self.root / 'codex.exe').write_bytes(b'fake executable, never run')
        self.config_path = self.root / 'config.json'
        self.config = {'token': 'FAKE_TOKEN', 'login_user_id': 'test-user',
                       'allow_user_ids': ['test-user'], 'allow_all': False,
                       'state_dir': str(self.root / 'state'), 'thread_id': 'old',
                       'codex': {'command': str(self.root / 'codex.exe'),
                                 'workspace': str(self.root), 'backend': 'app-server',
                                 'additional_directories': [], 'sandbox': 'workspace-write'}}
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.mapping_path = self.root / 'state' / 'threads.json'
        self.mapping_path.parent.mkdir()
        self.mapping_path.write_text(json.dumps({'test-user': 'old', 'other-user': 'other'}), encoding='utf-8')
        self.events = []
        self.service = core.SessionService(self.config_path, lambda k, d: self.events.append((k, d)), FakeClient)
        self.addCleanup(self.service.close)

    def test_busy_takeover_preserves_config_and_mapping(self):
        original_config = self.config_path.read_bytes()
        original_mapping = self.mapping_path.read_bytes()
        self.service._rpc = Mock(side_effect=RuntimeError('already has an active writer'))
        with self.assertRaises(core.UserError):
            self.service.takeover({'id': 'busy', 'name': 'Busy', 'cwd': str(self.root)})
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertEqual(self.mapping_path.read_bytes(), original_mapping)
        self.assertEqual(self.service.claimed_id, '')
        self.assertEqual(self.service._rpc.call_count, 1)
        self.assertEqual(self.service._rpc.call_args.args[0], 'thread/resume')

    def test_takeover_commits_only_after_resume_and_keeps_other_accounts(self):
        self.service._rpc = Mock(return_value={})
        self.service.takeover({'id': 'existing', 'name': 'Existing', 'cwd': str(self.root)})
        current = core.read_json(self.config_path)
        self.assertEqual(current['thread_id'], 'existing')
        self.assertEqual(current['token'], self.config['token'])
        self.assertEqual(core.read_json(self.mapping_path), {'test-user': 'existing', 'other-user': 'other'})
        self.assertEqual(self.service.claimed_id, 'existing')
        self.assertTrue(list((self.root / 'backups').glob('manager-*/config.json')))
        self.assertEqual(self.service._rpc.call_count, 1)

    def test_new_chat_has_one_start_one_name_and_shared_runner(self):
        self.service._rpc = Mock(side_effect=[{'thread': {'id': 'new'}}, {}])
        self.assertEqual(self.service.create('My chat', str(self.root)), 'new')
        self.assertEqual([c.args[0] for c in self.service._rpc.call_args_list], ['thread/start', 'thread/name/set'])
        self.assertEqual(core.read_json(self.config_path)['thread_id'], 'new')
        self.assertIs(self.service.runner.app_server, self.service.client)
        self.assertIn('new', self.service.client.resumed_threads)

    def test_external_config_edit_is_not_overwritten(self):
        external = copy.deepcopy(self.config)
        external['prefix'] = 'external edit'
        self.config_path.write_text(json.dumps(external), encoding='utf-8')
        self.service._rpc = Mock(return_value={})
        with self.assertRaises(core.UserError):
            self.service.takeover({'id': 'existing', 'name': 'Existing', 'cwd': str(self.root)})
        self.assertEqual(core.read_json(self.config_path), external)
        self.assertEqual(core.read_json(self.mapping_path)['test-user'], 'old')

    def test_list_paginates_and_pins_sort_first(self):
        self.service.cfg.thread_id = ''
        self.service._rpc = Mock(side_effect=[{'data': [{'id': 'a', 'updatedAt': 5}], 'nextCursor': 'next'},
                                             {'data': [{'id': 'b', 'isPinned': True, 'updatedAt': 1}], 'nextCursor': None}])
        with patch.object(core, 'writer_status', return_value='待接管'):
            self.assertEqual([r['id'] for r in self.service.list_threads()], ['b', 'a'])
        self.assertEqual(self.service._rpc.call_args.args[1]['cursor'], 'next')

    def test_default_without_first_message_remains_visible(self):
        self.service._rpc = Mock(side_effect=[{'data': [], 'nextCursor': None},
                                             {'thread': {'id': 'old', 'name': 'Newly created', 'ephemeral': False}}])
        with patch.object(core, 'writer_status', return_value='待接管'):
            records = self.service.list_threads()
        self.assertEqual(records[0]['id'], 'old')
        self.assertEqual(self.service._rpc.call_args.args[0], 'thread/read')

    def test_no_poll_without_claim_or_when_legacy_helper_runs(self):
        with self.assertRaises(core.UserError):
            self.service.start_polling()
        self.service.claimed_id = 'old'
        with patch.object(core, 'legacy_processes', return_value=[{'ProcessId': 123}]):
            with self.assertRaises(core.UserError):
                self.service.start_polling()
        self.assertIsNone(self.service.poll_thread)

    def test_stop_after_poll_does_not_advance_unprocessed_cursor(self):
        def stop_during_fetch(*_args, **_kwargs):
            self.service.stop_event.set()
            return {'get_updates_buf': 'next', 'msgs': [{'message_id': 1}]}
        with patch.object(core.bridge, 'load_sync_buf', return_value='old'), \
             patch.object(core.bridge, 'api_post', side_effect=stop_during_fetch), \
             patch.object(core.bridge, 'save_sync_buf') as save, \
             patch.object(core.bridge, 'process_message') as process:
            self.service._poll()
            save.assert_not_called()
            process.assert_not_called()

    def test_stop_finishes_already_received_batch(self):
        def stop_after_first(*_args):
            self.service.stop_event.set()
        with patch.object(core.bridge, 'load_sync_buf', return_value='old'), \
             patch.object(core.bridge, 'api_post', return_value={'get_updates_buf': 'next', 'msgs': [{'id': 1}, {'id': 2}]}), \
             patch.object(core.bridge, 'save_sync_buf') as save, \
             patch.object(core.bridge, 'process_message', side_effect=stop_after_first) as process:
            self.service._poll()
            self.assertEqual(process.call_count, 2)
            save.assert_called_once_with(self.service.cfg, 'next')

    def test_ui_log_redacts_token(self):
        self.service.log('credential FAKE_TOKEN')
        self.assertNotIn('FAKE_TOKEN', self.events[-1][1])
        self.assertNotIn('FAKE_TOKEN', self.service.log_file.read_text(encoding='utf-8'))

    def test_long_poll_can_wait_for_server_and_recovers_after_timeout(self):
        replies = iter([TimeoutError('The read operation timed out'),
                        {'ret': 0, 'msgs': []}, {'ret': 0, 'msgs': []}])
        def fetch(*args, **kwargs):
            # An idle server may wait well beyond the old 10s deadline.
            self.assertGreaterEqual(kwargs['timeout'], 45)
            result = next(replies)
            if isinstance(result, Exception):
                raise result
            if post.call_count == 3:
                self.service.stop_event.set()
            return result
        with patch.object(core.bridge, 'load_sync_buf', return_value='old'), \
             patch.object(core.bridge, 'api_post', side_effect=fetch) as post, \
             patch.object(self.service.stop_event, 'wait', return_value=False):
            self.service._poll()
        logs = '\n'.join(str(data) for kind, data in self.events if kind == 'log')
        self.assertIn('微信收消息超时', logs)
        self.assertIn('微信收消息连接已恢复', logs)
        self.assertNotIn('微信连接错误', logs)

    def test_auth_error_keeps_code_and_does_not_process_messages(self):
        def fail_and_stop(_delay):
            self.service.stop_event.set()
        error = core.urllib.error.HTTPError('https://example.invalid', 401, 'Unauthorized', {}, None)
        with patch.object(core.bridge, 'load_sync_buf', return_value='old'), \
             patch.object(core.bridge, 'api_post', side_effect=error), \
             patch.object(core.bridge, 'process_message') as process, \
             patch.object(self.service.stop_event, 'wait', side_effect=fail_and_stop):
            self.service._poll()
            process.assert_not_called()
        self.assertTrue(any('HTTP 401' in str(data) for kind, data in self.events if kind == 'log'))

    def test_stop_during_timeout_does_not_report_connection_failure(self):
        def stop_then_timeout(*args, **kwargs):
            self.service.stop_event.set()
            raise TimeoutError('The read operation timed out')
        with patch.object(core.bridge, 'load_sync_buf', return_value='old'), \
             patch.object(core.bridge, 'api_post', side_effect=stop_then_timeout):
            self.service._poll()
        self.assertFalse(any('超时' in str(data) for kind, data in self.events if kind == 'log'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
