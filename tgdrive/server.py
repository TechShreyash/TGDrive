"""Single-worker container launcher with narrowly scoped proxy discovery."""

from __future__ import annotations

import ipaddress
import logging
import os
import socket

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
    uvicorn.run(
        "tgdrive.web.app:create_app",
        factory=True,
        host=_CONTAINER_BIND,
        port=8000,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=trusted_proxy_ips(),
        timeout_graceful_shutdown=45,
        access_log=False,
    )


if __name__ == "__main__":
    main()
