"""Private stdio Codex app-server transport, compatible with Codex 0.163.0."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from .contracts import CodexEvent, EventHandler, Question
from .images import MAX_IMAGES, MAX_TOTAL_IMAGE_BYTES, ImageInputError

logger = logging.getLogger(__name__)
RequestId = str | int
ToolHandler = Callable[[str, str, str, dict[str, Any]], Awaitable[dict[str, Any]]]
READ_ONLY_PROFILE = "octopus-readonly"
READ_ONLY_DISABLED_FEATURES = tuple((
    "shell_tool shell_snapshot apps plugins remote_plugin recommended_plugins tool_suggest "
    "skill_search skill_mcp_dependency_install hooks memories multi_agent multi_agent_v2 "
    "browser_use browser_use_external browser_use_full_cdp_access computer_use in_app_browser "
    "code_mode code_mode_only image_generation view_image request_permissions_tool "
    "exec_permission_approvals stable_environment_tools workspace_dependencies goals realtime_conversation "
    "default_mode_request_user_input"
).split())


def readonly_thread_config() -> dict[str, Any]:
    """Reapply capability restrictions on both creation and restoration."""
    return {
        **{f"features.{name}": False for name in READ_ONLY_DISABLED_FEATURES},
        # Current model catalogs require code_mode_only. This host is a Rust/V8
        # isolate with no filesystem, network, process or import APIs; its only
        # external operations are the registered dynamic query callbacks.
        "features.code_mode_host.enabled": True,
        "features.code_mode_host.disable_in_process_fallback": True,
        "features.skip_host_skill_discovery": True,
        "web_search": "disabled", "project_doc_max_bytes": 0,
        "skills.include_instructions": False, "skills.bundled.enabled": False,
        "cloud.skills.enabled": False, "orchestrator.mcp.enabled": False,
        "apps._default.enabled": False, "agents.enabled": False,
        "tools.experimental_request_user_input.enabled": False,
    }


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _config_layer_value(layers: Any, key: str) -> Any:
    """Read a leaf from Codex 0.163.0's highest-to-lowest config layers."""
    if not isinstance(layers, list):
        return None
    for layer in layers:
        if not isinstance(layer, dict):
            return None
        if layer.get("disabledReason") is not None:
            continue
        value = layer.get("config")
        for part in key.split("."):
            # A higher-priority scalar/array parent replaces the lower table.
            if not isinstance(value, dict):
                return None
            if part not in value:
                break
            value = value[part]
        else:
            return value
    return None


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _application_policy_context(instructions: str) -> dict[str, dict[str, str]]:
    """Avoid 0.163.0's per-fragment truncation without dropping policy text."""
    fragments = []
    remaining = instructions
    while remaining:
        part = remaining.encode("utf-8")[:3000].decode("utf-8", errors="ignore")
        if len(part) < len(remaining):
            boundary = part.rfind("\n\n")
            if boundary > 0:
                part = part[:boundary + 2]
            else:
                boundary = part.rfind(" ")
                if boundary >= len(part) // 2:
                    part = part[:boundary + 1]
        fragments.append(part)
        remaining = remaining[len(part):]
    context = {"octopus-answer-policy": {
        "kind": "application",
        "value": "以下按编号排列的应用规则分段合起来是本轮当前生效的完整规则，"
                 "替代本应用历史会话中的旧规则。历史回答不构成本轮规则；"
                 f"请合并本轮全部 {len(fragments)} 段规则回答当前问题。",
    }}
    context.update({f"octopus-answer-policy-{index:04d}": {"kind": "application", "value": part}
                    for index, part in enumerate(fragments, 1)})
    return context


def _is_id(value: Any) -> bool:
    return isinstance(value, str) or type(value) is int


def _error(value: Any) -> str:
    return _string(_object(value).get("message")) or str(value or "未知错误")


