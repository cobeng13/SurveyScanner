from hashlib import sha256
import hmac
from ipaddress import ip_address
import logging
from secrets import token_bytes
import time

import jwt
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

LOG = logging.getLogger("survey.security")


class Access:
    def __init__(self, settings):
        self.settings = settings
        self.secret = token_bytes(32)
        self.issuer = f"https://{settings.cf_team}"
        if settings.auth == "cloudflare":
            self.keys = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", timeout=4)

    def authenticate(self, token):
        if not token or len(token) > 8192:
            return None
        try:
            signing_key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, signing_key.key, algorithms=["RS256"], audience=self.settings.cf_aud,
                                issuer=self.issuer, options={"require": ["exp", "iat", "aud", "iss", "sub"]})
            subject = claims["sub"]
            if subject and isinstance(claims.get("email"), str) and claims["email"]:
                return subject
        except jwt.PyJWKClientConnectionError:
            LOG.warning("access_public_keys_unavailable")
        except (jwt.PyJWTError, ValueError, TypeError):
            pass
        return None

    def issue(self, owner, timestamp=None):
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        signature = hmac.new(self.secret, f"{owner}:{timestamp}".encode(), sha256).hexdigest()
        return f"{timestamp}.{signature}"

    def check(self, owner, token):
        timestamp, _, _ = str(token).partition(".")
        return (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12
                and 0 <= int(time.time()) - int(timestamp) <= 8 * 3600
                and hmac.compare_digest(self.issue(owner, timestamp), str(token)))


class SecurityMiddleware:
    def __init__(self, app, access, settings):
        self.app = app
        self.access = access
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path = scope["path"]
        root = scope.get("root_path")
        if root and path.startswith(root + "/"):
            path = path[len(root):]
        public_asset = path == "/health" or path.startswith("/static/")
        owner = None
        if not public_asset:
            if self.settings.auth == "local":
                try:
                    peer = scope.get("client", ("", 0))[0]
                    if ip_address(peer).is_loopback:
                        owner = "local-development"
                except (ValueError, TypeError):
                    pass
            else:
                owner = await run_in_threadpool(self.access.authenticate, headers.get("cf-access-jwt-assertion"))
            if not owner:
                await JSONResponse({"detail": "Institutional sign-in is required."}, status_code=401,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
                return
        scope.setdefault("state", {})["owner"] = owner

        max_body = self.settings.max_bytes + 128 * 1024
        if scope["method"] == "POST":
            length = headers.get("content-length")
            if length is not None:
                if not length.isascii() or not length.isdigit() or len(length) > 12:
                    await JSONResponse({"detail": "Invalid request size."}, status_code=400)(scope, receive, send)
                    return
                if int(length) > max_body:
                    await JSONResponse({"detail": "The upload exceeds the configured limit."},
                                       status_code=413)(scope, receive, send)
                    return

        bytes_read = 0

        async def limited_receive():
            nonlocal bytes_read
            message = await receive()
            if message["type"] == "http.request":
                bytes_read += len(message.get("body", b""))
                if bytes_read > max_body:
                    raise HTTPException(413, "The upload exceeds the configured limit.")
            return message

        async def secured_send(message):
            if message["type"] == "http.response.start":
                output = MutableHeaders(scope=message)
                output["Cache-Control"] = "no-store"
                output["X-Content-Type-Options"] = "nosniff"
                output["Referrer-Policy"] = "same-origin"
                output["Content-Security-Policy"] = ("default-src 'self'; frame-ancestors 'none'; "
                                                     "object-src 'none'; base-uri 'none'; form-action 'self'")
            await send(message)

        await self.app(scope, limited_receive, secured_send)
