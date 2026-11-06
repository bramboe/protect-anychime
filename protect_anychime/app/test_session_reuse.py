"""One signed-in Protect session is reused between management actions.

Fake HTTP sessions only: no console, credentials on disk or audio.
"""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import management

HOST = '10.23.45.1'
PIN = 'f' * 64
ACCOUNT = {'host': HOST, 'username': 'local-user', 'password': 'private-test-password', 'fingerprint': PIN}


class Response:
    def __init__(self, data=None, *, status=200, headers=None):
        self.status = status; self.headers = headers or {}
        self.content = Mock(); self.content.read = AsyncMock(return_value=json.dumps(data).encode() if data is not None else b'')
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass


class Cookie:
    def __init__(self, key, value): self.key, self.value = key, value


def fake_session(*responses, cookies=()):
    session = Mock(); session.close = AsyncMock()
    session.request.side_effect = list(responses)
    session.cookie_jar = Mock(); session.cookie_jar.__iter__ = lambda self: iter([Cookie(k, v) for k, v in cookies])
    return session


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


class SessionCacheTests(unittest.TestCase):
    def test_entries_are_bound_to_console_pin_and_account_and_expire(self):
        clock = Clock(); cache = management.SessionCache(clock)
        cache.put(ACCOUNT, {'TOKEN': 'cookie'}, 'csrf', fresh=True)
        self.assertEqual(cache.get(ACCOUNT), ({'TOKEN': 'cookie'}, 'csrf'))
        for change in ({'password': 'other'}, {'username': 'other'}, {'fingerprint': 'e' * 64}, {'host': '10.23.45.2'}):
            with self.subTest(change=change):
                self.assertIsNone(cache.get({**ACCOUNT, **change}))
        clock.now += management.SessionCache.TTL
        self.assertIsNone(cache.get(ACCOUNT))

    def test_reuse_never_extends_the_session_and_unknown_sessions_are_not_stored(self):
        clock = Clock(); cache = management.SessionCache(clock)
        cache.put(ACCOUNT, {}, 'csrf', fresh=False)
        self.assertIsNone(cache.get(ACCOUNT))
        cache.put(ACCOUNT, {}, 'csrf', fresh=True)
        clock.now += management.SessionCache.TTL - 1
        cache.put(ACCOUNT, {}, 'newer', fresh=False)
        self.assertEqual(cache.get(ACCOUNT), ({}, 'newer'))
        clock.now += 1
        self.assertIsNone(cache.get(ACCOUNT))

    def test_password_is_not_kept_in_the_cache(self):
        cache = management.SessionCache(Clock())
        cache.put(ACCOUNT, {'TOKEN': 'cookie'}, 'csrf', fresh=True)
        self.assertNotIn('private-test-password', json.dumps(cache.entry))


class SessionReuseTests(unittest.IsolatedAsyncioTestCase):
    async def run_action(self, session, cache):
        with patch('management._PinnedConnector'), patch('management.aiohttp.ClientSession', return_value=session):
            async with management.ProtectSession(dict(ACCOUNT), cache=cache) as client:
                return await client.chimes()

    def paths(self, session):
        return [c.args[1].split(HOST, 1)[1] for c in session.request.call_args_list]

    async def test_second_action_reuses_the_session_without_signing_in(self):
        cache = management.SessionCache(Clock())
        first = fake_session(Response(headers={'x-csrf-token': 'csrf-1'}), Response([]), cookies=[('TOKEN', 'abc')])
        await self.run_action(first, cache)
        self.assertEqual(self.paths(first), ['/api/auth/login', '/proxy/protect/api/chimes'])
        second = fake_session(Response([]))
        await self.run_action(second, cache)
        self.assertEqual(self.paths(second), ['/proxy/protect/api/chimes'])
        self.assertEqual(second.request.call_args.kwargs['headers'], {'X-CSRF-Token': 'csrf-1'})
        second.cookie_jar.update_cookies.assert_called_once()
        self.assertEqual(second.cookie_jar.update_cookies.call_args.args[0], {'TOKEN': 'abc'})

    async def test_expired_session_signs_in_once_and_repeats_the_request(self):
        cache = management.SessionCache(Clock())
        cache.put(ACCOUNT, {'TOKEN': 'old'}, 'old-csrf', fresh=True)
        session = fake_session(Response(status=401), Response(headers={'x-csrf-token': 'csrf-2'}), Response([{'id': '1' * 24}]))
        result = await self.run_action(session, cache)
        self.assertEqual(result, [{'id': '1' * 24}])
        self.assertEqual(self.paths(session), ['/proxy/protect/api/chimes', '/api/auth/login', '/proxy/protect/api/chimes'])
        self.assertEqual(cache.get(ACCOUNT)[1], 'csrf-2')

    async def test_refused_new_sign_in_is_not_retried_and_clears_the_session(self):
        cache = management.SessionCache(Clock())
        cache.put(ACCOUNT, {}, 'old-csrf', fresh=True)
        session = fake_session(Response(status=401), Response(status=401))
        with self.assertRaises(management.ManagementError):
            await self.run_action(session, cache)
        self.assertEqual(session.request.call_count, 2)
        self.assertIsNone(cache.get(ACCOUNT))

    async def test_fresh_sign_in_failure_is_never_repeated(self):
        cache = management.SessionCache(Clock())
        session = fake_session(Response(status=429))
        with self.assertRaises(management.ManagementError):
            await self.run_action(session, cache)
        self.assertEqual(session.request.call_count, 1)
        self.assertIsNone(cache.get(ACCOUNT))

    async def test_refreshed_csrf_token_is_remembered(self):
        cache = management.SessionCache(Clock())
        cache.put(ACCOUNT, {}, 'csrf-1', fresh=True)
        session = fake_session(Response([], headers={'x-updated-csrf-token': 'csrf-3'}))
        await self.run_action(session, cache)
        self.assertEqual(cache.get(ACCOUNT)[1], 'csrf-3')


class ManagerSharesOneSessionTests(unittest.TestCase):
    def test_default_manager_actions_share_its_session_cache(self):
        manager = management.ProtectManagement('/nonexistent/protect-management.json')
        client = manager.client_factory(dict(ACCOUNT))
        self.assertIsInstance(client, management.ProtectSession)
        self.assertIs(client.cache, manager.sessions)
        manager.sessions.put(ACCOUNT, {}, 'csrf', fresh=True)
        manager.forget()
        self.assertIsNone(manager.sessions.get(ACCOUNT))


if __name__ == '__main__':
    unittest.main()
