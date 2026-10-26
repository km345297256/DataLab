"""Exercise actual application shutdown with synthetic dependency owners."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main


@pytest.fixture
def resources(monkeypatch):
    calls = []
    services = {}
    for name in ("pool", "node", "agent", "analysis", "approval", "plugin", "redis", "mongo"):
        async def shutdown(name=name):
            calls.append(name)
        services[name] = SimpleNamespace(shutdown=AsyncMock(side_effect=shutdown))
    services["node"].stop = services["node"].shutdown
    for field in ("jupyter_reaper_task", "spill_reaper_task", "analysis_job_monitor_task", "open_data_worker_task"):
        monkeypatch.setattr(main, field, None)
    monkeypatch.setattr(main, "execution_node_monitor", services["node"])
    monkeypatch.setattr(main, "get_sandbox_pool", lambda: services["pool"])
    monkeypatch.setattr(main, "set_sandbox_pool", lambda value: None)
    for name, getter in (("agent", "get_agent_service"), ("analysis", "get_analysis_job_service"),
                         ("approval", "get_tool_approval_service"), ("redis", "get_redis"), ("mongo", "get_mongodb")):
        monkeypatch.setattr(main, getter, lambda name=name: services[name])
    return SimpleNamespace(state=SimpleNamespace()), calls, services


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pool", "node", "agent", "analysis", "approval", "plugin", "redis", "mongo"])
async def test_one_failure_does_not_skip_remaining_cleanup(resources, failure):
    app, calls, services = resources

    async def fail():
        calls.append(failure)
        raise RuntimeError(failure)

    services[failure].shutdown.side_effect = fail
    for _ in range(2):
        with pytest.raises(RuntimeError, match=failure):
            await main._shutdown_application(app, services["plugin"])
    assert calls == ["node", "pool", "agent", "analysis", "approval", "plugin", "redis", "mongo"]
    for service in services.values():
        service.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_multiple_failures_remain_observable(resources):
    app, calls, services = resources
    services["analysis"].shutdown.side_effect = ValueError("analysis")
    services["redis"].shutdown.side_effect = RuntimeError("redis")
    with pytest.raises(ExceptionGroup) as caught:
        await main._shutdown_application(app, services["plugin"])
    assert [type(error) for error in caught.value.exceptions] == [ValueError, RuntimeError]
    assert services["mongo"].shutdown.await_count == 1
    assert services["approval"].shutdown.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_resource_does_not_skip_remaining_cleanup(resources):
    app, calls, services = resources
    services["analysis"].shutdown.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await main._shutdown_application(app, services["plugin"])
    services["approval"].shutdown.assert_awaited_once()
    services["mongo"].shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeated_caller_cancel_waits_for_final_write_before_store_close(resources):
    app, calls, services = resources
    entered, release = asyncio.Event(), asyncio.Event()

    async def final_write():
        calls.append("agent")
        entered.set()
        await release.wait()
        assert services["redis"].shutdown.await_count == 0
        assert services["mongo"].shutdown.await_count == 0
        calls.append("final-write")

    services["agent"].shutdown.side_effect = final_write
    first = asyncio.create_task(main._shutdown_application(app, services["plugin"]))
    second = asyncio.create_task(main._shutdown_application(app, services["plugin"]))
    await entered.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert calls.index("final-write") < calls.index("redis") < calls.index("mongo")
    for service in services.values():
        service.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_producer_settles_before_services_close(resources, monkeypatch):
    app, calls, services = resources
    running, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def producer():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            assert not calls
            calls.append("producer-settled")

    producer_task = asyncio.create_task(producer())
    monkeypatch.setattr(main, "analysis_job_monitor_task", producer_task)
    await running.wait()
    closing = asyncio.create_task(main._shutdown_application(app, services["plugin"]))
    await cleaning.wait()
    assert not closing.done() and not calls
    release.set()
    await closing
    assert calls[0] == "producer-settled"
    assert main.analysis_job_monitor_task is None


@pytest.mark.asyncio
async def test_lifespan_cancellation_drains_open_data_before_remaining_shutdown(resources, monkeypatch):
    from unittest.mock import Mock
    from app.application.services import open_data_service
    app, calls, services = resources
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def background():
        await asyncio.Event().wait()

    async def open_data():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            assert not calls
            calls.append("open-data-settled")

    monkeypatch.setattr(main.settings, "auth_provider", "password")
    monkeypatch.setattr(main.settings, "jwt_secret_key", "x" * 32)
    monkeypatch.setattr(main.settings, "sandbox_isolation", "shared")
    services["mongo"].initialize = AsyncMock()
    services["mongo"].client = {main.settings.mongodb_database: object()}
    services["redis"].initialize = AsyncMock()
    services["node"].start = Mock()
    services["plugin"].start = AsyncMock(return_value=SimpleNamespace(revision="revision", plugin_count=0, tool_count=0))
    services["agent"].start_input_recovery = Mock()
    monkeypatch.setattr(main, "get_auth_service", lambda: SimpleNamespace(bootstrap_admin=AsyncMock()))
    monkeypatch.setattr(main, "init_beanie", AsyncMock())
    monkeypatch.setattr(main, "ensure_safety_rule_seeds", AsyncMock())
    monkeypatch.setattr(main, "ensure_local_default_node", AsyncMock(return_value=SimpleNamespace(runtime_config={})))
    monkeypatch.setattr(main, "ExecutionNodeMonitor", lambda **kwargs: services["node"])
    monkeypatch.setattr(main, "get_plugin_runtime", lambda: services["plugin"])
    for name in ("_reap_idle_jupyter_sessions", "_reap_expired_spill_artifacts", "_maintain_analysis_jobs"):
        monkeypatch.setattr(main, name, background)
    monkeypatch.setattr(open_data_service, "run_open_data_worker", open_data)
    lifespan = main.lifespan(app)
    await lifespan.__aenter__()
    await started.wait()
    closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
    await cleaning.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and not calls
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert calls == ["open-data-settled", "node", "pool", "agent", "analysis", "approval", "plugin", "redis", "mongo"]
    assert main.open_data_worker_task is None
    for service in services.values():
        service.shutdown.assert_awaited_once()
