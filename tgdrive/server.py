"""Single-worker container launcher with narrowly scoped proxy discovery."""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from pathlib import Path

import uvicorn

logger = logging.getLogger(__name__)
# The listener is reachable within the container network; Compose controls ingress.
_CONTAINER_BIND = "0.0.0.0"  # noqa: S104  # nosec B104


def trusted_proxy_ips() -> str:
    explicit = os.environ.get("FORWARDED_ALLOW_IPS", "").strip()
    if explicit and explicit != "auto":
        return explicit
    trusted = {"127.0.0.1", "::1"}
    for host in os.environ.get("TGDRIVE_PROXY_HOSTS", "").split(","):
        host = host.strip()
        if not host:
            continue
        try:
            addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            logger.warning(
                "Configured proxy hostname could not be resolved; only loopback "
                "and other resolved proxies are trusted. Restart after the proxy "
                "joins the application network."
            )
            continue
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_private and not ip.is_unspecified and not ip.is_multicast:
                trusted.add(str(ip))
    return ",".join(sorted(trusted))


def main() -> None:
    wait_for_initialization()
    uvicorn.run(
        "main:app",
        host=_CONTAINER_BIND,
        port=8000,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=trusted_proxy_ips(),
        timeout_graceful_shutdown=45,
        access_log=False,
    )


def wait_for_initialization() -> None:
    """Keep a new container accessible for its explicit owner init command."""
    if os.environ.get("TGDRIVE_AUTO_SECRETS") != "true":
        return
    from tgdrive.mongo import configure

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        return
    backend = configure(
        Path(os.environ.get("DATA_DIR", "data")).expanduser().resolve(),
        uri,
        os.environ.get("MONGODB_DATABASE", "tgdrive"),
    )
    if backend.db.catalog.find_one({"_id": "catalog"}):
        return
    logger.warning(
        "Drive is not initialized. Run 'python -m tgdrive init' once in this "
        "container's terminal for a NEW drive. For an existing drive, check "
        "MONGODB_URI/MONGODB_DATABASE instead. Existing state is preserved."
    )
    while not backend.db.catalog.find_one({"_id": "catalog"}):
        time.sleep(5)
    # The init command creates the catalog before saving its settings and
    # releasing ownership. Do not race its final transaction/startup cleanup.
    while backend.db.runtime.find_one(
        {"_id": "owner", "$expr": {"$gt": ["$expires_at", "$$NOW"]}}
    ):
        time.sleep(1)


if __name__ == "__main__":
    main()
