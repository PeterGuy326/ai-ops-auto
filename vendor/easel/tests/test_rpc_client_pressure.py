"""Exercise native client-request pressure with a real stdio child process."""
import asyncio
import os
import sys

import pytest

from easel.harnesses import rpc as transport
from easel.harnesses.base import HarnessError, RunRequest


CHILD = r'''
import json, sys
def send(value):
    print(json.dumps(value), flush=True)
def read():
    return json.loads(sys.stdin.readline())
request = read()
batch, rounds = map(int, sys.argv[1:])
for turn in range(rounds):
    for index in range(batch):
        send({"id": f"client-{turn}-{index}", "method": "ask", "params": {}})
        if batch == 4 and index < 3:
            # Ensure callbacks are actually waiting before the overload arrives.
            assert read()["method"] == "entered"
    for index in range(batch):
        read()
send({"id": request["id"], "result": {"done": True}})
send({"method": "done", "params": {}})
sys.stdin.read()
'''


@pytest.mark.parametrize("acp", [False, True])
@pytest.mark.parametrize("notification", [False, True])
@pytest.mark.parametrize("pooled", [False, True])
def test_overload_fails_waiter_and_cleans_pending_callbacks(tmp_path, monkeypatch, acp, notification, pooled):
    monkeypatch.setattr(transport, "MAX_CLIENT_REQUESTS", 3, raising=False)

    async def exercise():
        active = set()
        entered = []
        async def respond(*args):
            task = asyncio.current_task()
            active.add(task)
            entered.append(task)
            try:
                await rpc.send({"method": "entered", "params": {}})
                await asyncio.Event().wait()
            finally:
                active.remove(task)
        request = RunRequest("", tmp_path, dict(os.environ), session_key="pressure" if pooled else "")
        with pytest.raises(HarnessError, match="客户端请求并发超过上限"):
            async with transport.connection(request, [sys.executable, "-c", CHILD, "4", "1"],
                                            lambda *args: None, respond, acp=acp) as rpc:
                if notification:
                    await rpc.send({"id": 1, "method": "start"})
                    await asyncio.wait_for(rpc.wait_notification(asyncio.get_running_loop().create_future()), 5)
                else:
                    await asyncio.wait_for(rpc.call("start", {}, timeout=4), 5)
        assert len(entered) == 3
        assert not active
        assert all(task.done() for task in rpc.tasks)
        assert rpc.process.returncode is not None
        assert rpc.reader.done() and rpc.stderr.done()
        assert not transport._POOLS.get(asyncio.get_running_loop())
    asyncio.run(exercise())


@pytest.mark.parametrize("acp", [False, True])
def test_completed_requests_release_capacity_across_batches(tmp_path, monkeypatch, acp):
    monkeypatch.setattr(transport, "MAX_CLIENT_REQUESTS", 3, raising=False)

    async def exercise():
        completed = []
        async def respond(method, params):
            await asyncio.sleep(0)
            completed.append(method)
            return {}
        request = RunRequest("", tmp_path, dict(os.environ))
        async with transport.RPC(request, [sys.executable, "-c", CHILD, "3", "3"],
                                 lambda *args: None, respond, acp=acp) as rpc:
            assert await rpc.call("start", {}, timeout=5) == {"done": True}
        assert completed == ["ask"] * 9
    asyncio.run(exercise())
