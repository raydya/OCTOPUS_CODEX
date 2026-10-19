"""Controlled app-server peer for tests. Does not import or execute Codex."""

import argparse
import json
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--record", required=True)
parser.add_argument("--scenario", default="normal")
args, _ = parser.parse_known_args()
active = args.scenario in {"active", "unknown-active"}


def send(message):
    print(json.dumps(message), flush=True)


def notify(method, **params):
    send({"method": method, "params": {"threadId": "thread-1", "turnId": "turn-1", **params}})


def approval(request_id=7, decisions=None):
    send({"id": request_id, "method": "item/commandExecution/requestApproval", "params": {
        "threadId": "thread-1", "turnId": "turn-1", "itemId": "item-cmd", "approvalId": "NOT_THE_RPC_ID",
        "kind": "command", "startedAtMs": 123, "environmentId": None, "command": "echo safe",
        "cwd": "/tmp/project", "reason": "test", "availableDecisions": decisions or ["accept", "decline"],
    }})


def complete(status="completed", items=None):
    notify("turn/completed", turn={"id": "turn-1", "status": status, "items": items or [], "error": None})


for line in sys.stdin:
    message = json.loads(line)
    with open(args.record, "a", encoding="utf8") as stream:
        stream.write(json.dumps(message) + "\n")
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        if args.scenario == "init-timeout":
            continue
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        pass
    elif method in {"thread/start", "thread/resume"}:
        if args.scenario == "rpc-error":
            send({"id": request_id, "error": {"code": -32602, "message": "invalid cwd"}})
        else:
            send({"id": request_id, "result": {"thread": {"id": "thread-1", "status": {"type": "active" if active else "idle"}}}})
    elif method == "thread/read":
        send({"id": request_id, "result": {"thread": {"id": "thread-1", "status": {"type": "active" if active else "idle"}}}})
    elif method == "thread/turns/list":
        send({"id": request_id, "result": {"data": [
            {"id": "old-turn", "status": "inProgress", "items": []}
        ] if active and args.scenario != "unknown-active" else [], "nextCursor": None}})
    elif method == "turn/start":
        if args.scenario == "timeout":
            continue
        if args.scenario == "disconnect":
            sys.exit(23)
        if args.scenario == "invalid-json":
            print("not-json", flush=True)
            continue
        send({"id": request_id, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
        if args.scenario == "normal":
            notify("item/started", item={"type": "agentMessage", "id": "progress", "text": "", "phase": "commentary"})
            notify("item/agentMessage/delta", itemId="progress", delta="checking")
            notify("item/completed", item={"type": "agentMessage", "id": "progress", "text": "checking", "phase": "commentary"})
            notify("item/started", item={"type": "commandExecution", "id": "cmd", "command": "echo ok"})
            notify("item/commandExecution/outputDelta", itemId="cmd", delta="ok")
            notify("item/agentMessage/delta", itemId="answer", delta="partial")
            notify("item/completed", item={"type": "agentMessage", "id": "answer", "text": "最终答案", "phase": "final_answer"})
            complete()
        elif args.scenario in {"approval", "resolved", "restricted", "approval-disconnect"}:
            approval(decisions=["decline"] if args.scenario == "restricted" else None)
            if args.scenario == "resolved":
                notify("serverRequest/resolved", requestId=7)
            if args.scenario == "approval-disconnect":
                sys.exit(24)
        elif args.scenario == "question":
            send({"id": "question-rpc", "method": "item/tool/requestUserInput", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "itemId": "question-item", "isBlocking": True,
                "questions": [{"id": "scope", "header": "范围", "question": "哪个模块？", "isOther": True,
                    "isSecret": False, "options": [{"label": "全部", "description": "所有模块"}]}],
            }})
        elif args.scenario == "unsupported":
            send({"id": "unknown", "method": "item/tool/call", "params": {}})
            send({"id": "permissions", "method": "item/permissions/requestApproval", "params": {
                "permissions": {"network": {"enabled": True}},
            }})
        elif args.scenario == "pending":
            pass
    elif method == "turn/interrupt":
        active = False
        send({"id": request_id, "result": {}})
        complete("interrupted")
    elif request_id is not None and "result" in message:
        if request_id in (7, "question-rpc"):
            notify("serverRequest/resolved", requestId=request_id)
            complete()
        if request_id == "permissions":
            complete()
