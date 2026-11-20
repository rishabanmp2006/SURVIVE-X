"""Bounded, cancellable local llama.cpp chat worker."""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from chat import DEFAULT_MODEL_PATH, DEFAULT_SYSTEM_PROMPT, extract_explicit_constraints

SERVER = os.environ.get('SURVIVEX_LLAMA_SERVER') or shutil.which('llama-server') or '/opt/homebrew/bin/llama-server'
MODEL = os.environ.get('SURVIVEX_MODEL', DEFAULT_MODEL_PATH)


class ChatWorker:
    def __init__(self, server=SERVER, model=MODEL, *, persistent=None, idle_timeout=None):
        self.server, self.model = shutil.which(server) or server, model
        self.persistent = (os.environ.get("SURVIVEX_AI_PERSISTENT", "1") != "0") if persistent is None else persistent
        self.idle_timeout = float(os.environ.get("SURVIVEX_AI_IDLE_SECONDS", "180")) if idle_timeout is None else idle_timeout
        if self.idle_timeout <= 0:
            raise ValueError("Idle timeout must be positive")
        self.base = None
        self.idle_timer = None
        self.closed = False
        self.status = 'READY'
        self.error = ''
        self.history = []
        self.constraints = []
        self.messages = []
        self.process = None
        self.thread = None
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.started = 0
        self.startup_seconds = None
        self.response_seconds = None

    def submit(self, prompt):
        prompt = prompt.strip()
        if self.closed or (self.thread and self.thread.is_alive()):
            return False
        with self.lock:
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
        if not prompt or len(prompt.encode('utf-8')) > 2048 or '<|' in prompt or '|>' in prompt:
            self.status, self.error = 'ERROR', 'Invalid or oversized input'
            return False
        if prompt.startswith('/forget '):
            target = prompt[8:].strip().casefold()
            with self.lock:
                matches = [c for c in self.constraints if c.casefold() == target]
                if not matches:
                    self.status, self.error = 'ERROR', 'Constraint not found; use exact wording'
                    return False
                self.constraints = [c for c in self.constraints if c.casefold() != target]
                self.history = [h for h in self.history if target not in h.casefold()]
                self.messages = (self.messages + [('You', prompt), ('AI', 'Constraint removed.')])[-12:]
                self.status, self.error = 'READY', ''
            return True
        self.cancelled.clear()
        self.error = ''
        self.status = 'LOADING'
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, args=(prompt,), daemon=True)
        self.thread.start()
        return True

    def cancel(self):
        if self.thread and self.thread.is_alive():
            self.cancelled.set()
            with self.lock:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
            self.status = 'CANCELLED'

    def clear(self):
        self.cancel()
        with self.lock:
            self.history.clear()
            self.constraints.clear()
            self.messages.clear()
            self.error = ''
        if not self.thread or not self.thread.is_alive():
            self.status = 'READY'

    @staticmethod
    def _reap(proc):
        if proc:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

    def _stop_server(self):
        with self.lock:
            proc = self.process
            self.process = None
            self.base = None
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
        self._reap(proc)

    def _expire(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            proc = self.process
            self.process = None
            self.base = None
            self.idle_timer = None
        self._reap(proc)

    def close(self):
        self.closed = True
        self.cancel()
        if self.thread:
            self.thread.join(timeout=3)
        self._stop_server()

    def _run(self, prompt):
        proc = None
        try:
            if not Path(self.model).is_file() or not os.access(self.server, os.X_OK):
                raise RuntimeError('Model or llama-server missing')
            with self.lock:
                proc, base = self.process, self.base
            if proc is not None and proc.poll() is not None:
                self._stop_server()
                proc = None
            if proc is None:
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                base = f'http://127.0.0.1:{port}'
                with self.lock:
                    if self.cancelled.is_set() or self.closed:
                        return
                    proc = subprocess.Popen([self.server, '-m', self.model, '-ngl', '0', '-t', '4',
                        '-c', '1024', '--parallel', '1', '--host', '127.0.0.1', '--port', str(port),
                        '--reasoning', 'off'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
                    self.process, self.base = proc, base
                deadline = time.monotonic() + 60
                while True:
                    if self.cancelled.is_set():
                        return
                    if proc.poll() is not None:
                        raise RuntimeError(f'Model startup failed (exit {proc.returncode})')
                    if time.monotonic() > deadline:
                        raise RuntimeError('Model startup timed out')
                    try:
                        with urllib.request.urlopen(base + '/health', timeout=0.5) as response:
                            if json.load(response).get('status') == 'ok':
                                break
                    except (OSError, ValueError):
                        time.sleep(0.1)
            self.startup_seconds = time.monotonic() - self.started
            history = list(self.history)
            constraints = list(self.constraints)
            for constraint in extract_explicit_constraints(prompt):
                if constraint not in constraints:
                    constraints.append(constraint)
            if len(constraints) > 32:
                raise RuntimeError('Too many constraints; remove or clear some')
            def post(path, data, timeout=10):
                request = urllib.request.Request(base + path, data=json.dumps(data).encode(),
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read(65537)
                if len(raw) > 65536:
                    raise RuntimeError('Model output too large')
                return json.loads(raw)
            while True:
                context = '\n'.join('- ' + h for h in history)
                confirmed = 'Confirmed constraints (remove with /forget exact wording):\n' + '\n'.join('- ' + c for c in constraints) + '\n\n' if constraints else ''
                user = confirmed + ('Previous user context (may be superseded):\n' + context + '\n\n' if context else '') + 'Current request:\n' + prompt
                messages = [{'role': 'system', 'content': DEFAULT_SYSTEM_PROMPT}, {'role': 'user', 'content': user}]
                template = post('/apply-template', {'messages': messages, 'chat_template_kwargs': {'enable_thinking': False}})['prompt']
                tokens = post('/tokenize', {'content': template, 'add_special': True, 'parse_special': True})['tokens']
                if len(tokens) <= 1024 - 128 - 16:
                    break
                if not history:
                    raise RuntimeError('Input exceeds model context budget')
                history.pop(0)
            if self.cancelled.is_set():
                return
            self.status = 'GENERATING'
            result = post('/v1/chat/completions', {'messages': messages, 'max_tokens': 128,
                'temperature': 0.2, 'top_p': 0.9, 'stream': False,
                'chat_template_kwargs': {'enable_thinking': False}}, timeout=120)
            if self.cancelled.is_set():
                return
            if proc.poll() is not None and proc.returncode != 0:
                raise RuntimeError(f'Model exited ({proc.returncode})')
            choice = result['choices'][0]
            finish_reason = choice.get('finish_reason')
            if finish_reason not in ('stop', 'length'):
                raise RuntimeError('Missing or unsupported completion reason')
            text = choice['message']['content']
            if not isinstance(text, str) or not text.strip() or re.search(r'\x1b|<\|(?:im_|endoftext)|loading model|llama_model_loader:', text, re.I):
                raise RuntimeError('Empty or malformed model response')
            text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32).strip()
            if not text:
                raise RuntimeError('Empty model response')
            if finish_reason == 'length':
                marker = '\n[INCOMPLETE: token limit]'
                text = text[:8192-len(marker)] + marker
            self.response_seconds = time.monotonic() - self.started
            with self.lock:
                if self.cancelled.is_set():
                    return
                self.history = (history + [prompt])[-3:]
                self.constraints = constraints
                self.messages = (self.messages + [('You', prompt), ('AI', text[:8192])])[-12:]
                self.status = 'READY'
        except Exception as exc:
            if not self.cancelled.is_set():
                self.error = str(exc)[:160] or type(exc).__name__
                self.status = 'ERROR'
        finally:
            if self.cancelled.is_set() or self.status == 'ERROR' or not self.persistent or self.closed:
                self._stop_server()
            elif proc:
                with self.lock:
                    self.idle_timer = threading.Timer(self.idle_timeout, self._expire)
                    self.idle_timer.daemon = True
                    self.idle_timer.start()
