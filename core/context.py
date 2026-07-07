# RequestContext — the (tenant, user_id, bearer) triple resolved once per palette
# action. Replaces the duplicated _get_fusion_user_id / _get_bearer / get_tenant
# helpers that the old add-in re-implemented in all eight command modules.

from . import auth


def get_fusion_user_id(app):
    """Return the Fusion user id/email as a string, or None.

    Tries several property names because the attribute has differed across Fusion
    API versions (user / currentUser; userId / user_id / id).
    """
    try:
        user = getattr(app, 'user', None) or getattr(app, 'currentUser', None)
        if user is not None:
            uid = (getattr(user, 'userId', None) or getattr(user, 'user_id', None)
                   or getattr(user, 'id', None))
            if uid:
                return str(uid)
    except Exception:
        pass
    return None


class RequestContext:
    """Carries the identity needed for every FM request.

    tenant  -- Fusion Manage tenant from the active hub (auth.get_tenant).
    user_id -- Fusion user id / email for the x-User-id header.
    bearer  -- valid APS access token (auth.get_valid_access_token).
    """

    __slots__ = ('app', 'tenant', 'user_id', 'bearer')

    def __init__(self, app, tenant, user_id, bearer):
        self.app = app
        self.tenant = tenant
        self.user_id = user_id
        self.bearer = bearer

    @classmethod
    def resolve(cls, app):
        """Resolve a context, or return None if not usable (no tenant or no token).

        A None return means "not signed in / not ready" — callers should surface the
        uniform unauthorized response rather than attempting an API call.
        """
        tenant = auth.get_tenant(app)
        if not tenant:
            return None
        bearer, _ = auth.get_valid_access_token()
        if not bearer:
            return None
        user_id = get_fusion_user_id(app)
        return cls(app, tenant, user_id, bearer)

    def refresh_bearer(self):
        """Force a real token refresh after a 401 (used by FmClient's refresh-retry).

        A server-side 401 can arrive on a token that is still within the local expiry
        buffer, so force=True bypasses the buffered cache and exchanges the refresh
        token. Returns the new token, or None if a valid token could not be obtained.
        """
        bearer, _ = auth.get_valid_access_token(force=True)
        self.bearer = bearer
        return bearer