def _image_inputs(images: tuple[str, ...]) -> list[dict[str, str]]:
    """Allow only bounded, inline images that the message layer has sanitized."""
    if not isinstance(images, tuple) or len(images) > MAX_IMAGES:
        raise ImageInputError(f"每条消息最多支持 {MAX_IMAGES} 张图片。")
    inputs: list[dict[str, str]] = []
    total_bytes = 0
    for image in images:
        if not isinstance(image, str):
            raise ImageInputError("图片输入格式无效，请重新发送图片。")
        prefix, separator, encoded = image.partition(",")
        if (not separator or prefix not in {"data:image/png;base64", "data:image/jpeg;base64"}
                or not encoded):
            raise ImageInputError("图片输入格式无效，请重新发送图片。")
        # Reject before allocating decoded bytes; input must fit in the same
        # bounded app-server frame as the prompt and the other images.
        remaining = MAX_TOTAL_IMAGE_BYTES - total_bytes
        if len(encoded) > 4 * ((remaining + 2) // 3):
            raise ImageInputError("图片总大小超过限制，请减少图片数量或压缩后重试。")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ImageInputError("图片输入格式无效，请重新发送图片。") from None
        total_bytes += len(decoded)
        if not decoded or total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ImageInputError("图片总大小超过限制，请减少图片数量或压缩后重试。")
        inputs.append({"type": "image", "url": image})
    return inputs


@dataclass
class _Prompt:
    kind: str
    thread_id: str
    turn_id: str
    decisions: set[str] | None = None
    question_ids: set[str] | None = None


class ActiveTurnError(RuntimeError):
    """The caller must reconcile or explicitly stop the existing turn first."""

    def __init__(self, thread_id: str, turn_id: str | None = None):
        self.thread_id = thread_id
        self.turn_id = turn_id
        detail = f"（任务 {turn_id}）" if turn_id else "（暂时无法确认任务编号）"
        super().__init__(f"此 Codex 会话仍有任务运行{detail}，新消息尚未提交。请先查看状态或明确停止原任务。")


class AppServerClient:
    """Own a subprocess; never attach to or stop the desktop app's daemon.

    ``extra_args`` precedes ``app-server --stdio``. This supports runners such as
    ``AppServerClient(sys.executable, extra_args=[str(fake_server)])`` in tests.
    """

    def __init__(self, executable: str = "codex", model: str | None = None,
                 request_timeout: float = 30, extra_args: list[str] | None = None,
                 url: str | None = None, token_file: str | None = None,
                 read_only: bool = False, tool_specs: list[dict[str, Any]] | None = None,
                 tool_handler: ToolHandler | None = None, reasoning_effort: str | None = None):
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("request_timeout 必须是正数")
        self.executable = executable
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.request_timeout = request_timeout
        self.extra_args = list(extra_args or [])
        self.read_only = read_only
        self.tool_specs = json.loads(json.dumps(tool_specs or []))
        names: set[str] = set()
        for spec in self.tool_specs:
            name = spec.get("name") if isinstance(spec, dict) else None
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                    or name in names or spec.get("type") != "function"
                    or not isinstance(spec.get("inputSchema"), dict)):
                raise ValueError("Invalid dynamic query tool specification")
            names.add(name)
        self._tool_names = names
        self.tool_handler = tool_handler
        self._tool_tasks: dict[RequestId, asyncio.Task[Any]] = {}
        self._read_only_cwd: str | None = None
        self._read_only_home: str | None = None
        self._read_only_developer_instructions: str | None = None
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("Codex URL 必须是无内嵌凭据的 ws:// 或 wss:// 地址")
            if not token_file:
                raise ValueError("WebSocket 模式必须配置 token_file")
        elif token_file:
            raise ValueError("token_file 仅适用于 WebSocket 模式")
        self.url = url
        self.token_file = token_file
        self._process: asyncio.subprocess.Process | None = None
        self._websocket: Any = None
        self._transport: Any = None
        self._starting: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._handlers: list[EventHandler] = []
        self._pending: dict[RequestId, asyncio.Future[Any]] = {}
        self._prompts: dict[RequestId, _Prompt] = {}
        self._messages: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
        self._write_lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._generation = 0
        self._serial = 0

    def on_event(self, handler: EventHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def remove() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)
        return remove

    def _emit(self, event: CodexEvent) -> None:
        for handler in tuple(self._handlers):
            try:
                handler(event)
            except Exception:
                # A UI callback must not corrupt the protocol reader or leave approvals hanging.
                logger.exception("Codex event handler failed")

    def _track(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            if not completed.cancelled() and completed.exception() is not None:
                logger.error("Codex transport background task failed (%s)", type(completed.exception()).__name__)
        task.add_done_callback(done)
        return task

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Codex 客户端已关闭")
        if self._ready:
            return
        if self._starting is None:
            self._starting = asyncio.create_task(self._connect())
        task = self._starting
        try:
            await asyncio.shield(task)
        finally:
            if task.done() and self._starting is task:
                self._starting = None

    async def _connect(self) -> None:
        if self.url:
            await self._connect_websocket()
            return
        process = await asyncio.create_subprocess_exec(
            self.executable, *self.extra_args, "app-server", "--stdio",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024,
        )
        if self._closed:
            await self._terminate(process)
            raise RuntimeError("Codex 客户端已关闭")
        self._process = process
        self._transport = process
        self._generation += 1
        self._track(self._read_stdout(process))
        self._track(self._drain_stderr(process))
        self._track(self._watch_exit(process))
        try:
            await self._handshake(process)
        except BaseException:
            self._lose_connection(process, "Codex 初始化失败")
            await self._terminate(process)
            raise

    async def _connect_websocket(self) -> None:
        from websockets.asyncio.client import connect

        assert self.url and self.token_file
        with Path(self.token_file).open("r", encoding="utf8") as stream:
            token = stream.read(16_385).strip()
        if not token or len(token) > 16_384 or any(character.isspace() for character in token):
            raise ValueError("Codex token 文件必须包含一个有效的单行 token")
        # A dedicated quiet logger prevents DEBUG HTTP header dumps containing the token.
        transport_logger = logging.Logger("octopus.codex.websocket", logging.CRITICAL + 1)
        try:
            websocket = await connect(
                self.url, additional_headers={"Authorization": f"Bearer {token}"},
                open_timeout=self.request_timeout, close_timeout=2,
                max_size=8 * 1024 * 1024, proxy=None, logger=transport_logger,
            )
        except Exception as error:
            raise ConnectionError(f"Codex WebSocket 连接失败（{type(error).__name__}）") from None
        if self._closed:
            await websocket.close()
            raise RuntimeError("Codex 客户端已关闭")
        self._websocket = websocket
        self._transport = websocket
        self._generation += 1
        self._track(self._read_websocket(websocket))
        try:
            await self._handshake(websocket)
        except BaseException:
            self._lose_connection(websocket, "Codex 初始化失败")
            await websocket.close()
            raise

    async def _handshake(self, transport: Any) -> None:
        initialized = _object(await self._request("initialize", {
            "clientInfo": {"name": "octopus_codex", "title": "Octopus Codex", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        }, require_ready=False))
        if self.read_only:
            product = _string(initialized.get("userAgent")).split(" ", 1)[0]
            home = _string(initialized.get("codexHome"))
            if not product.endswith("/0.163.0") or not home.endswith("/.data/codex-readonly/home"):
                raise RuntimeError("Codex 只读模式需要经过验证的版本和独立配置目录")
            self._read_only_home = home
        await self._write({"method": "initialized"})
        if self.read_only:
            await self._attest_read_only()
        if self._transport is not transport or self._closed:
            raise RuntimeError("Codex 初始化期间连接中断")
        self._ready = True

    async def _attest_read_only(self) -> None:
        """Refuse a personal or drifted host before any colleague thread loads."""
        self._read_only_cwd = None
        self._read_only_developer_instructions = None
        result = _object(await self._request("config/read", {"includeLayers": True}, require_ready=False))
        config = _object(result.get("config"))
        for key, expected in readonly_thread_config().items():
            if key == "tools.experimental_request_user_input.enabled":
                # ToolsV2 only serializes web_search in 0.163.0. The original
                # layers retain this supported runtime setting without loss.
                actual = _config_layer_value(result.get("layers"), key)
            else:
                actual: Any = config
                for part in key.split("."):
                    actual = _object(actual).get(part)
            if actual != expected or type(actual) is not type(expected):
                raise RuntimeError("Codex 只读隔离配置校验失败，拒绝接入")
        if (config.get("approval_policy") != "never"
                or config.get("default_permissions") != READ_ONLY_PROFILE
                or _object(config.get("mcp_servers")) or _object(config.get("plugins"))):
            raise RuntimeError("Codex 只读隔离配置校验失败，拒绝接入")
        profile = _object(_object(config.get("permissions")).get(READ_ONLY_PROFILE))
        filesystem = _object(profile.get("filesystem"))
        filesystem = {key: value for key, value in filesystem.items()
                      if not (key == "glob_scan_max_depth" and value is None)}
        roots = [path for path, access in filesystem.items()
                 if path not in {":root", ":minimal"} and access == "read"]
        if (profile.get("extends") or filesystem.get(":root") != "deny"
                or filesystem.get(":minimal") != "read" or len(roots) != 1
                or set(filesystem) != {":root", ":minimal", roots[0]}
                or not roots[0].endswith("/.data/codex-readonly/workspace")
                or not roots[0].startswith("/") or "/../" in roots[0]
                or _object(profile.get("network")).get("enabled") is not False
                or profile.get("workspace_roots")):
            raise RuntimeError("Codex 只读文件系统权限校验失败，拒绝接入")
        if self._read_only_home != roots[0].removesuffix("workspace") + "home":
            raise RuntimeError("Codex 只读配置目录与工作目录不匹配")
        instructions = config.get("developer_instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise RuntimeError("Codex 只读回答规则缺失，拒绝接入")
        # Keep the complete host policy, including security instructions. Per-turn
        # application context below also updates model-visible restored history.
        self._read_only_cwd = roots[0]
        self._read_only_developer_instructions = instructions

    def _thread_permissions(self, cwd: str) -> dict[str, Any]:
        if not self.read_only:
            return {"cwd": cwd, "sandbox": "workspace-write", "approvalPolicy": "on-request",
                    "approvalsReviewer": "user"}
        if not self._read_only_cwd or cwd.rstrip("/") != self._read_only_cwd:
            raise ValueError("Codex 只读模式必须使用隔离工作目录")
        if self._read_only_developer_instructions is None:
            raise RuntimeError("Codex 只读回答规则尚未校验，拒绝创建或恢复会话")
        return {"cwd": self._read_only_cwd, "runtimeWorkspaceRoots": [],
                "permissions": READ_ONLY_PROFILE, "approvalPolicy": "never",
                "approvalsReviewer": "user", "config": readonly_thread_config(),
                "developerInstructions": self._read_only_developer_instructions}

    def _check_thread_permissions(self, result: dict[str, Any]) -> None:
        if not self.read_only:
            return
        profile = _object(result.get("activePermissionProfile"))
        sandbox = _object(result.get("sandbox"))
        if (result.get("approvalPolicy") != "never" or result.get("cwd") != self._read_only_cwd
                or result.get("runtimeWorkspaceRoots") != [] or result.get("instructionSources") != []
                or profile.get("id") != READ_ONLY_PROFILE or profile.get("extends")
                or sandbox.get("type") != "readOnly" or sandbox.get("networkAccess") is not False):
            raise RuntimeError("Codex 会话只读权限校验失败，任务未提交")

    async def _read_websocket(self, websocket: Any) -> None:
        reason = "Codex WebSocket 连接已结束"
        try:
            async for payload in websocket:
                if self._transport is not websocket:
                    return
                message = json.loads(payload)
                if not isinstance(message, dict):
                    raise ValueError("协议消息不是 JSON 对象")
                await self._receive(message)
        except Exception as error:
            reason = f"Codex WebSocket 连接错误（{type(error).__name__}）"
        finally:
            self._lose_connection(websocket, reason)
            await websocket.close()

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        # Drain, without forwarding possible credentials and internal diagnostic data to chat.
        while await process.stderr.read(8192):
            pass

    async def _watch_exit(self, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        self._lose_connection(process, f"Codex 进程已退出（{code}）")

    async def _read_stdout(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        reason = "Codex 协议连接已结束"
        try:
            while line := await process.stdout.readline():
                if self._process is not process:
                    return
                if not line.strip():
                    continue
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("协议消息不是 JSON 对象")
                await self._receive(message)
        except Exception as error:
            reason = f"Codex 协议连接错误：{error}"
        finally:
            self._lose_connection(process, reason)
            await self._terminate(process)

    def _lose_connection(self, transport: Any, reason: str) -> None:
        if self._transport is not transport:
            return
        self._transport = None
        self._process = None
        self._websocket = None
        self._ready = False
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError(reason))
        self._pending.clear()
        for request_id, prompt in tuple(self._prompts.items()):
            self._emit(CodexEvent("request_resolved", thread_id=prompt.thread_id, request_id=request_id))
        self._prompts.clear()
        self._messages.clear()
        for task in self._tool_tasks.values():
            task.cancel()
        self._tool_tasks.clear()
        self._read_only_cwd = None
        self._read_only_home = None
        self._read_only_developer_instructions = None
        if not self._closed:
            self._emit(CodexEvent("disconnect", error=reason, text=reason))

    async def _write(self, message: dict[str, Any]) -> None:
        async with self._write_lock:
            if self._websocket is not None:
                websocket = self._websocket
                try:
                    await websocket.send(json.dumps(message, ensure_ascii=False))
                except Exception:
                    self._lose_connection(websocket, "Codex WebSocket 写入失败")
                    raise ConnectionError("Codex WebSocket 写入失败") from None
                return
            process = self._process
            if process is None or process.stdin is None or process.returncode is not None:
                raise ConnectionError("Codex 未连接")
            try:
                process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                await process.stdin.drain()
            except (OSError, RuntimeError) as error:
                self._lose_connection(process, f"Codex 写入失败：{error}")
                raise ConnectionError("Codex 写入失败") from error

    async def _request(self, method: str, params: dict[str, Any], *, require_ready: bool = True) -> Any:
        if require_ready and not self._ready:
            raise ConnectionError("Codex 尚未连接，请先启动客户端")
        self._serial += 1
        request_id = f"bridge:{self._generation}:{self._serial}"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            try:
                async with asyncio.timeout(self.request_timeout):
                    await self._write({"id": request_id, "method": method, "params": params})
                    return await future
            except TimeoutError as error:
                raise TimeoutError(f"Codex {method} 请求超时；执行状态未知，请检查状态，不要自动重试任务。") from error
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Retrieve a disconnect exception even if the write failed first.

    async def create_thread(self, cwd: str) -> str:
        params: dict[str, Any] = {
            **self._thread_permissions(cwd), "ephemeral": False,
        }
        if self.read_only:
            # No local execution environment means no apply_patch or local file
            # tools are registered in Codex 0.163.0. Dynamic tools still work.
            params["environments"] = []
            params["dynamicTools"] = self.tool_specs
        if self.model:
            params["model"] = self.model
        result = _object(await self._request("thread/start", params))
        self._check_thread_permissions(result)
        thread_id = _string(_object(result.get("thread")).get("id"))
        if not thread_id:
            raise RuntimeError("Codex thread/start 响应缺少 thread.id")
        return thread_id

    async def resume_thread(self, thread_id: str, cwd: str) -> None:
        params: dict[str, Any] = {
            **self._thread_permissions(cwd), "threadId": thread_id, "excludeTurns": True,
        }
        if self.model:
            params["model"] = self.model
        result = _object(await self._request("thread/resume", params))
        self._check_thread_permissions(result)
        thread = _object(result.get("thread"))
        if _object(thread.get("status")).get("type") == "active":
            try:
                turn_id = await self._find_active_turn(thread_id, thread)
            except Exception:
                turn_id = None
            raise ActiveTurnError(thread_id, turn_id)

    async def _find_active_turn(self, thread_id: str, thread: dict[str, Any]) -> str | None:
        for entry in thread.get("turns") or []:
            turn = _object(entry)
            if turn.get("status") == "inProgress" and isinstance(turn.get("id"), str):
                return turn["id"]
        result = _object(await self._request("thread/turns/list", {
            "threadId": thread_id, "sortDirection": "desc", "itemsView": "summary", "limit": 20,
        }))
        for entry in result.get("data") or []:
            turn = _object(entry)
            if turn.get("status") == "inProgress" and isinstance(turn.get("id"), str):
                return turn["id"]
        return None

    async def active_turn(self, thread_id: str) -> str | None:
        """Inspect a mapped thread; never interrupt or submit input to it.

        An active status without an identifiable turn raises instead of looking idle.
        The bridge can use the returned ID for an explicit user-requested interrupt.
        """
        result = _object(await self._request("thread/read", {"threadId": thread_id, "includeTurns": False}))
        thread = _object(result.get("thread"))
        status = _object(thread.get("status")).get("type")
        if status == "active":
            turn_id = await self._find_active_turn(thread_id, thread)
            if turn_id is None:
                raise ActiveTurnError(thread_id)
            return turn_id
        if status not in {"idle", "notLoaded"}:
            raise RuntimeError("无法确认 Codex 会话处于空闲状态，未提交新任务")
        return None

    async def start_turn(self, thread_id: str, text: str, images: tuple[str, ...] = ()) -> str:
        image_inputs = _image_inputs(images)
        # turn/start may steer an existing turn. Never use it as an implicit queue.
        active_turn_id = await self.active_turn(thread_id)
        if active_turn_id:
            raise ActiveTurnError(thread_id, active_turn_id)
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text, "text_elements": []}, *image_inputs],
        }
        if self.reasoning_effort:
            # Apply on every turn so resumed conversations use the current setting.
            params["effort"] = self.reasoning_effort
        if self.read_only:
            instructions = self._read_only_developer_instructions
            if not isinstance(instructions, str) or not instructions.strip():
                raise RuntimeError("Codex 只读回答规则尚未校验，拒绝开始任务")
            # 0.163.0 accepts resume overrides without replacing developer
            # messages already in history. Application context is model-visible
            # on this turn, including when the thread was restored from disk.
            params["additionalContext"] = _application_policy_context(instructions)
            params["environments"] = []
            params["approvalPolicy"] = "never"
            params["permissions"] = READ_ONLY_PROFILE
            params["runtimeWorkspaceRoots"] = []
        result = _object(await self._request("turn/start", params))
        turn_id = _string(_object(result.get("turn")).get("id"))
        if not turn_id:
            raise RuntimeError("Codex turn/start 响应缺少 turn.id；请检查任务状态，勿自动重试")
        return turn_id

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def respond_approval(self, request_id: RequestId, decision: str) -> None:
        if self.read_only:
            raise ValueError("只读模式不允许提升执行权限")
        prompt = self._prompts.get(request_id)
        if not prompt or prompt.kind != "approval":
            raise ValueError("审批已失效或不属于当前连接")
        if decision not in {"accept", "decline"} or (prompt.decisions is not None and decision not in prompt.decisions):
            raise ValueError("Codex 不允许此审批选项")
        # Consume before awaiting so concurrent clicks cannot grant twice.
        del self._prompts[request_id]
        await self._write({"id": request_id, "result": {"decision": decision}})
        self._emit(CodexEvent("request_resolved", thread_id=prompt.thread_id, request_id=request_id))

    async def respond_question(self, request_id: RequestId, answers: dict[str, list[str]]) -> None:
        prompt = self._prompts.get(request_id)
        if not prompt or prompt.kind != "question":
            raise ValueError("问题已失效或不属于当前连接")
        if any(key not in (prompt.question_ids or set()) or not isinstance(values, list)
               or not all(isinstance(value, str) for value in values) for key, values in answers.items()):
            raise ValueError("问题答案格式不正确")
        del self._prompts[request_id]
        await self._write({"id": request_id, "result": {
            "answers": {key: {"answers": values} for key, values in answers.items()},
        }})
        self._emit(CodexEvent("request_resolved", thread_id=prompt.thread_id, request_id=request_id))

    async def _receive(self, message: dict[str, Any]) -> None:
        method = _string(message.get("method"))
        request_id = message.get("id")
        if not method and _is_id(request_id):
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            if "error" in message:
                future.set_exception(RuntimeError(f"Codex：{_error(message['error'])}"))
            elif "result" in message:
                future.set_result(message["result"])
            else:
                future.set_exception(RuntimeError("Codex RPC 响应缺少 result/error"))
        elif method:
            params = _object(message.get("params"))
            if _is_id(request_id):
                await self._server_request(request_id, method, params)
            else:
                self._notification(method, params)

    async def _server_request(self, request_id: RequestId, method: str, params: dict[str, Any]) -> None:
        if request_id in self._prompts:
            return
        thread_id, turn_id = _string(params.get("threadId")), _string(params.get("turnId"))
        if method == "item/tool/call" and self.read_only:
            if request_id not in self._tool_tasks:
                task = self._track(self._dynamic_tool(request_id, params))
                self._tool_tasks[request_id] = task
                task.add_done_callback(lambda _: self._tool_tasks.pop(request_id, None))
            return
        if method == "item/permissions/requestApproval":
            await self._write({"id": request_id, "result": {"permissions": {}, "scope": "turn"}})
            return
        if thread_id and turn_id and method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            if self.read_only:
                await self._write({"id": request_id, "result": {"decision": "decline"}})
                return
            detail = "\n".join(filter(None, [
                _string(params.get("command")),
                f"目录：{params['cwd']}" if params.get("cwd") else "",
                _string(params.get("reason")),
                f"写入范围：{params['grantRoot']}" if params.get("grantRoot") else "",
            ])) or "Codex 请求允许修改文件"
            available = params.get("availableDecisions")
            decisions = {choice for choice in available if isinstance(choice, str)} if isinstance(available, list) else None
            self._prompts[request_id] = _Prompt("approval", thread_id, turn_id, decisions=decisions)
            self._emit(CodexEvent("approval", thread_id=thread_id, turn_id=turn_id, request_id=request_id,
                                  kind="fileChange" if method == "item/fileChange/requestApproval" else "commandExecution", detail=detail))
            return
        if thread_id and turn_id and method == "item/tool/requestUserInput" and isinstance(params.get("questions"), list):
            questions = []
            for entry in params["questions"]:
                question = _object(entry)
                if not isinstance(question.get("id"), str) or not isinstance(question.get("question"), str):
                    await self._write({"id": request_id, "error": {"code": -32602, "message": "Malformed user-input question"}})
                    return
                raw_options = question.get("options")
                options = [{"label": option["label"], "description": _string(option.get("description"))}
                           for option in (raw_options if isinstance(raw_options, list) else [])
                           if isinstance(option, dict) and isinstance(option.get("label"), str)]
                questions.append(Question(id=question["id"], header=_string(question.get("header")) or "问题",
                    question=question["question"], options=options,
                    is_other=question.get("isOther") is True, is_secret=question.get("isSecret") is True))
            self._prompts[request_id] = _Prompt("question", thread_id, turn_id, question_ids={q.id for q in questions})
            self._emit(CodexEvent("question", thread_id=thread_id, turn_id=turn_id, request_id=request_id, questions=questions))
            return
        await self._write({"id": request_id, "error": {"code": -32601, "message": f"Unsupported server request: {method}"}})

    async def _dynamic_tool(self, request_id: RequestId, params: dict[str, Any]) -> None:
        """Keep the protocol reader free while the authorized query is running."""
        thread_id, turn_id = _string(params.get("threadId")), _string(params.get("turnId"))
        name, arguments = params.get("tool"), params.get("arguments")
        generation = self._generation
        success, text = False, "Query denied: invalid or unavailable query tool."
        if (thread_id and turn_id and (params.get("namespace") is None or params.get("namespace") == "")
                and isinstance(name, str) and name in self._tool_names
                and isinstance(arguments, dict) and self.tool_handler is not None):
            try:
                async with asyncio.timeout(90):
                    result = await self.tool_handler(thread_id, turn_id, name, arguments)
                if not isinstance(result, dict):
                    raise ValueError("Invalid query result")
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > 128_000:
                    raise ValueError("Query result exceeds limit")
                success, text = True, encoded
            except asyncio.CancelledError:
                raise
            except Exception:
                # Backend exceptions may contain CLI arguments, access tokens or
                # source data. None of that is a valid user-facing error.
                text = "Query failed or access was revoked. Narrow the request or contact the administrator."
        if generation == self._generation and self._transport is not None:
            await self._write({"id": request_id, "result": {
                "contentItems": [{"type": "inputText", "text": text}], "success": success,
            }})

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = _string(params.get("threadId"))
        if not thread_id:
            return
        if method == "serverRequest/resolved" and _is_id(params.get("requestId")):
            request_id = params["requestId"]
            self._prompts.pop(request_id, None)
            self._emit(CodexEvent("request_resolved", thread_id=thread_id, request_id=request_id))
            return
        turn = _object(params.get("turn"))
        turn_id = _string(params.get("turnId")) or _string(turn.get("id"))
        if not turn_id:
            return
        key = (thread_id, turn_id)
        if method == "item/agentMessage/delta" and isinstance(params.get("delta"), str) and isinstance(params.get("itemId"), str):
            message = self._messages.setdefault(key, {}).setdefault(params["itemId"], {"text": "", "phase": ""})
            message["text"] += params["delta"]
            self._emit(CodexEvent("text", thread_id=thread_id, turn_id=turn_id, delta=params["delta"]))
        elif method in {"item/started", "item/completed"}:
            item = _object(params.get("item"))
            if item.get("type") == "agentMessage" and isinstance(item.get("id"), str):
                messages = self._messages.setdefault(key, {})
                previous = messages.get(item["id"], {})
                messages[item["id"]] = {"text": _string(item.get("text")) if "text" in item else previous.get("text", ""), "phase": _string(item.get("phase"))}
            elif method == "item/started":
                text = ""
                if item.get("type") == "commandExecution":
                    text = f"执行命令：{_string(item.get('command'))[:1500]}"
                elif item.get("type") == "fileChange":
                    text = "正在修改文件"
                elif item.get("type") == "mcpToolCall":
                    text = f"调用工具：{_string(item.get('server'))}/{_string(item.get('tool'))}"
                elif item.get("type") == "webSearch":
                    text = "正在搜索资料"
                if text:
                    self._emit(CodexEvent("activity", thread_id=thread_id, turn_id=turn_id, text=text))
        elif method == "item/commandExecution/outputDelta" and isinstance(params.get("delta"), str):
            self._emit(CodexEvent("activity", thread_id=thread_id, turn_id=turn_id, text=f"命令输出：{params['delta'][-1500:]}"))
        elif method == "error":
            prefix = "Codex 正在重试" if params.get("willRetry") is True else "Codex 执行出错"
            self._emit(CodexEvent("activity", thread_id=thread_id, turn_id=turn_id, text=f"{prefix}：{_error(params.get('error'))}"))
        elif method == "turn/completed" and turn:
            messages = self._messages.pop(key, {})
            for entry in turn.get("items", []):
                item = _object(entry)
                if item.get("type") == "agentMessage" and isinstance(item.get("id"), str) and isinstance(item.get("text"), str):
                    messages[item["id"]] = {"text": item["text"], "phase": _string(item.get("phase"))}
            all_messages = [message for message in messages.values() if message.get("text")]
            finals = [message for message in all_messages if message.get("phase") == "final_answer"]
            unknown = [message for message in all_messages if not message.get("phase")]
            text = "\n\n".join(message["text"] for message in (finals or unknown or all_messages[-1:]))
            for request_id, prompt in tuple(self._prompts.items()):
                if (prompt.thread_id, prompt.turn_id) == key:
                    del self._prompts[request_id]
                    self._emit(CodexEvent("request_resolved", thread_id=thread_id, request_id=request_id))
            self._emit(CodexEvent("completed", thread_id=thread_id, turn_id=turn_id,
                                  status=_string(turn.get("status")) or "failed", text=text,
                                  error=_error(turn["error"]) if turn.get("error") else ""))

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def close(self) -> None:
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._close())
        await asyncio.shield(self._closing)

    async def _close(self) -> None:
        self._ready = False
        if self._starting is not None and not self._starting.done():
            self._starting.cancel()
            await asyncio.gather(self._starting, return_exceptions=True)
        process = self._process
        websocket = self._websocket
        if websocket is not None:
            self._lose_connection(websocket, "Codex 客户端已关闭")
            # Disconnect only this client. The independently managed app-server stays up.
            await websocket.close()
        if process is not None:
            self._lose_connection(process, "Codex 客户端已关闭")
            if process.stdin is not None:
                process.stdin.close()
            await self._terminate(process)
        tasks = list(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
