"""Cancellation-safe media resource ownership for the pinned Telethon 1.45.0."""

import asyncio
from collections.abc import Awaitable
from copy import copy
from typing import cast

from telethon import TelegramClient, functions
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER

from tgdrive.transfers.tasks import _journalled


class MediaTelegramClient(TelegramClient):
    def _iter_download(self, file, **kwargs):
        iterator = super()._iter_download(file, **kwargs)
        # Native close() uses None for "no sender". The lazy first anext()
        # can be cancelled before _init assigns this field for the first time.
        iterator._sender = None
        return iterator

    async def _create_exported_sender(self, dc_id):
        # Telethon registers the sender in its pool only after authorization.
        # Until then this method owns it, including any running transport tasks.
        dc = await self._get_dc(dc_id)
        sender = MTProtoSender(None, loggers=self._log)
        try:
            await sender.connect(
                self._connection(
                    dc.ip_address,
                    dc.port,
                    dc.id,
                    loggers=self._log,
                    proxy=self._proxy,
                    local_addr=self._local_addr,
                )
            )
            auth = await self(functions.auth.ExportAuthorizationRequest(dc_id))
            init = copy(self._init_request)
            init.query = functions.auth.ImportAuthorizationRequest(
                id=auth.id, bytes=auth.bytes
            )
            request = functions.InvokeWithLayerRequest(LAYER, init)
            await cast(Awaitable[object], sender.send(request))
        except BaseException:
            await _journalled(sender.disconnect)()
            raise
        return sender

    async def _return_exported_sender(self, sender):
        try:
            await super()._return_exported_sender(sender)
        except asyncio.CancelledError:
            # Native return only suspends while acquiring the pool lock. Once
            # acquired, it decrements synchronously. close() clears its handle
            # even on cancellation, so finish this pending return exactly once.
            await _journalled(super()._return_exported_sender)(sender)
            raise
