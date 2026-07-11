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
  // The cold-cache resume path (added alongside the session-lookup cache) can
  // yield a session whose `auth` blob is absent/null. Reading `.refreshToken`
  // straight off it throws TypeError before the no_refresh_token check runs.
  const refreshToken = session.auth && session.auth.refreshToken;
  if (!refreshToken) {
    return { ok: false, reason: 'no_refresh_token', session };
  }
  const next = tokenService.exchange(refreshToken);
  session.auth = { ...session.auth, ...next };
  return { ok: true, session };
}

module.exports = { newAuthState, resumeSession, refreshAccessToken };
