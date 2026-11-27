from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
import json
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from octopus.contracts import CardAction, IncomingMessage
from octopus.provider_authorization import ProviderAuthorization
from octopus.queries import QueryClient


DMS_URL = ("https://signin.aliyun.com/oauth2/v1/auth?response_type=code"
           "&client_id=4038181954557748008&redirect_uri=http%3A%2F%2F127.0.0.1%3A12345%2Fcli%2Fcallback"
           "&state=azAZ0123456789XY&code_challenge=" + "A" * 43 + "&code_challenge_method=S256")


class Store:
    def __init__(self):
        self.people = {
            "ou_alice": {"open_id": "ou_alice", "role": "admin", "enabled": True},
            "ou_bob": {"open_id": "ou_bob", "role": "admin", "enabled": True},
            "ou_member": {"open_id": "ou_member", "role": "member", "enabled": True},
        }
        self.audits = []

    async def member(self, actor):
        return self.people.get(actor)

    async def members(self):
        return list(self.people.values())

    async def audit(self, *args):
        self.audits.append(args)


class Messenger:
    def __init__(self):
        self.notices, self.private, self.updates = [], [], []

    async def send(self, chat, view, reply):
        self.notices.append((chat, view, reply))
        return "om_notice"

    async def send_user(self, actor, view):
        card = "om_card_" + str(len(self.private))
        self.private.append((actor, view, card))
        return card

    async def update(self, card, view):
        self.updates.append((card, view))


class Queries:
    url = "https://signin.volcengine.com/cli/device?user_code=PRIVATE-CODE"

    def __init__(self):
        self.calls = []
        self.state = "pending"
        self.start_entered = asyncio.Event()
        self.start_gate = asyncio.Event()
        self.start_gate.set()

    def result(self, provider, request_id):
        return {"provider": provider, "request_id": request_id, "state": self.state,
                "verification_url": self.url, "user_code": "PRIVATE-CODE", "expires_in": 300}

    async def auth_start(self, provider, actor, request_id):
        self.calls.append(("start", provider, actor, request_id))
        self.start_entered.set()
        await self.start_gate.wait()
        return self.result(provider, request_id)

    async def auth_status(self, provider, actor, request_id):
        self.calls.append(("status", provider, actor, request_id))
        return self.result(provider, request_id)

    async def auth_cancel(self, provider, actor, request_id):
        self.calls.append(("cancel", provider, actor, request_id))
        return {"provider": provider, "request_id": request_id, "state": "cancelled"}


class ProviderAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store, self.messenger, self.queries = Store(), Messenger(), Queries()
        self.auth = ProviderAuthorization(self.store, self.messenger, self.queries)
        self.allowed = True
        self.tasks = []
        self.poll = patch("octopus.provider_authorization.POLL_SECONDS", 0.005)
        self.poll.start()
        self.addCleanup(self.poll.stop)

    async def asyncTearDown(self):
        await self.auth.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def permitted(self):
        return self.allowed

    async def until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0.001)

    def begin(self, actor="ou_alice", kind="p2p", identifier="om_question"):
        message = IncomingMessage(identifier, "oc_origin", actor, "查日志", kind, 1)
        task = asyncio.create_task(self.auth.recover(message, still_allowed=self.permitted))
        self.tasks.append(task)
        return task

    def manual(self, actor="ou_alice", kind="p2p"):
        message = IncomingMessage("om_manual", "oc_manual", actor, "/tls-auth", kind, 1)
        task = asyncio.create_task(self.auth.request_manual(message))
        self.tasks.append(task)
        return task

    async def action(self, actor="ou_alice", chat_id="oc_admin_private"):
        await self.until(lambda: any(r[0] == actor for r in self.messenger.private))
        record = next(r for r in self.messenger.private if r[0] == actor)
        return CardAction(record[1].actions[0].ticket_id, "accept", actor, chat_id, record[2])

    async def pending(self, actor="ou_alice", kind="p2p"):
        task = self.begin(actor, kind)
        action = await self.action()
        await self.auth.receive_action(action)
        await self.until(lambda: bool(self.messenger.updates))
        return task, action

    def count(self, operation):
        return sum(call[0] == operation for call in self.queries.calls)

    async def test_dms_group_recovery_confines_login_and_rejects_a_tls_link(self):
        request = IncomingMessage("om_db", "oc_group", "ou_member", "核对订单状态", "group", 1)
        task = asyncio.create_task(self.auth.recover(request, provider="dms", still_allowed=self.permitted))
        self.tasks.append(task)
        action = await self.action()
        await self.until(lambda: len(self.messenger.private) == 2)
        self.assertIn("DMS", self.messenger.notices[0][1].text)
        self.assertFalse(self.messenger.notices[0][1].actions)
        await self.auth.receive_action(action)
        self.assertFalse(await task)
        self.assertEqual([c[:2] for c in self.queries.calls], [("start", "dms"), ("cancel", "dms")])
        for _, card in self.messenger.updates:
            self.assertIn("DMS 授权未完成", card.text)
            self.assertFalse(card.actions)
        self.assertNotIn(self.queries.url, str(self.messenger.private + self.messenger.notices + self.messenger.updates))
        self.assertEqual(self.store.audits[-1][1:4], ("provider.auth", "dms", "failed"))

    async def test_success_sends_link_only_to_selected_admin_and_clears_it(self):
        task = self.begin("ou_member", "group")
        action = await self.action()
        await self.until(lambda: len(self.messenger.private) == 2)
        self.assertEqual(self.queries.calls, [])
        await self.auth.receive_action(action)
        await self.until(lambda: bool(self.messenger.updates))
        self.assertIn("signin.volcengine.com", self.messenger.updates[-1][1].text)
        self.assertEqual(self.messenger.updates[-1][0], action.message_id)
        self.assertIn("所有已获准成员", self.messenger.private[0][1].text)
        self.queries.state = "succeeded"
        self.assertTrue(await asyncio.wait_for(task, 1))
        self.assertEqual(self.count("start"), 1)
        self.assertRegex(self.queries.calls[0][3], r"^[A-Za-z0-9_-]+$")
        self.assertEqual(action.ticket_id, "provider-auth:" + self.queries.calls[0][3])
        self.assertGreaterEqual(self.count("status"), 1)
        self.assertEqual(self.count("cancel"), 0)
        for _, view, _ in self.messenger.notices:
            self.assertNotIn("signin", view.text)
            self.assertNotIn("PRIVATE-CODE", view.text)
        self.assertNotIn("signin", str(self.store.audits))
        self.assertNotIn("PRIVATE-CODE", str(self.store.audits))
        final = {card: view for card, view in self.messenger.updates}
        self.assertEqual(set(final), {r[2] for r in self.messenger.private})
        for view in final.values():
            self.assertFalse(view.actions)
            self.assertNotIn("signin", view.text)
            self.assertNotIn("PRIVATE-CODE", view.text)

    async def test_admin_request_prefers_only_that_admin(self):
        task, _ = await self.pending()
        self.assertEqual([r[0] for r in self.messenger.private], ["ou_alice"])
        self.queries.state = "succeeded"
        self.assertTrue(await task)

    async def test_manual_login_invites_only_requester_verifies_and_clears_card(self):
        task = self.manual()
        action = await self.action(chat_id="oc_manual")
        self.assertEqual([actor for actor, _, _ in self.messenger.private], ["ou_alice"])
        self.assertFalse(self.messenger.notices)
        self.assertIn("共享 TLS 查询身份", self.messenger.private[0][1].text)
        self.assertFalse(self.queries.calls)
        await self.auth.receive_action(replace(action, sender_id="ou_bob"))
        await self.auth.receive_action(replace(action, chat_id="oc_other"))
        self.assertFalse(self.auth._flows["tls"].decision)
        self.assertFalse(self.queries.calls)
        await self.auth.receive_action(action)
        await self.until(lambda: bool(self.messenger.updates))
        self.assertEqual(self.messenger.updates[-1][0], action.message_id)
        self.assertIn(self.queries.url, self.messenger.updates[-1][1].text)
        self.assertFalse(task.done())
        self.queries.state = "succeeded"
        self.assertTrue(await task)
        self.assertEqual(self.count("start"), 1)
        result = self.messenger.updates[-1][1]
        self.assertIn("授权已完成", result.text)
        self.assertEqual(self.store.audits[-1][3], "succeeded")
        self.assertFalse(result.actions)
        self.assertNotIn(self.queries.url, result.text)
        all_cards = self.messenger.private + self.messenger.updates
        for claim in ("登录已失效", "继续查询", "原任务", "自动继续"):
            self.assertNotIn(claim, str(all_cards))

    async def test_manual_success_requires_a_displayed_device_link(self):
        self.queries.url = None
        for actor, initial in (("ou_alice", "succeeded"), ("ou_bob", "pending")):
            with self.subTest(initial=initial):
                self.queries.state = initial
                task = self.manual(actor)
                await self.auth.receive_action(await self.action(actor, "oc_manual"))
                if initial == "pending":
                    await self.until(lambda: self.count("status") > 0)
                    self.queries.state = "succeeded"
                self.assertFalse(await task)
                result = self.messenger.updates[-1][1]
                self.assertNotIn("授权已完成", result.text)
                self.assertIn("/tls-auth", result.text)
                self.assertIn("/tls_auth", result.text)
                self.assertIn("重新发起 TLS 授权", result.text)
                self.assertEqual(self.store.audits[-1][3], "no_device_prompt")
                self.assertEqual(self.count("cancel"), 0)
        self.assertTrue(all(not view.actions for _, view in self.messenger.updates))

    async def test_recovery_accepts_verified_identity_without_a_device_prompt(self):
        self.queries.state = "succeeded"
        self.queries.url = None
        task = self.begin()
        await self.auth.receive_action(await self.action())
        self.assertTrue(await task)
        self.assertEqual(self.store.audits[-1][3], "succeeded")

    async def test_manual_requires_current_admin_in_private_chat(self):
        self.store.people["ou_bob"]["enabled"] = False
        for actor, kind in (("ou_alice", "group"), ("ou_member", "p2p"), ("ou_bob", "p2p")):
            with self.subTest(actor=actor, kind=kind):
                self.assertFalse(await self.manual(actor, kind))
                self.assertIn("管理员", self.messenger.notices[-1][1].text)
                self.assertIn("私聊", self.messenger.notices[-1][1].text)
        self.assertFalse(self.messenger.private)
        self.assertFalse(self.queries.calls)

    async def test_manual_revocation_during_host_start_withholds_device_link(self):
        self.queries.start_gate.clear()
        task = self.manual()
        await self.auth.receive_action(await self.action(chat_id="oc_manual"))
        await self.queries.start_entered.wait()
        self.store.people["ou_alice"]["role"] = "member"
        self.queries.start_gate.set()
        self.assertFalse(await task)
        self.assertEqual(self.count("cancel"), 1)
        self.assertNotIn(self.queries.url, str(self.messenger.updates))
        self.assertNotIn("本次查询", self.messenger.updates[-1][1].text)

    async def test_manual_and_recovery_share_one_active_login(self):
        active, _ = await self.pending()
        self.assertFalse(await self.manual("ou_bob"))
        self.assertIn("已有 TLS 授权", self.messenger.notices[-1][1].text)
        self.assertNotIn("查询", self.messenger.notices[-1][1].text)
        self.assertEqual(len(self.messenger.private), 1)
        self.assertEqual(self.count("cancel"), 0)
        self.queries.state = "succeeded"
        self.assertTrue(await active)
        manual = self.manual("ou_bob")
        action = await self.action("ou_bob", "oc_manual")
        self.assertFalse(await self.begin("ou_member", "group"))
        await self.auth.receive_action(replace(action, decision="decline"))
        self.assertFalse(await manual)
        self.assertEqual(self.count("start"), 1)
        self.assertIn("已取消", self.messenger.updates[-1][1].text)

    async def test_manual_card_delivery_failure_gives_safe_private_notice(self):
        self.messenger.send_user = AsyncMock(side_effect=RuntimeError("private-delivery-detail"))
        self.assertFalse(await self.manual())
        chat, view, reply = self.messenger.notices[-1]
        self.assertEqual((chat, reply), ("oc_manual", "om_manual"))
        self.assertIn("卡片", view.text)
        self.assertNotIn("登录已失效", view.text)
        self.assertNotIn("private-delivery-detail", view.text)
        self.assertFalse(self.queries.calls)

    async def test_real_query_client_accepts_opaque_request_ids_and_binds_responses(self):
        client = QueryClient.__new__(QueryClient)
        client.url, client.token = "http://query-host", "synthetic-test-token"
        self.auth.queries = client
        complete, calls = False, []
        def open_request(request, timeout):
            data = json.loads(request.data)
            calls.append((request.full_url, data))
            response = {"provider": "tls", "request_id": data["request_id"],
                        "state": "succeeded" if complete else "pending",
                        "verification_url": Queries.url, "user_code": "PRIVATE-CODE"}
            return BytesIO(json.dumps(response).encode())
        with patch("octopus.queries.build_opener", return_value=SimpleNamespace(open=open_request)):
            task = self.begin()
            action = await self.action()
            await self.auth.receive_action(action)
            await self.until(lambda: bool(self.messenger.updates))
            complete = True
            self.assertTrue(await task)
        self.assertEqual(calls[0][0], "http://query-host/v1/auth/start")
        self.assertIn("http://query-host/v1/auth/status", [c[0] for c in calls])
        self.assertEqual({c[1]["request_id"] for c in calls}, {action.ticket_id.removeprefix("provider-auth:")})

    async def test_forged_and_replayed_callbacks_never_start_extra_login(self):
        task = self.begin()
        action = await self.action()
        for forged in (replace(action, sender_id="ou_bob"), replace(action, message_id="om_copied"),
                       replace(action, ticket_id="provider-auth:forged"), replace(action, chat_id=None),
                       replace(action, chat_id=""), replace(action, decision="other")):
            await self.auth.receive_action(forged)
        self.assertEqual(self.count("start"), 0)
        await asyncio.gather(*(self.auth.receive_action(action) for _ in range(5)))
        await self.queries.start_entered.wait()
        self.queries.state = "succeeded"
        self.assertTrue(await task)
        await self.auth.receive_action(action)
        self.assertEqual(self.count("start"), 1)

    async def test_membership_await_race_consumes_one_decision(self):
        task = self.begin()
        action = await self.action()
        original = self.store.member
        gate = asyncio.Event()
        entered = 0
        async def lookup(actor):
            nonlocal entered
            entered += 1
            if entered == 2:
                gate.set()
            await gate.wait()
            return await original(actor)
        self.store.member = lookup
        await asyncio.gather(self.auth.receive_action(action), self.auth.receive_action(action))
        self.store.member = original
        self.queries.state = "succeeded"
        self.assertTrue(await task)
        self.assertEqual(self.count("start"), 1)

    async def test_decline_before_start_never_opens_login(self):
        task = self.begin()
        await self.auth.receive_action(replace(await self.action(), decision="decline"))
        self.assertFalse(await task)
        self.assertEqual(self.queries.calls, [])

    async def test_cancel_during_login_cancels_same_request_once(self):
        task, action = await self.pending()
        cancel = replace(action, decision="decline")
        await asyncio.gather(self.auth.receive_action(cancel), self.auth.receive_action(cancel))
        self.assertFalse(await task)
        self.assertEqual(self.count("cancel"), 1)
        self.assertEqual(self.queries.calls[0][1:], self.queries.calls[-1][1:])

    async def test_busy_request_does_not_cancel_existing_authorization(self):
        first, _ = await self.pending()
        self.assertFalse(await self.begin("ou_member", "group", "om_other"))
        self.assertIn("已有 TLS 授权", self.messenger.notices[-1][1].text)
        self.assertFalse(first.done())
        self.assertEqual(self.count("cancel"), 0)
        self.queries.state = "succeeded"
        self.assertTrue(await first)

    async def test_revoked_admin_cannot_accept_or_complete_login(self):
        task = self.begin()
        action = await self.action()
        self.store.people["ou_alice"]["enabled"] = False
        await self.auth.receive_action(action)
        self.assertEqual(self.count("start"), 0)
        self.store.people["ou_alice"]["enabled"] = True
        await self.auth.receive_action(action)
        await self.queries.start_entered.wait()
        self.store.people["ou_alice"]["role"] = "member"
        self.queries.state = "succeeded"
        self.assertFalse(await task)
        self.assertEqual(self.count("cancel"), 1)

    async def test_stopped_request_cancels_pending_login(self):
        task, _ = await self.pending()
        self.allowed = False
        self.queries.state = "succeeded"
        self.assertFalse(await task)
        self.assertEqual(self.count("cancel"), 1)

    async def test_cancel_during_awaited_admin_check_cannot_publish_login_url(self):
        task = self.begin()
        action = await self.action()
        self.queries.start_gate.clear()
        await self.auth.receive_action(action)
        await self.queries.start_entered.wait()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.store.member
        async def pending_member(actor):
            entered.set()
            await release.wait()
            return await original(actor)
        self.store.member = pending_member
        self.queries.start_gate.set()
        await entered.wait()
        flow = self.auth._flows["tls"]
        flow.cancelled = True
        flow.wakeup.set()
        release.set()
        self.assertFalse(await task)
        self.assertEqual(self.count("cancel"), 1)
        self.assertNotIn("signin.volcengine.com", str(self.messenger.updates))

    async def test_expiry_and_restarted_manager_reject_old_card(self):
        task, action = await self.pending()
        restarted = ProviderAuthorization(self.store, self.messenger, self.queries)
        await restarted.receive_action(action)
        self.auth._flows["tls"].deadline = monotonic() - 1
        self.auth._flows["tls"].wakeup.set()
        self.assertFalse(await task)
        self.assertIn("到期", self.messenger.updates[-1][1].text)
        await self.auth.receive_action(action)
        self.assertEqual(self.count("start"), 1)
        self.assertEqual(self.count("cancel"), 1)
        await restarted.close()

    async def test_untrusted_url_never_reaches_any_message(self):
        for url in ("https://signin.volcengine.com.evil.example/login", "http://signin.volcengine.com/cli",
                    "https://evil@signin.volcengine.com/cli", "https://signin.volcengine.com/cli)[evil](https://evil)"):
            with self.subTest(url=url):
                self.queries.url = url
                previous = len(self.messenger.private)
                task = self.begin()
                await self.until(lambda: len(self.messenger.private) > previous)
                actor, view, card = self.messenger.private[-1]
                await self.auth.receive_action(CardAction(view.actions[0].ticket_id, "accept", actor, "oc_admin", card))
                self.assertFalse(await task)
                self.assertNotIn(url, str(self.messenger.notices + self.messenger.private + self.messenger.updates))

    async def test_lost_start_response_is_cancelled_and_no_exception_text_leaks(self):
        task = self.begin()
        action = await self.action()
        self.queries.auth_start = AsyncMock(side_effect=RuntimeError("private-provider-token"))
        await self.auth.receive_action(action)
        self.assertFalse(await task)
        self.assertEqual(self.count("cancel"), 1)
        self.assertNotIn("private-provider-token", str(self.messenger.updates))

    async def test_no_admin_returns_fixed_notice_without_login(self):
        for member in self.store.people.values():
            member["role"] = "member"
        self.assertFalse(await self.begin("ou_member"))
        self.assertFalse(self.messenger.private)
        self.assertEqual(self.queries.calls, [])
        self.assertIn("没有可处理", self.messenger.notices[0][1].text)

    async def test_external_task_cancellation_cleans_up_session_and_card(self):
        task, _ = await self.pending()
        runner = self.auth._flows["tls"].task
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await runner
        self.assertEqual(self.count("cancel"), 1)
        self.assertNotIn("signin", self.messenger.updates[-1][1].text)

    async def test_close_cancels_login_and_unblocks_waiter(self):
        task, _ = await self.pending()
        await self.auth.close()
        self.assertFalse(await task)
        self.assertEqual(self.count("cancel"), 1)
        self.assertNotIn("tls", self.auth._flows)

    async def test_close_after_task_cancellation_does_not_interrupt_link_cleanup(self):
        task, _ = await self.pending()
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.messenger.update
        async def update(card, view):
            if not view.actions:
                entered.set()
                await release.wait()
            await original(card, view)
        self.messenger.update = update
        task.cancel()
        await self.until(entered.is_set)
        closing = asyncio.create_task(self.auth.close())
        await asyncio.sleep(0)
        release.set()
        await closing
        self.assertEqual(self.count("cancel"), 1)
        self.assertFalse(self.messenger.updates[-1][1].actions)
        self.assertNotIn("signin", self.messenger.updates[-1][1].text)


if __name__ == "__main__":
    unittest.main()
