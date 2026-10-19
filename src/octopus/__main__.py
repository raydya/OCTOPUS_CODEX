from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import signal
import sys
import time
from pathlib import Path

from .bridge import Bridge
from .codex import AppServerClient
from .config import Config, ConfigurationError, database_url_from_env
from .contracts import CardAction, IncomingMessage, View
from .feishu import FeishuMessenger
from .store import Store

log = logging.getLogger("octopus")
STATUS_FILE = Path(os.environ.get("OCTOPUS_STATUS_FILE", "/run/octopus/status.json"))


def status(state: str) -> None:
    try:
        STATUS_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = STATUS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"state": state, "time": time.time()}))
        temporary.replace(STATUS_FILE)
    except OSError:
        pass  # A read-only invocation outside Docker need not publish health.


def stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop


def codex_client(config: Config) -> AppServerClient:
    return AppServerClient(executable=config.codex_executable, model=config.model,
                           url=config.codex_url, token_file=config.codex_token_file)


async def run(wait_for_config: bool) -> None:
    stop = stop_event()
    try:
        config = Config.from_env()
    except ConfigurationError as exc:
        if not wait_for_config:
            raise
        status("waiting_configuration")
        log.warning("%s。填写本地配置后重建 bridge 容器；当前不会接收任务。", exc)
        await stop.wait()
        return
    store = await Store.open(config.database_url, config.app_id)
    bridge: Bridge | None = None
    try:
        if not config.allowed_users and not await store.owners():
            status("waiting_pair")
            log.warning("新应用尚未配对。运行 docker compose exec bridge python -m octopus pair。")
            while not stop.is_set() and not await store.owners():
                try:
                    await asyncio.wait_for(stop.wait(), 2)
                except TimeoutError:
                    pass
        if stop.is_set():
            return
        # The pair process holds this lock until it closes its temporary listener.
        while not stop.is_set() and not await store.acquire_service_lock():
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except TimeoutError:
                pass
        if stop.is_set():
            return
        messenger = FeishuMessenger(config.app_id, config.app_secret, logger=log.info, bot_open_id=config.bot_open_id)
        bridge = Bridge(config, store, messenger, codex_client(config))
        await bridge.start()
        log.info("已启动应用 %s 的桥接进程；消息接收情况需在飞书验证。", config.app_id)
        while not stop.is_set() and not bridge.fatal.is_set():
            if not messenger.is_alive:
                raise RuntimeError("飞书监听进程已退出")
            if messenger.connection_stalled:
                raise RuntimeError("飞书连接或心跳超时")
            status("running" if messenger.is_connected else "feishu_connecting")
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except TimeoutError:
                pass
        if bridge.fatal.is_set():
            status("codex_disconnected")
            try:
                await asyncio.wait_for(bridge.idle.wait(), 5)
            except TimeoutError:
                pass
            raise RuntimeError("Codex 连接中断，等待 Compose 重启恢复；未知结果的会话会暂停")
    finally:
        if bridge:
            try:
                await asyncio.wait_for(bridge.close(), 20)
            except TimeoutError:
                log.warning("服务停止超时；未完成任务将在重启时标记待检查")
        await store.close()
        status("stopped")


async def pair() -> None:
    config = Config.from_env()
    stop = stop_event()
    store = await Store.open(config.database_url, config.app_id)
    messenger = FeishuMessenger(config.app_id, config.app_secret, logger=log.info, bot_open_id=config.bot_open_id)
    try:
        if not await store.acquire_service_lock():
            raise RuntimeError("桥接已在运行。初次配对请在 waiting_pair 状态操作；新增用户可配置 FEISHU_ALLOWED_USERS。")
        code = secrets.token_urlsafe(12)
        complete: asyncio.Future[IncomingMessage] = asyncio.get_running_loop().create_future()

        async def on_message(message: IncomingMessage) -> None:
            if complete.done() or message.chat_type != "p2p":
                return
            supplied = message.text.strip().removeprefix("/pair ")
            if not message.text.strip().startswith("/pair ") or not secrets.compare_digest(supplied, code):
                return
            await store.add_owner(message.sender_id)
            complete.set_result(message)

        async def on_action(_action: CardAction) -> None:
            return

        await messenger.start(on_message, on_action)
        print(f"向新应用机器人私聊发送（10 分钟内有效）：\n/pair {code}", flush=True)
        stop_task = asyncio.create_task(stop.wait())
        try:
            deadline = time.monotonic() + 600
            done = set()
            while time.monotonic() < deadline:
                done, _ = await asyncio.wait([complete, stop_task], timeout=2,
                                             return_when=asyncio.FIRST_COMPLETED)
                if done:
                    break
                if not messenger.is_alive or messenger.connection_stalled:
                    raise RuntimeError("飞书监听连接失败，请检查新应用凭证和事件配置后重新配对")
            if complete in done:
                message = complete.result()
                try:
                    await messenger.send(message.chat_id, View("info", "配对成功",
                        "此飞书账户已绑定本地 Codex。桥接即将就绪，可以发送 /help 查看命令。"), message.id)
                except Exception:
                    log.warning("用户已保存，但配对通知发送失败；请检查机器人发消息权限")
                print("配对成功，用户已保存到 PostgreSQL。", flush=True)
            elif not stop.is_set():
                raise RuntimeError("配对码已过期，请重新运行 pair")
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
    finally:
        await messenger.close()
        await store.close()


async def check() -> None:
    config = Config.from_env()
    store = await Store.open(config.database_url, config.app_id)
    client = codex_client(config)
    try:
        await client.start()
        print(f"App ID：{config.app_id}\nPostgreSQL：连接成功\nCodex：认证和协议握手成功")
        print("未执行模型任务。飞书权限和收发消息仍需配对时验证。")
    finally:
        await client.close()
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="独立飞书应用与宿主机 Codex 的持续会话桥接")
    parser.add_argument("command", choices=["run", "pair", "check", "health", "test-db"], nargs="?", default="run")
    parser.add_argument("--wait-for-config", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "health":
        try:
            snapshot = json.loads(STATUS_FILE.read_text())
            return 0 if snapshot["state"] == "running" and time.time() - snapshot["time"] < 60 else 1
        except (OSError, ValueError, KeyError):
            return 1
    if args.command == "test-db":
        import unittest
        os.environ["OCTOPUS_TEST_DATABASE_URL"] = database_url_from_env()
        suite = unittest.defaultTestLoader.discover("/app/tests", pattern="test_store.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    try:
        if args.command == "run":
            asyncio.run(run(args.wait_for_config))
        elif args.command == "pair":
            asyncio.run(pair())
        else:
            asyncio.run(check())
        return 0
    except ConfigurationError as exc:
        log.error("%s", exc)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # HTTP/DB errors may embed credentials or request bodies. Do not dump them.
        log.error("服务未能完成操作（%s）。检查配置、依赖服务和连接状态。", type(exc).__name__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
