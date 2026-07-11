'use strict';
// Regression test for INC-1.
//
// Incident: PR #1 ("perf: cache session lookups", merged 2026-07-10) added a
// cold-cache resume path AND changed refreshAccessToken() to read
// `session.auth.refreshToken` directly. resumeSession() normalizes a missing
// auth blob to `auth: null`, so on a cold-cache resume the direct read threw
//   TypeError: Cannot read properties of null (reading 'refreshToken')
// BEFORE the function's own no_refresh_token guard could run. Warm-cache
// traffic was unaffected, which is why it presented as a partial, traffic-
// dependent auth failure rather than a hard outage.
//
// These tests pin the contract so the same class of defect cannot ship again.

const test = require('node:test');
const assert = require('node:assert');

const {
  newAuthState,
  resumeSession,
  refreshAccessToken,
} = require('../service/checkout/session.js');

const tokenService = {
  exchange: (rt) => ({ accessToken: 'at_' + rt, refreshToken: rt, expiresAt: 9999 }),
};

// Every raw record shape a cold-cache lookup can hand to resumeSession().
const COLD_CACHE_SHAPES = [
  ['auth key absent entirely', { id: 's1', userId: 'u1' }],
  ['auth explicitly null', { id: 's2', userId: 'u2', auth: null }],
  ['auth undefined', { id: 's3', userId: 'u3', auth: undefined }],
  ['empty record', {}],
  ['non-object record', null],
];

test('cold-cache resume degrades gracefully instead of throwing (INC-1)', async (t) => {
  for (const [label, raw] of COLD_CACHE_SHAPES) {
    await t.test(label, () => {
      const session = resumeSession(raw);

      // The regression itself: this must not throw.
      const out = refreshAccessToken(session, tokenService);

      assert.strictEqual(out.ok, false, 'cold resume must not report success');
      assert.strictEqual(
        out.reason,
        'no_refresh_token',
        'cold resume must reach the no_refresh_token branch'
      );
      assert.ok(out.session, 'the session must still be returned to the caller');
    });
  }
});

test('a present-but-empty auth blob also degrades gracefully', () => {
  const session = resumeSession({ id: 's', userId: 'u', auth: newAuthState() });
  const out = refreshAccessToken(session, tokenService);
  assert.strictEqual(out.ok, false);
  assert.strictEqual(out.reason, 'no_refresh_token');
});

test('warm-cache refresh still exchanges and merges tokens (PR #1 perf intent)', () => {
  const session = resumeSession({
    id: 's9',
    userId: 'u9',
    auth: { refreshToken: 'rt_live', accessToken: 'stale', expiresAt: 1 },
  });

  const out = refreshAccessToken(session, tokenService);

  assert.strictEqual(out.ok, true, 'warm-cache refresh must still succeed');
  assert.strictEqual(out.session.auth.accessToken, 'at_rt_live', 'new access token must be applied');
  assert.strictEqual(out.session.auth.expiresAt, 9999, 'new expiry must be applied');
  assert.strictEqual(out.session.auth.refreshToken, 'rt_live', 'refresh token must be preserved');
});

test('resumeSession normalizes shape and never throws', () => {
  for (const [, raw] of COLD_CACHE_SHAPES) {
    const s = resumeSession(raw);
    assert.ok('id' in s && 'userId' in s && 'auth' in s, 'normalized shape must be complete');
  }
});

test('module exports the expected public surface', () => {
  assert.deepStrictEqual(
    Object.keys(require('../service/checkout/session.js')).sort(),
    ['newAuthState', 'refreshAccessToken', 'resumeSession']
  );
});
