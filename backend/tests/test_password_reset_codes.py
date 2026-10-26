"""Password recovery must consume codes once and never expose their contents."""
import asyncio
import copy
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.application.errors.exceptions import AppException, BadRequestError
from app.application.services.email_service import EmailService
from app.infrastructure.external.cache.redis_cache import RedisCache


class AtomicCache:
    def __init__(self):
        self.values = {}
        self.lock = asyncio.Lock()

    async def set(self, key, value, ttl=None):
        self.values[key] = copy.deepcopy(value)
        return True

    async def set_if_absent(self, key, value, ttl):
        async with self.lock:
            if key in self.values:
                return False
            self.values[key] = value
            return True

    async def verify_one_time_code(self, key, digest, legacy_code):
        async with self.lock:
            value = self.values.get(key)
            if value is None:
                return False
            matches = value.get('code_hash') == digest
            value['attempts'] += 1
            if matches or value['attempts'] >= 3:
                del self.values[key]
            return matches


@pytest.fixture
def service(monkeypatch):
    result = EmailService(AtomicCache())
    result.settings = SimpleNamespace(jwt_secret_key='reset-unit-test-secret')
    monkeypatch.setattr(result, '_ensure_smtp_configured', lambda: None)
    monkeypatch.setattr(result, '_create_verification_email', lambda email, code: {'email': email, 'code': code})
    monkeypatch.setattr(result, '_send_smtp_email', AsyncMock())
    return result


@pytest.mark.asyncio
async def test_concurrent_requests_send_once_and_store_only_digest(service, monkeypatch, caplog):
    monkeypatch.setattr(service, '_generate_verification_code', lambda: '123456')
    with caplog.at_level(logging.DEBUG):
        responses = await asyncio.gather(*(service.send_verification_code(' Researcher@Example.com ') for _ in range(8)), return_exceptions=True)
    assert responses.count(None) == 1
    assert sum(isinstance(value, BadRequestError) for value in responses) == 7
    service._send_smtp_email.assert_awaited_once()
    record = service.cache.values['verification_code:researcher@example.com']
    assert 'code' not in record and len(record['code_hash']) == 64
    assert '123456' not in caplog.text
    assert await service.verify_code('RESEARCHER@example.com', '123456')
    with pytest.raises(BadRequestError, match='60 seconds'):
        await service.send_verification_code('researcher@example.com')


@pytest.mark.asyncio
async def test_code_is_single_use_and_cannot_be_replayed_for_another_account(service):
    await service._store_verification_code('alice@example.com', '234567')
    assert not await service.verify_code('bob@example.com', '234567')
    outcomes = await asyncio.gather(*(service.verify_code('alice@example.com', '234567') for _ in range(20)))
    assert outcomes.count(True) == 1


@pytest.mark.asyncio
async def test_three_wrong_attempts_exhaust_code(service):
    await service._store_verification_code('alice@example.com', '234567')
    for _ in range(3):
        assert not await service.verify_code('alice@example.com', '999999')
    assert not await service.verify_code('alice@example.com', '234567')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['reserve', 'persist', 'consume'])
async def test_storage_failure_fails_closed_without_email(service, failure):
    if failure == 'reserve':
        service.cache.set_if_absent = AsyncMock(side_effect=ConnectionError('private details'))
    elif failure == 'persist':
        service.cache.set = AsyncMock(return_value=False)
    else:
        service.cache.verify_one_time_code = AsyncMock(side_effect=ConnectionError('private details'))
    with pytest.raises(AppException) as error:
        if failure == 'consume':
            await service.verify_code('alice@example.com', '123456')
        else:
            await service.send_verification_code('alice@example.com')
    assert error.value.status_code == 503
    assert 'private details' not in str(error.value)
    service._send_smtp_email.assert_not_awaited()


def test_code_generation_uses_cryptographic_randomness(service, monkeypatch):
    monkeypatch.setattr('app.application.services.email_service.secrets.randbelow', lambda bound: bound - 1)
    assert service._generate_verification_code() == '999999'


@pytest.mark.asyncio
async def test_smtp_runs_off_the_event_loop(service, monkeypatch):
    # A slow mail server must not block authenticated requests in the API worker.
    called = []
    monkeypatch.setattr(service, '_send_smtp_email_sync', lambda message, email: called.append(email))
    await EmailService._send_smtp_email(service, {}, 'alice@example.com')
    assert called == ['alice@example.com']


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv('DATALAB_AUTH_TEST_REDIS_URL'), reason='Requires a disposable Redis test server')
async def test_real_redis_consumption_attempt_budget_ttl_and_legacy_codes():
    from redis.asyncio import Redis

    client = Redis.from_url(os.environ['DATALAB_AUTH_TEST_REDIS_URL'])
    cache = RedisCache()
    cache.redis_client = SimpleNamespace(initialize=AsyncMock(), client=client)
    prefix = 'datalab-reset-test:' + uuid4().hex
    keys = [prefix + suffix for suffix in (':single', ':attempts', ':legacy', ':invalid', ':reservation', ':expired')]
    try:
        await cache.set(keys[0], {'code_hash': 'digest', 'attempts': 0}, ttl=20)
        assert sum(await asyncio.gather(*(cache.verify_one_time_code(keys[0], 'digest', '123456') for _ in range(20)))) == 1
        await cache.set(keys[1], {'code_hash': 'digest', 'attempts': 0}, ttl=20)
        before = await client.pttl(keys[1])
        assert not await cache.verify_one_time_code(keys[1], 'wrong', '654321')
        assert 0 < await client.pttl(keys[1]) <= before
        await asyncio.gather(*(cache.verify_one_time_code(keys[1], 'wrong', '654321') for _ in range(10)))
        assert not await cache.verify_one_time_code(keys[1], 'digest', '123456')
        await cache.set(keys[2], {'code': '123456', 'attempts': 0}, ttl=20)
        assert await cache.verify_one_time_code(keys[2], 'digest', '123456')
        await client.set(keys[3], 'invalid', ex=20)
        assert not await cache.verify_one_time_code(keys[3], 'digest', '123456')
        assert not await client.exists(keys[3])
        assert sum(await asyncio.gather(*(cache.set_if_absent(keys[4], True, ttl=20) for _ in range(10)))) == 1
        await cache.set(keys[5], {'code_hash': 'digest', 'attempts': 0}, ttl=20)
        await client.pexpire(keys[5], 0)
        assert not await cache.verify_one_time_code(keys[5], 'digest', '123456')
    finally:
        await client.delete(*keys)
        await client.aclose()
