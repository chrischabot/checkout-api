'use strict';

function newAuthState() {
  return { refreshToken: null, accessToken: null, expiresAt: 0 };
}

function resumeSession(raw) {
  const session = raw && typeof raw === 'object' ? raw : {};
  return {
    id: session.id || null,
    userId: session.userId || null,
    auth: session.auth || null,
  };
}

function refreshAccessToken(session, tokenService) {
  const auth = (session && session.auth) || newAuthState();
  const refreshToken = auth.refreshToken;
  if (!refreshToken) {
    return { ok: false, reason: 'no_refresh_token', session };
  }
  const next = tokenService.exchange(refreshToken);
  session.auth = { ...auth, ...next };
  return { ok: true, session };
}

module.exports = { newAuthState, resumeSession, refreshAccessToken };
