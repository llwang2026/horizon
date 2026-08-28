# Copyright (c) 2026 Your Organization
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Firewall-only UI restriction middleware for OpenStack Horizon.

Goal: a low-privilege SRE account (which still carries the ``admin`` role so
that it can list *all tenants'* firewalls through the admin context) should,
in the Horizon web UI, only ever see the firewall-related panels and nothing
else (no networks, routers, instances, volumes, identity, settings, ...).

How it works
------------
Horizon decides panel/dashboard visibility via ``allowed(self, context)``
(panels) and ``can_access(self, context)`` (dashboards). Both receive a
``RequestContext`` and read the current request from ``context['request']``.

This middleware wraps those methods **once at first request** (thread-safe,
idempotent). The wrapped methods make the decision *at call time* based on
``request.user``, so there is no shared mutable state and no cross-request
leak even under concurrent multi-threaded WSGI.

A user is "restricted" when it matches either:
  * FIREWALL_ONLY_ROLES  (default: ['fw_admin'])  -- recommended, role-based
  * FIREWALL_ONLY_USERS  (default: [])            -- by username, fallback

Restricted users:
  * see only panels whose slug matches FIREWALL_ONLY_PANEL_SLUGS
    (default: any slug containing "firewall")
  * cannot access the dashboards listed in FIREWALL_ONLY_HIDDEN_DASHBOARDS
    (default: admin, identity, settings)
  * are bounced (HTTP 302) to the firewall landing page whenever they hit any
    non-firewall URL (incl. the project overview, or a stray /network/ link),
    instead of being shown the generic "no permission" page. The landing URL
    is the first firewall panel found, or FIREWALL_ONLY_LANDING if set.

Non-restricted users (real admins) are completely unaffected.

This ships as a built-in, always-loaded middleware (see MIDDLEWARE in
openstack_dashboard/settings.py) exactly like horizon.middleware.
HorizonMiddleware or horizon.middleware.SimultaneousSessionsMiddleware. Its
defaults (openstack_dashboard/defaults.py) make it a no-op for everyone
except users who are explicitly given the FIREWALL_ONLY_ROLES role(s) --
no local_settings.py edit is required to turn it on.
"""

import threading

from django.conf import settings
from django.shortcuts import redirect
from django.utils.deprecation import MiddlewareMixin
from horizon import Horizon


_LOCK = threading.Lock()
_WRAPPED = False
# Populated in _wrap(): URL prefixes of firewall panels + the landing URL that
# restricted users are bounced to.
_FW_PREFIXES = []
_LANDING = "/dashboard/project/firewalls/"


def _is_restricted(user):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    cfg_roles = [r.lower() for r in settings.FIREWALL_ONLY_ROLES]
    cfg_users = settings.FIREWALL_ONLY_USERS
    if cfg_roles:
        roles = {r["name"].lower() for r in getattr(user, "roles", [])}
        if roles.intersection(cfg_roles):
            return True
    if cfg_users and getattr(user, "username", None) in cfg_users:
        return True
    return False


def _panel_is_firewall(panel):
    slugs = settings.FIREWALL_ONLY_PANEL_SLUGS
    slug = (getattr(panel, "slug", "") or "").lower()
    if slugs:
        return slug in {s.lower() for s in slugs}
    return "firewall" in slug


def _is_firewall_path(path):
    """True if *path* points at a firewall panel (so restricted users may stay)."""
    for p in _FW_PREFIXES:
        if path.startswith(p + "/") or path == p:
            return True
    return False


def _request_from(context):
    if context is None:
        return None
    if isinstance(context, dict):
        return context.get("request")
    return getattr(context, "request", None)


def _make_can_access(orig):
    """Build a replacement for ``Dashboard.can_access`` (a class method)."""
    def can_access(self, context):
        request = _request_from(context)
        if _is_restricted(getattr(request, "user", None)):
            return False
        return orig(self, context) if orig else True
    return can_access


def _make_allowed(orig, allow):
    """Build a replacement for ``Panel.allowed`` / ``Dashboard.allowed``.

    ``allow`` is the boolean this component resolves to for a restricted
    user (True only for firewall panels).
    """
    def allowed(self, context):
        request = _request_from(context)
        if _is_restricted(getattr(request, "user", None)):
            return allow
        return orig(self, context) if orig else True
    return allowed


def _wrap():
    global _WRAPPED
    if _WRAPPED:
        return
    with _LOCK:
        if _WRAPPED:
            return

        hidden_dashboards = {
            d.lower() for d in settings.FIREWALL_ONLY_HIDDEN_DASHBOARDS
        }

        # NOTE: we patch the *class* methods (type(dash).can_access /
        # type(panel).allowed), NOT instance attributes. An instance
        # attribute that is a plain function is NOT auto-bound to ``self`` by
        # Python, so Horizon's ``self.allowed(context)`` call would receive
        # only one argument and crash with
        #   TypeError: allowed() missing 1 required positional argument
        # Patching the class keeps the method a proper bound method, and also
        # survives Horizon re-instantiating the component.
        for dash in Horizon._registry.values():
            dash_cls = type(dash)

            if dash.slug in hidden_dashboards:
                dash_cls.can_access = _make_can_access(dash_cls.can_access)

            for panel in dash._registry.values():
                panel_cls = type(panel)
                is_fw = _panel_is_firewall(panel)
                panel_cls.allowed = _make_allowed(panel_cls.allowed, is_fw)

        # Build firewall URL prefixes and choose a landing page: restricted
        # users are bounced here instead of ever seeing a permission-denied
        # page for a non-firewall resource (also fixes the default landing
        # that would otherwise be the project overview).
        global _FW_PREFIXES, _LANDING
        prefixes = []
        for dash in Horizon._registry.values():
            for panel in dash._registry.values():
                if _panel_is_firewall(panel):
                    prefixes.append("/dashboard/%s/%s" % (dash.slug, panel.slug))
        _FW_PREFIXES = prefixes
        configured = settings.FIREWALL_ONLY_LANDING
        if configured:
            _LANDING = configured if configured.startswith("/") else "/" + configured
        elif prefixes:
            _LANDING = prefixes[0] + "/"

        _WRAPPED = True


class FirewallOnlyMiddleware(MiddlewareMixin):
    def process_request(self, request):
        _wrap()
        user = getattr(request, "user", None)
        if not _is_restricted(user):
            return None
        path = request.path
        # Never bounce asset / API / i18n / auth endpoints. Horizon is usually
        # mounted under /dashboard, so the real prefixes are /dashboard/static,
        # /dashboard/api, ... -- the old "/static/" check did NOT match
        # /dashboard/static/dashboard/js/*.js, which made this middleware 302
        # the Angular JS bundles to the (HTML) landing page and broke the whole
        # SPA: "Uncaught SyntaxError: Unexpected token '<'" ->
        # "Module 'horizon.app' is not available". Use STATIC_URL/MEDIA_URL
        # (always correct for this install's mount point) and cover both bare
        # and /dashboard/-prefixed forms for the rest.
        _static = getattr(settings, "STATIC_URL", "/static/") or "/"
        _media = getattr(settings, "MEDIA_URL", "/media/") or "/"
        _skip = (
            _static, _media,
            "/dashboard/auth/", "/auth/",
            "/dashboard/api/", "/api/",
            "/dashboard/i18n/", "/i18n/", "/dashboard/js/",
            "/jasmine/",
        )
        if (any(path.startswith(p) for p in _skip)
                or "/static/" in path or "/media/" in path):
            return None
        if request.method != "GET":
            return None
        if _is_firewall_path(path):
            return None
        return redirect(_LANDING)
