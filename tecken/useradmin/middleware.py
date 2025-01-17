# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import logging
from urllib.parse import urlparse

import markus

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import MiddlewareNotUsed, PermissionDenied
from django.contrib import auth

from tecken.librequests import session_with_retries


logger = logging.getLogger("tecken")
metrics = markus.get_metrics("tecken")


class Auth0ManagementAPIError(Exception):
    """happens if the Auth0 management API can't be reached"""


@metrics.timer_decorator("useradmin_is_blocked_in_auth0")
def is_blocked_in_auth0(email):
    session = session_with_retries(total_retries=5)
    users = find_users(
        settings.OIDC_RP_CLIENT_ID,
        settings.OIDC_RP_CLIENT_SECRET,
        urlparse(settings.OIDC_OP_USER_ENDPOINT).netloc,
        email,
        session,
    )
    for user in users:
        if user.get("blocked"):
            return True
    return False


def _get_access_token(client_id, client_secret, domain, session):
    url = f"https://{domain}/oauth/token"
    audience = f"https://{domain}/api/v2/"
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "audience": audience,
    }
    response = session.post(url, json=payload)
    if response.status_code != 200:
        raise Auth0ManagementAPIError(response.status_code)
    return response.json()["access_token"]


def find_users(client_id, client_secret, domain, email, session):
    access_token = _get_access_token(client_id, client_secret, domain, session)

    url = f"https://{domain}/api/v2/users"
    query = {"q": f'email:"{email}"'}
    response = session.get(
        url, params=query, headers={"Authorization": f"Bearer {access_token}"}
    )
    if response.status_code != 200:
        raise Auth0ManagementAPIError(response.status_code)
    return response.json()


class NotBlockedInAuth0Middleware:
    """If the user is found in Auth0's User Management API *and*
    is "blocked" make the user inactive. In active users can't have any
    permissions but we don't have to destroy or change any API tokens.

    Also, if the user is made inactive, raise a 4xx error to stop the
    request. This is more explicit to the client.

    For simplicity, only proceed if the user can be *found* in Auth0.
    If she can't be found, perhaps the user never actually signed in once
    in Auth0. For example, relevant to existing users manually migrated
    from Socorro.

    The check is time consuming so the fact that the check was made is
    at throttled interval.
    """

    def __init__(self, get_response=None):
        if not settings.ENABLE_AUTH0_BLOCKED_CHECK:  # pragma: no cover
            logger.warning("Auth0 blocked check disabled")
            raise MiddlewareNotUsed
        self.get_response = get_response

    def __call__(self, request):
        response = self.process_request(request)
        if not response:
            response = self.get_response(request)
        return response

    def process_request(self, request):
        if not request.user.is_active or not request.user.email:
            return
        cache_key = f"NotBlockedInAuth0Middleware:${request.user.id}"
        if cache.get(cache_key) is None:
            # We have to do the check
            if is_blocked_in_auth0(request.user.email):
                # oh my!
                request.user.is_active = False
                request.user.save()
                logger.warning(
                    f"User {request.user.email} is blocked in Auth0 "
                    f"and now made inactive"
                )
                auth.logout(request)
                raise PermissionDenied("User is blocked in Auth0 and made inactive.")
            else:
                logger.info(f"User {request.user.email} is NOT blocked in Auth0")
            cache.set(cache_key, True, settings.NOT_BLOCKED_IN_AUTH0_INTERVAL_SECONDS)
