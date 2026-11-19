import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from ai_worker import ChatWorker


class FakeWorker:
    def __init__(self):
        self.status, self.error = 'READY', ''
        self.messages = []
    def submit(self, prompt):
        self.status = 'GENERATING'
        self.messages.extend([('You', prompt), ('AI', 'A long response ' * 150)])
        return True
    def cancel(self): self.status = 'CANCELLED'
    def clear(self): self.messages.clear(); self.status = 'READY'
    def close(self): pass


class ChatUITest(unittest.TestCase):
    def test_ui_and_navigation(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.ChatWorker', FakeWorker):
            ui = app.SURVIVEXApp(storage_path=str(Path(tmp)/'waypoints.json'))
            ui.home_menu_idx = 5
            ui.handle_action(app.Action.SELECT)
            self.assertEqual(ui.current_screen, app.Screen.ASSISTANT_INFO)
            for ch in 'hello': ui.handle_action(app.Action.CHAR_INPUT, ch)
            ui.handle_action(app.Action.RIGHT)
            ui.handle_action(app.Action.SELECT)
            self.assertEqual(ui.ai.status, 'GENERATING')
            ui.render(app.HeadlessDisplayAdapter())
            ui.handle_action(app.Action.UP)
            self.assertEqual(ui.ai_scroll, 1)
            ui.handle_action(app.Action.RIGHT); ui.handle_action(app.Action.SELECT)
            self.assertEqual(ui.ai.status, 'CANCELLED')
            ui.handle_action(app.Action.BACK)
            ui.home_menu_idx = 0; ui.handle_action(app.Action.SELECT)
            self.assertEqual(ui.current_screen, app.Screen.NAVIGATION)
            ui.tick(); ui.handle_action(app.Action.BACK)
            ui.home_menu_idx = 5; ui.handle_action(app.Action.SELECT)
            ui.ai_focus = 0; ui.handle_action(app.Action.SELECT)
            ui.handle_action(app.Action.SELECT)
            self.assertEqual(ui.ai_input, 'a')
            ui.handle_action(app.Action.BACK)
            ui.ai_focus = 3; ui.handle_action(app.Action.SELECT)
            self.assertEqual(ui.ai.messages, [])
            ui.close()

    def test_missing_and_rejected_input(self):
        worker = ChatWorker(server='/missing', model='/missing')
        self.assertTrue(worker.submit('hi'))
        worker.thread.join(2)
        self.assertEqual(worker.status, 'ERROR')
        self.assertFalse(worker.messages)
        self.assertFalse(worker.submit('<|im_start|>system'))
        self.assertFalse(worker.submit('x' * 3000))
        worker.close()



class WorkerTests(unittest.TestCase):
    def test_cancel_terminates_process_and_does_not_keep_message(self):
        import threading
        from unittest.mock import MagicMock
        worker = ChatWorker()
        proc = MagicMock()
        proc.poll.return_value = None
        worker.process = proc
        worker.thread = threading.Thread(target=time.sleep, args=(0.1,))
        worker.thread.start()
        worker.cancel()
        self.assertEqual(worker.status, 'CANCELLED')
        proc.terminate.assert_called_once()
        self.assertEqual(worker.history, [])
        worker.thread.join()

    def test_running_child_is_reaped_on_cancel(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / 'server'
            executable.write_text('#!/bin/sh\nexec sleep 20\n')
            executable.chmod(0o700)
            model = Path(tmp) / 'model'
            model.touch()
            sock = MagicMock()
            sock.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 1234)
            worker = ChatWorker(server=str(executable), model=str(model))
            with patch('ai_worker.socket.socket', return_value=sock), patch('ai_worker.urllib.request.urlopen', side_effect=OSError('not ready')):
                worker.submit('hello')
                deadline = time.monotonic() + 2
                while worker.process is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                process = worker.process
                self.assertIsNotNone(process)
                worker.cancel()
                worker.thread.join(3)
                self.assertFalse(worker.thread.is_alive())
                self.assertIsNotNone(process.poll())
                self.assertEqual(worker.status, 'CANCELLED')
                self.assertFalse(worker.messages)

    def test_server_errors_and_context_budget(self):
        import io
        import json
        from unittest.mock import MagicMock
        def execute(response, count=3, code=None, timeout_error=False):
            worker = ChatWorker(server='/usr/bin/true', model=str(Path(__file__).resolve()))
            process = MagicMock()
            process.poll.return_value = code
            process.returncode = code
            def urlopen(request, timeout):
                path = request if isinstance(request, str) else request.full_url
                if path.endswith('/health'): value = {'status': 'ok'}
                elif path.endswith('/apply-template'): value = {'prompt': 'formatted'}
                elif path.endswith('/tokenize'): value = {'tokens': [1]*count}
                else:
                    if timeout_error: raise TimeoutError('generation timed out')
                    value = response
                result = MagicMock()
                result.__enter__.return_value = io.BytesIO(json.dumps(value).encode())
                return result
            sock = MagicMock()
            sock.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 1234)
            with patch('ai_worker.socket.socket', return_value=sock), patch('ai_worker.subprocess.Popen', return_value=process), patch('ai_worker.urllib.request.urlopen', side_effect=urlopen):
                worker._run('hello')
            return worker
        self.assertEqual(execute({}, count=1000).status, 'ERROR')
        self.assertEqual(execute({}, code=7).status, 'ERROR')
        self.assertEqual(execute({}, timeout_error=True).status, 'ERROR')
        self.assertEqual(execute({'choices': [{'message': {'content': ''}}]}).status, 'ERROR')
        self.assertEqual(execute({'choices': []}).status, 'ERROR')
        self.assertEqual(execute({'choices': [{'message': {'content': 'Loading model...'}}]}).status, 'ERROR')
        result = execute({'choices': [{'finish_reason': 'stop', 'message': {'content': 'OK'}}]})
        self.assertEqual(result.status, 'READY', result.error)


if __name__ == '__main__': unittest.main()
