"""Re-copyable share capabilities derived from the persistent owner secret.

Only hashes enter the catalog. Domain separation keeps this capability distinct
from login sessions. Re-copying a link requires the original persistent secret.
"""

import base64
import hashlib
import hmac
import time

from argon2 import PasswordHasher

SHARE_UNLOCK_TTL = 8 * 60 * 60
share_password_hasher = PasswordHasher(time_cost=2, memory_cost=32768, parallelism=2)


def token_factory(settings):
    key = settings.session_secret.get_secret_value().encode()

    def derive(identity):
        digest = hmac.new(
            key, b"tgdrive/share-link/v1\0" + identity.encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    return derive


def unlock_cookie_name(token):
    digest = hashlib.sha256(token.encode()).hexdigest()[:32]
    return "tgdrive_share_" + digest


def _unlock_signature(settings, token, password_hash, expired_at):
    key = settings.session_secret.get_secret_value().encode()
    data = "\0".join(("tgdrive/share-unlock/v1", token, password_hash, expired_at))
    return hmac.digest(key, data.encode(), "sha256").hex()


def create_unlock(settings, token, password_hash):
    expiry = str(int(time.time()) + SHARE_UNLOCK_TTL)
    return expiry + "." + _unlock_signature(settings, token, password_hash, expiry)


def share_unlock(request, settings, token):
    if not token or len(token) > 1024:
        return None
    raw = request.cookies.get(unlock_cookie_name(token), "")
    expiry, _, signature = raw.partition(".")
    if (
        len(signature) != 64
        or not signature.isascii()
        or not (1 <= len(expiry) <= 12 and expiry.isascii() and expiry.isdecimal())
    ):
        return None

    def verify(password_hash):
        return int(expiry) > time.time() and hmac.compare_digest(
            signature, _unlock_signature(settings, token, password_hash, expiry)
        )

    return verify
