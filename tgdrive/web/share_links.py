"""Re-copyable share capabilities derived from the persistent owner secret.

Only hashes enter the catalog. Domain separation keeps this capability distinct
from login sessions. Re-copying a link requires the original persistent secret.
"""

import base64
import hashlib
import hmac


def token_factory(settings):
    key = settings.session_secret.get_secret_value().encode()

    def derive(identity):
        digest = hmac.new(
            key, b"tgdrive/share-link/v1\0" + identity.encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    return derive
