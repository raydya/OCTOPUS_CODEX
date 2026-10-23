from __future__ import annotations

import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from octopus.codex import AppServerClient, TOPIC_ROUTING_SCHEMA, _topic_result
from octopus.images import ImageInputError


RESULT = {
    'action': 'continue', 'topic_id': 'payment', 'title': '支付超时',
    'summary': '调查订单 123 今天支付超时的原因', 'reason': '继续排查同一订单',
    'clarification': '', 'use_pending': False, 'context_ids': ['message-1'],
}


class TopicCodexTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client = AppServerClient(model='business-model', reasoning_effort='xhigh')
        client._ready = True
        client._transport = object()
        client._write = AsyncMock()
        client.tool_handler = AsyncMock()
        self.events = []
        client.on_event(self.events.append)
        self.next_id = 0
        self.responses = {}

        async def request(method, params, **kwargs):
            if method == 'config/read':
                return {'config': {'mcp_servers': {'grafana': {}, 'with.dot': {}}}}
            if method == 'thread/start':
                self.next_id += 1
                thread_id = f'private-{self.next_id}'
                return {'thread': {'id': thread_id, 'ephemeral': True, 'environments': []},
                        'instructionSources': [], 'approvalPolicy': 'never',
                        'sandbox': {'type': 'readOnly', 'networkAccess': False}}
            if method == 'mcpServerStatus/list':
                return {'data': [], 'nextCursor': None}
            if method == 'turn/start':
                thread_id = params['threadId']
                self.responses[thread_id] = params
                return {'turn': {'id': f'turn-{thread_id}'}}
            return {}

        client._request = AsyncMock(side_effect=request)
        return client

    async def wait_turns(self, count):
        async with asyncio.timeout(1):
            while len(self.responses) < count:
                await asyncio.sleep(0)

    def complete(self, client, thread_id, result=RESULT, *, status='completed', snapshot=False):
        item = {'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer',
                'text': result if isinstance(result, str) else json.dumps(result)}
        if not snapshot:
            client._notification('item/completed', {'threadId': thread_id, 'turnId': f'turn-{thread_id}', 'item': item})
        client._notification('turn/completed', {'threadId': thread_id, 'turn': {
            'id': f'turn-{thread_id}', 'status': status, 'items': [item] if snapshot else [],
        }})

    async def test_isolated_ephemeral_low_effort_uses_same_model_and_no_business_policy(self):
        client = self.client()
        task = asyncio.create_task(client.classify_topic('/workspace', {'topics': [{'id': 'payment'}]}))
        await self.wait_turns(1)
        start = next(call.args[1] for call in client._request.await_args_list if call.args[0] == 'thread/start')
        self.assertEqual(start['model'], 'business-model')
        self.assertTrue(start['ephemeral'])
        self.assertEqual(start['environments'], [])
        self.assertEqual(start['dynamicTools'], [])
        self.assertEqual(start['approvalPolicy'], 'never')
        self.assertEqual(start['sandbox'], 'read-only')
        config = start['config']
        self.assertEqual(config['mcp_servers'], {'grafana': {'enabled': False}, 'with.dot': {'enabled': False}})
        for key in ('features.shell_tool', 'features.view_image', 'features.code_mode_host.enabled',
                    'features.skill_search', 'skills.include_instructions', 'skills.bundled.enabled',
                    'tools.update_plan.enabled'):
            self.assertFalse(config[key], key)
        self.assertTrue(config['features.skip_host_skill_discovery'])
        self.assertEqual(config['features.code_mode']['excluded_tool_namespaces'], ['functions', 'clock'])
        self.assertEqual(config['features.code_mode']['direct_only_tool_namespaces'], [])
        self.assertEqual(config['project_doc_max_bytes'], 0)
        turn = self.responses['private-1']
        self.assertEqual(turn['effort'], 'low')
        self.assertEqual(turn['environments'], [])
        self.assertEqual(turn['outputSchema'], TOPIC_ROUTING_SCHEMA)
        self.assertNotIn('additionalContext', turn)
        self.assertEqual(client.reasoning_effort, 'xhigh')
        self.complete(client, 'private-1')
        self.assertEqual(await task, RESULT)
        client._request.assert_awaited_with('thread/unsubscribe', {'threadId': 'private-1'})
        self.assertEqual(self.events, [])
        self.assertEqual(client._topic_turns, {})

    async def test_parallel_classifiers_and_business_notifications_are_isolated(self):
        client = self.client()
        tasks = [asyncio.create_task(client.classify_topic('/workspace', {'number': n})) for n in (1, 2)]
        await self.wait_turns(2)
        client._notification('item/agentMessage/delta', {'threadId': 'private-1', 'turnId': 't', 'itemId': 'a', 'delta': 'private text'})
        client._notification('item/agentMessage/delta', {'threadId': 'business', 'turnId': 't', 'itemId': 'a', 'delta': 'normal answer'})
        second = {**RESULT, 'action': 'new', 'topic_id': None, 'title': '独立问题'}
        self.complete(client, 'private-2', second, snapshot=True)
        self.complete(client, 'private-1')
        self.assertEqual(await asyncio.gather(*tasks), [RESULT, second])
        self.assertEqual([(e.thread_id, e.delta) for e in self.events], [('business', 'normal answer')])
        # Late notifications still cannot reach the bridge after unsubscribe.
        client._notification('item/agentMessage/delta', {'threadId': 'private-1', 'turnId': 't', 'itemId': 'a', 'delta': 'late'})
        self.assertEqual(len(self.events), 1)

    async def test_tools_approvals_and_questions_never_reach_business_handlers(self):
        for method in ('item/tool/call', 'item/permissions/requestApproval',
                       'mcpServer/elicitation/request', 'item/commandExecution/requestApproval',
                       'item/fileChange/requestApproval', 'item/tool/requestUserInput'):
            with self.subTest(method=method):
                client = self.client()
                task = asyncio.create_task(client.classify_topic('/workspace', {}))
                await self.wait_turns(1)
                await client._server_request('private-request', method, {
                    'threadId': 'private-1', 'turnId': 'turn-private-1', 'tool': 'tls_search', 'arguments': {},
                    'questions': [{'id': 'x', 'question': 'read secrets?'}],
                })
                with self.assertRaisesRegex(RuntimeError, '不允许'):
                    await task
                client.tool_handler.assert_not_awaited()
                self.assertFalse(client._prompts)
                self.assertFalse(self.events)
                methods = [call.args[0] for call in client._request.await_args_list]
                self.assertIn('turn/interrupt', methods)
                self.assertEqual(methods[-1], 'thread/unsubscribe')

    async def test_instruction_leak_fails_before_model_turn(self):
        client = self.client()
        original = client._request.side_effect

        async def request(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == 'thread/start':
                result['instructionSources'] = ['/host/AGENTS.md']
            if method == 'thread/read':
                result = {'thread': {'status': {'type': 'idle'}}}
            return result

        client._request.side_effect = request
        with self.assertRaisesRegex(RuntimeError, '隔离配置'):
            await client.classify_topic('/workspace', {})
        self.assertFalse(self.responses)
        client._request.assert_awaited_with('thread/unsubscribe', {'threadId': 'private-1'})

    async def test_only_verified_global_agents_source_is_allowed(self):
        client = self.client()
        client._read_only_home = '/host/.data/codex-readonly/home'
        original = client._request.side_effect

        async def request(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == 'thread/start':
                result['instructionSources'] = [client._read_only_home + '/AGENTS.md']
            return result

        client._request.side_effect = request
        task = asyncio.create_task(client.classify_topic('/workspace', {}))
        await self.wait_turns(1)
        self.complete(client, 'private-1')
        self.assertEqual(await task, RESULT)

    async def test_visible_mcp_tool_fails_before_model_turn(self):
        client = self.client()
        original = client._request.side_effect

        async def request(method, params, **kwargs):
            if method == 'mcpServerStatus/list':
                return {'data': [{'name': 'grafana', 'tools': {'query': {}}}], 'nextCursor': None}
            if method == 'thread/read':
                return {'thread': {'status': {'type': 'idle'}}}
            return await original(method, params, **kwargs)

        client._request.side_effect = request
        with self.assertRaisesRegex(RuntimeError, '工具隔离'):
            await client.classify_topic('/workspace', {})
        self.assertFalse(self.responses)
        client._request.assert_awaited_with('thread/unsubscribe', {'threadId': 'private-1'})

    async def test_timeout_and_cancellation_interrupt_only_private_turn(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                client = self.client()
                with patch('octopus.codex.TOPIC_ROUTING_TIMEOUT', .03):
                    task = asyncio.create_task(client.classify_topic('/workspace', {}))
                    await self.wait_turns(1)
                    if cancel:
                        task.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                        await task
                client._request.assert_any_await('turn/interrupt', {'threadId': 'private-1', 'turnId': 'turn-private-1'})
                client._request.assert_awaited_with('thread/unsubscribe', {'threadId': 'private-1'})
                self.assertEqual(client._topic_turns, {})
                self.assertFalse(self.events)

    async def test_disconnect_fails_without_waiting_for_overall_timeout(self):
        client = self.client()
        task = asyncio.create_task(client.classify_topic('/workspace', {}))
        await self.wait_turns(1)
        client._lose_connection(client._transport, 'synthetic disconnect')
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(task, .2)
        self.assertEqual([e.type for e in self.events], ['disconnect'])

    async def test_invalid_image_fails_before_rpc(self):
        client = self.client()
        with self.assertRaises(ImageInputError):
            await client.classify_topic('/workspace', {}, images=('https://example.com/picture.png',))
        client._request.assert_not_awaited()

    async def test_closed_thread_tombstones_are_bounded_and_reject_late_tool_calls(self):
        client = self.client()
        for number in range(1030):
            thread_id = f'finished-{number}'
            client._private_threads.add(thread_id)
            client._notification('thread/closed', {'threadId': thread_id})
        self.assertEqual(len(client._private_threads), 1024)
        self.assertEqual(len(client._closed_private_threads), 1024)
        await client._server_request('late', 'item/tool/call', {
            'threadId': 'finished-1029', 'turnId': 'old', 'tool': 'tls_search', 'arguments': {},
        })
        client.tool_handler.assert_not_awaited()
        self.assertFalse(client._write.await_args.args[0]['result']['success'])
        self.assertFalse(self.events)
        client._lose_connection(client._transport, 'finished')
        self.assertFalse(client._private_threads)
        self.assertFalse(client._closed_private_threads)

    async def test_attestation_preserves_verified_snapshot_during_parallel_routing(self):
        client = self.client()
        client.read_only = True
        client._read_only_cwd = '/workspace'
        client._read_only_developer_instructions = 'Verified business policy'
        client._read_only_mcp_servers = ('grafana',)
        pending = asyncio.get_running_loop().create_future()

        async def read(*args, **kwargs):
            return await pending

        client._request.side_effect = read
        task = asyncio.create_task(client._attest_read_only())
        await asyncio.sleep(0)
        self.assertEqual(client._thread_permissions('/workspace')['developerInstructions'], 'Verified business policy')
        # Invalid refreshes still clear the snapshot and fail closed.
        pending.set_result({'config': {}})
        with self.assertRaises(RuntimeError):
            await task
        self.assertIsNone(client._read_only_cwd)
        self.assertIsNone(client._read_only_developer_instructions)
        self.assertEqual(client._read_only_mcp_servers, ())

    async def test_malformed_final_does_not_route_or_emit_progress(self):
        for output in ('not json', '{"action":"new"}', 'x' * 16001):
            with self.subTest(output=output[:20]):
                client = self.client()
                task = asyncio.create_task(client.classify_topic('/workspace', {}))
                await self.wait_turns(1)
                self.complete(client, 'private-1', output)
                with self.assertRaises(ValueError):
                    await task
                self.assertFalse(self.events)

    def test_schema_rejects_invalid_actions_ids_lengths_and_context(self):
        mutations = [
            {'action': 'execute'}, {'action': []}, {'topic_id': None},
            {'action': 'new'}, {'title': 'a' * 121}, {'summary': 'a' * 1201},
            {'reason': 'a' * 301}, {'clarification': 'a' * 201},
            {'use_pending': 1}, {'context_ids': [7]}, {'context_ids': ['x', 'x']},
            {'context_ids': ['x'] * 25}, {'extra': 'unexpected'},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                _topic_result(json.dumps({**copy.deepcopy(RESULT), **mutation}))
        clarify = {**RESULT, 'action': 'clarify', 'topic_id': None, 'title': '', 'clarification': '是哪个订单？'}
        self.assertEqual(_topic_result(json.dumps(clarify)), clarify)


if __name__ == '__main__':
    unittest.main()
