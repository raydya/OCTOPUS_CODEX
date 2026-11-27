"""Host-owned TLS and DMS login sessions, separate from model tools and query execution.

The bridge must authenticate an enabled administrator and consume its one-shot
approval before calling start. This module never accepts commands or endpoints.
Authorization URLs and user codes stay in memory and are returned only to that
approved actor; subprocess output and provider credentials are never returned.
"""
from __future__ import annotations

from collections import OrderedDict
import codecs
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlsplit


MAX_SESSIONS = 64
MAX_AUTH_SECONDS = 300
MAX_OUTPUT_BYTES = 16384
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_USER_CODE = re.compile(r"[A-Za-z0-9-]{4,64}\Z")
_REPLACE_PROMPT = re.compile(r'Profile "[^"\r\n\x00-\x1f]{1,128}" is bound to a different login session\. Replace it\? \[y/N\]: \Z')
_TERMINAL = frozenset({"succeeded", "failed", "expired", "cancelled"})
_DMS_PROFILE = "company-cli-user"
_DMS_LINK = re.compile(rb"(?:\A|\n)Please open the following URL in your browser to authorize:\r?\n([^\r\n]+)\r?\n")


def dms_verification_url(value: str) -> str:
    """Accept only aliyun CLI's public CN PKCE challenge for its own loopback listener."""
    if (not isinstance(value, str) or len(value) > 1024
            or not re.fullmatch(r"[A-Za-z0-9._~%/?&=:+-]+", value)):
        raise ValueError("invalid DMS authorization URL")
    parsed = urlsplit(value)
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=6)
    except ValueError:
        raise ValueError("invalid DMS authorization URL") from None
    fields = dict(pairs)
    if (parsed.scheme != "https" or parsed.netloc != "signin.aliyun.com"
            or parsed.path != "/oauth2/v1/auth" or len(pairs) != 6
            or fields.keys() != {"client_id", "redirect_uri", "response_type", "state",
                                 "code_challenge", "code_challenge_method"}
            or fields["client_id"] != "4038181954557748008" or fields["response_type"] != "code"
            or fields["code_challenge_method"] != "S256"
            or not re.fullmatch(r"http://127\.0\.0\.1:1234[5-9]/cli/callback", fields["redirect_uri"])
            or not re.fullmatch(r"[A-Za-z0-9]{16}", fields["state"])
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", fields["code_challenge"])):
        raise ValueError("invalid DMS authorization URL")
    return value


def _default_aliyun_profile(executable: str, env: dict[str, str]) -> str:
    # The CLI owns its credential file. Only the marked name in the first
    # column is needed; its other columns must never be logged or returned.
    result = subprocess.run([executable, "configure", "list"], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
    names = []
    for line in result.stdout.splitlines():
        first, separator, _ = line.partition(b"|")
        first = first.strip()
        if separator and first.endswith(b" *"):
            names.append(first[:-2].decode("utf-8"))
    if (len(names) != 1 or not 1 <= len(names[0]) <= 128 or names[0].startswith("-")
            or any(ord(c) < 32 or ord(c) == 127 for c in names[0])):
        raise ValueError("cannot identify the original CLI profile")
    return names[0]


def _restore_aliyun_profile(executable: str, env: dict[str, str], original: str) -> None:
    if original == _DMS_PROFILE or _default_aliyun_profile(executable, env) != _DMS_PROFILE:
        return
    subprocess.run([executable, "configure", "switch", "--profile", original], env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=5, check=True)


def _verification_url(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or "\\" in value:
        raise ValueError("invalid authorization URL")
    decoded = value
    for _ in range(3):
        if any(ord(c) <= 32 or ord(c) == 127 for c in decoded):
            raise ValueError("invalid authorization URL")
        decoded = unquote(decoded)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "signin.volcengine.com"
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment
                or any(key.lower() in {"device_code", "access_token", "refresh_token", "id_token",
                                       "token", "code_verifier"} for key, _ in parse_qsl(parsed.query))):
            raise ValueError("invalid authorization URL")
    except ValueError:
        raise ValueError("invalid authorization URL") from None
    return value


def _environment(provider: str = "tls") -> dict[str, str]:
    # Match the query broker's CLI identity environment. Do not inject static
    # cloud credentials, browser controls, shell startup files or arbitrary flags.
    keys = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "XDG_CONFIG_HOME",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
            "all_proxy", "no_proxy")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    if provider == "tls":
        env.update(VE_CALLER_TYPE="ai_agent", VE_CALLER_NAME="codex", VE_SKILL_NAME="volcengine-tls")
    return env


@dataclass
class _Session:
    provider: str
    actor: str
    request_id: str
    deadline: float
    state: str = "starting"
    verification_url: str = ""
    user_code: str = ""
    error: str = ""
    process: Any = None
    thread: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)


class ProviderAuth:
    def __init__(self, *, popen: Callable = subprocess.Popen, runner: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic, dms_verify: Callable[[], bool] | None = None):
        self._popen, self._runner, self._clock = popen, runner, clock
        self._dms_verify = dms_verify
        self._lock = threading.RLock()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._closed = False

    @staticmethod
    def _validate(provider: str, actor: str, request_id: str) -> None:
        if (provider not in ("tls", "dms") or not isinstance(actor, str) or not _IDENTIFIER.fullmatch(actor)
                or not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id)):
            raise ValueError("invalid provider authorization request")

    def _snapshot(self, session: _Session) -> dict[str, Any]:
        result: dict[str, Any] = {"provider": session.provider, "request_id": session.request_id, "state": session.state}
        if session.state in {"starting", "pending"}:
            result["expires_in"] = max(0, int(session.deadline - self._clock()))
        if session.state == "pending":
            result["verification_url"] = session.verification_url
            if session.user_code:
                result["user_code"] = session.user_code
        if session.error:
            result["error"] = session.error
        return result

    @staticmethod
    def _unavailable(provider: str, request_id: str, state: str) -> dict[str, str]:
        return {"provider": provider, "request_id": request_id, "state": state}

    def start(self, provider: str, actor: str, request_id: str) -> dict[str, Any]:
        self._validate(provider, actor, request_id)
        with self._lock:
            if self._closed or (provider == "dms" and self._dms_verify is None):
                return self._unavailable(provider, request_id, "unavailable")
            existing = self._sessions.get(request_id)
            if existing:
                if existing.actor == actor and existing.provider == provider:
                    return self._snapshot(existing)
                return self._unavailable(provider, request_id, "forbidden")
            if any(s.state not in _TERMINAL or (s.thread and s.thread.is_alive()) for s in self._sessions.values()):
                return self._unavailable(provider, request_id, "busy")
            # Recent completed tickets are idempotent. The bridge also owns a
            # one-shot approval, so eviction never authorizes another attempt.
            while len(self._sessions) >= MAX_SESSIONS:
                self._sessions.popitem(last=False)
            session = _Session(provider, actor, request_id, self._clock() + MAX_AUTH_SECONDS)
            self._sessions[request_id] = session
            worker = self._run_dms if provider == "dms" else self._run
            session.thread = threading.Thread(target=worker, args=(session,), daemon=True,
                                              name=f"octopus-{provider}-auth")
            session.thread.start()
            return self._snapshot(session)

    def status(self, provider: str, actor: str, request_id: str) -> dict[str, Any]:
        self._validate(provider, actor, request_id)
        with self._lock:
            session = self._sessions.get(request_id)
            if session is None or session.actor != actor or session.provider != provider:
                return self._unavailable(provider, request_id, "forbidden")
            return self._snapshot(session)

    def _finish(self, session: _Session, state: str, error: str = "") -> None:
        with self._lock:
            if session.state in _TERMINAL:
                return
            session.state, session.error = state, error
            session.verification_url = session.user_code = ""
            session.stop.set()

    def cancel(self, provider: str, actor: str, request_id: str) -> dict[str, Any]:
        self._validate(provider, actor, request_id)
        with self._lock:
            session = self._sessions.get(request_id)
            if session is None or session.actor != actor or session.provider != provider:
                return self._unavailable(provider, request_id, "forbidden")
            self._finish(session, "cancelled")
            process = session.process
            result = self._snapshot(session)
        self._stop_process(process)
        return result

    @staticmethod
    def _stop_process(process: Any) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _verify(self) -> bool:
        runner = self._runner
        if runner is None:
            # Delayed import keeps this privileged module out of model tools and
            # avoids a module cycle when the HTTP host constructs ProviderAuth.
            from .query_host import run_cli
            runner = run_cli
        result = runner(["ve", "volclog", "tool", "exec", "project.describe-projects", "--input",
                         '{"PageSize":1}', "--output-mode", "stdout"], tls=True, timeout=15)
        return isinstance(result, dict) and result.get("status") == "success"

    def _run_dms(self, session: _Session) -> None:
        process = None
        original_profile = ""
        executable = ""
        env = _environment("dms")
        state, error = "failed", "DMS 授权未完成，请重新发起。"
        output = bytearray()
        completed = False
        try:
            executable = shutil.which("aliyun", path=env.get("PATH", "")) or ""
            if not os.path.isabs(executable):
                raise ValueError("aliyun CLI is unavailable")
            # CLI 3.5.4's normal OAuth flow owns its loopback callback and PKCE
            # verifier. It opens a browser using exec.LookPath("open"/"xdg-open").
            # Launch the CLI by absolute path and hide those helpers from it.
            # --no-browser instead requires someone to copy a secret callback URL.
            env["PATH"] = os.devnull
            original_profile = _default_aliyun_profile(executable, env)
            if session.stop.is_set():
                return
            process = self._popen([executable, "configure", "--mode", "OAuth", "--oauth-site-type", "CN",
                                   "--profile", _DMS_PROFILE], env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(Path.home()),
                                  start_new_session=True, shell=False)
            with self._lock:
                session.process = process
            # EOF on stdin preserves the CLI's current region and language. The
            # device doing the browser login must be this Mac to reach loopback.
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                while not session.stop.is_set() and self._clock() < session.deadline:
                    if not selector.select(0.1):
                        continue
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT_BYTES:
                        raise ValueError("authorization output limit exceeded")
                    if session.state == "starting" and (match := _DMS_LINK.search(output)):
                        url = dms_verification_url(match[1].decode("ascii"))
                        with self._lock:
                            if not session.stop.is_set():
                                session.verification_url, session.state = url, "pending"
            if session.stop.is_set() or self._clock() >= session.deadline:
                return
            completed = (process.wait(timeout=1) == 0 and session.state == "pending"
                         and b"Saving profile[company-cli-user] ...Done." in output)
        except Exception:
            # CLI output may contain credential fragments, account details and
            # authorization material. Never include it or exception text.
            pass
        finally:
            self._stop_process(process)
            if process is not None:
                process.stdout.close()
                try:
                    # Configure selects its target as the default when saving.
                    # Cancellation or a lost final reply can race that save.
                    _restore_aliyun_profile(executable, env, original_profile)
                except Exception:
                    completed = False
                    state, error = "failed", "无法确认阿里云 CLI 默认配置已恢复，请管理员在宿主机检查。"
            # The verification CLI explicitly selects company-cli-user. Restore
            # the user's original default before the potentially slow cloud read.
            if completed and not session.stop.is_set() and self._clock() < session.deadline:
                error = "DMS 登录已完成，但生产目录查询权限验证未通过。"
                try:
                    if self._dms_verify is not None and self._dms_verify() is True:
                        state, error = "succeeded", ""
                except Exception:
                    pass
            if self._clock() >= session.deadline:
                state, error = "expired", "DMS 授权已过期，请重新发起。"
            with self._lock:
                session.process = None
                self._finish(session, state, error)

    def _run(self, session: _Session) -> None:
        process = None
        selector = selectors.DefaultSelector()
        output = bytearray()
        stderr = ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        total = 0
        expected_url = ""
        verification = complete = user_code = ""
        confirmed = False
        try:
            process = self._popen(["ve", "volclog", "login", "--no-browser"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_environment(),
                                  cwd=str(Path.home()), start_new_session=True, shell=False)
            with self._lock:
                session.process = process
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                if session.stop.is_set():
                    return
                if self._clock() >= session.deadline:
                    self._finish(session, "expired", "TLS 授权已过期，请重新发起。")
                    return
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        raise ValueError("authorization output limit exceeded")
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
                        continue
                    # Decode chunks incrementally, including profiles whose
                    # UTF-8 names happen to span pipe reads.
                    stderr += decoder.decode(chunk)
                    while "\n" in stderr:
                        line, stderr = stderr.split("\n", 1)
                        line = line.removesuffix("\r")
                        if expected_url:
                            url = _verification_url(line)
                            if expected_url == "complete":
                                complete = url
                            else:
                                verification = url
                            expected_url = ""
                        elif line == "Open the following URL to authorize this device:":
                            expected_url = "verification"
                        elif line == "Alternatively, open:":
                            expected_url = "complete"
                        elif line.startswith("Then enter the code: "):
                            user_code = line.removeprefix("Then enter the code: ")
                            if not _USER_CODE.fullmatch(user_code):
                                raise ValueError("invalid authorization user code")
                        elif line.startswith("This device code expires in "):
                            match = re.fullmatch(r"This device code expires in ([1-9][0-9]{0,7}) seconds\.", line)
                            if not match or not verification or not user_code:
                                raise ValueError("invalid authorization challenge")
                            with self._lock:
                                if session.state not in _TERMINAL:
                                    session.deadline = min(session.deadline, self._clock() + int(match[1]))
                                    session.verification_url = complete or verification
                                    session.user_code = "" if complete else user_code
                                    session.state = "pending"
                    # Session-replacement confirmation has no newline. Only
                    # this exact CLI prompt can consume the admin's previously
                    # granted consent to replace the shared TLS login identity.
                    if _REPLACE_PROMPT.fullmatch(stderr):
                        if confirmed or session.state != "pending" or session.stop.is_set():
                            raise ValueError("unexpected identity replacement prompt")
                        process.stdin.write(b"y\n")
                        process.stdin.flush()
                        confirmed, stderr = True, ""
                    if len(stderr) > 4096:
                        raise ValueError("authorization line limit exceeded")
            if session.stop.is_set():
                return
            code = process.wait(timeout=1)
            if code != 0:
                self._finish(session, "failed", "TLS 授权未完成或已被拒绝，请重新发起。")
                return
            result = json.loads(bytes(output))
            if (not isinstance(result, dict) or result.get("provider") != "console-login"
                    or not isinstance(result.get("profile"), str) or not result["profile"]
                    or len(result["profile"]) > 128):
                raise ValueError("invalid authorization completion")
            try:
                verified = self._clock() < session.deadline and self._verify()
            except Exception:
                verified = False
            if self._clock() >= session.deadline:
                self._finish(session, "expired", "TLS 授权已过期，请重新发起。")
            elif verified:
                self._finish(session, "succeeded")
            else:
                self._finish(session, "failed", "TLS 登录已完成，但查询权限验证未通过。")
        except Exception:
            self._finish(session, "failed", "TLS 授权未完成，请稍后重新发起。")
        finally:
            selector.close()
            self._stop_process(process)
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            with self._lock:
                session.process = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            for session in sessions:
                self._finish(session, "cancelled")
        for session in sessions:
            self._stop_process(session.process)
            if session.thread is not None:
                session.thread.join(timeout=2)
