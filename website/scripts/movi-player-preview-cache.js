// Exact substitutions for Movi 0.4.1; preview bytes never move playback's window.
export const PREVIEW_CACHE_METHODS = `  _tgPreviewBytes = new LRUCache(8);
  _tgPreviewResponses = new WeakSet();
  cachePreviewBytes(offset, bytes, request) {
    if (this.closed || !request?.preview || request.source !== this || request.preview.borrowSource !== this ||
        request.preview.lifetimeAbort?.signal?.aborted !== false || request.url !== this.url ||
        request.preview.url !== this.url || !(bytes instanceof Uint8Array) ||
        !Number.isSafeInteger(offset) || offset < 0 || bytes.byteLength === 0 ||
        bytes.byteLength > 8388608 || !Number.isSafeInteger(offset + bytes.byteLength) ||
        this.size <= 0 || offset + bytes.byteLength > this.size) return;
    const headers = JSON.stringify([...new Headers(this.headers)]);
    if (request.headers !== headers || request.headers !== JSON.stringify([...new Headers(request.preview.headers)])) return;
    if (this.previewByteCoverage(offset, bytes.byteLength).end === offset + bytes.byteLength) return;
    // Own exactly these bytes; a view could otherwise retain a much larger buffer.
    const copy = bytes.slice();
    this._tgPreviewBytes.set(this.url, offset, copy.byteLength, copy.buffer);
    while (this._tgPreviewBytes.cache.size > 128) this._tgPreviewBytes.evictLRU();
  }
  previewByteCoverage(offset, length) {
    if (this.closed || !Number.isSafeInteger(offset) || !Number.isSafeInteger(length) ||
        offset < 0 || length <= 0 || !Number.isSafeInteger(offset + length))
      return {end: offset, entries: []};
    const entries = this._tgPreviewBytes.findOverlapping(this.url, offset, length)
      .sort((left, right) => left.offset - right.offset);
    let end = offset;
    for (const entry of entries) {
      if (entry.offset > end) break;
      end = Math.min(offset + length, Math.max(end, entry.offset + entry.length));
    }
    return {end, entries};
  }
  peekPreviewBytes(offset, length) {
    if (this.size > 0) length = Math.min(length, this.size - offset);
    if (length <= 0 || length > 8388608) return null;
    const {end, entries} = this.previewByteCoverage(offset, length);
    if (end < offset + length) return null;
    const bytes = new Uint8Array(length);
    let cursor = offset;
    for (const entry of entries) {
      const to = Math.min(end, entry.offset + entry.length);
      if (to > cursor) {
        bytes.set(new Uint8Array(entry.data, cursor - entry.offset, to - cursor), cursor - offset);
        cursor = to;
      }
      if (cursor === end) break;
    }
    return bytes;
  }
  previewRangeEnd(offset, requestedEnd, openEnded) {
    if (openEnded) return requestedEnd;
    const {end, entries} = this.previewByteCoverage(offset, requestedEnd - offset + 1);
    if (end > offset) return end - 1;
    return entries.length ? entries[0].offset - 1 : requestedEnd;
  }
  async fetchRangeOrPreview(offset, end, signal, openEnded) {
    if (signal.aborted) throw signal.reason || new DOMException("Source closed", "AbortError");
    const bytes = !openEnded && this.peekPreviewBytes(offset, end - offset + 1);
    if (!bytes) return fetch(this.url, {
      headers: await this.buildRequestHeaders({offset, length: end - offset + 1, openEnded}),
      cache: "no-store", signal
    });
    const response = new Response(new ReadableStream({start(controller) {
      controller.enqueue(bytes); controller.close();
    }}), {status: 206});
    this._tgPreviewResponses.add(response);
    return response;
  }
`;

export const PREVIEW_SHARE_METHOD = `  previewByteRangeEnd(response, offset, requestedEnd, request) {
    const range = /^bytes (\\d+)-(\\d+)\\/(\\d+)$/i.exec(response.headers.get("Content-Range") || "");
    const encoding = response.headers.get("Content-Encoding");
    if (!request?.source || response.status !== 206 || !range || encoding && encoding.toLowerCase() !== "identity" ||
        request.source !== this.borrowSource || request.url !== this.url ||
        !request.source || request.source.url !== this.url || request.source.closed) return -1;
    const start = Number(range[1]), end = Number(range[2]), size = Number(range[3]);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || !Number.isSafeInteger(size) ||
        start !== offset || end < start || end > requestedEnd || end >= size ||
        size !== this.size || size !== request.source.getKnownSize()) return -1;
    return end + 1;
  }
  spendPreviewRequestBytes(length) {
    const budget = this._tgPreviewBudget;
    if (!budget) return true;
    if (!Number.isSafeInteger(length) || length <= 0 || budget.remaining < length) return false;
    budget.remaining -= length;
    return true;
  }
`;

export const PREVIEW_CACHE_PATCHES = [
    [
        "    this.isPreviewGenerating = true;\n    let Q2 = () => {",
        "    this.isPreviewGenerating = true;\n    this._tgPreviewBudget = {remaining: 16777216};\n    let Q2 = () => {",
    ],
    [
        "    const s2 = await this.thumbnailSource.getSize();",
        "    if (this.thumbnailSource instanceof ThumbnailHttpSource) this.thumbnailSource._tgPreviewBudget = this._tgPreviewBudget;\n    const s2 = await this.thumbnailSource.getSize();",
    ],
    [
        "      const g2 = await this.thumbnailSource.read(A3, I3);",
        "      if (this.thumbnailSource instanceof ThumbnailHttpSource) this.thumbnailSource._tgPreviewBudget = this._tgPreviewBudget;\n      const g2 = await this.thumbnailSource.read(A3, I3);",
    ],
    [
        '      A2 = await fetch(this.url, { method: "GET", headers: { ...this.headers, Range: "bytes=0-0" }, signal: this.lifetimeAbort.signal });',
        '      if (!this.spendPreviewRequestBytes(1)) return null;\n      A2 = await fetch(this.url, { method: "GET", headers: { ...this.headers, Range: "bytes=0-0" }, signal: this.lifetimeAbort.signal });',
    ],
    [
        "      const E2 = hA(this.lifetimeAbort.signal), t2 = setTimeout(() => E2.abort(), 1e4);",
        "      if (!this.spendPreviewRequestBytes(Q2 - C2 + 1)) return new ArrayBuffer(0);\n      const E2 = hA(this.lifetimeAbort.signal), t2 = setTimeout(() => E2.abort(), 1e4);",
    ],
    [
        "for (0 === this.streamStartTime && (this.streamStartTime = k2, this.lastSpeedTime = k2);",
        "for (!this._tgPreviewResponses.has(c2) && 0 === this.streamStartTime && (this.streamStartTime = k2, this.lastSpeedTime = k2);",
    ],
    [
        "          if (C4 >= 0.5) {\n            const I3 = this.totalBytesDownloaded - this.lastSpeedBytes;",
        "          if (!this._tgPreviewResponses.has(c2) && C4 >= 0.5) {\n            const I3 = this.totalBytesDownloaded - this.lastSpeedBytes;",
    ],
    [
        "          if (F2 - d2 > 1048576) {",
        "          if (!this._tgPreviewResponses.has(c2) && F2 - d2 > 1048576) {",
    ],
    [
        "  peekHead(A2, I2) {\n",
        PREVIEW_CACHE_METHODS + "  peekHead(A2, I2) {\n",
    ],
    [
        '    const C2 = this.peekMetadata(A2, I2);\n    if (C2) return this.position = A2 + I2, g.debug(QA, "Read: served from metadata LRU"), C2.buffer;',
        '    const C2 = this.peekMetadata(A2, I2);\n    if (C2) return this.position = A2 + I2, g.debug(QA, "Read: served from metadata LRU"), C2.buffer;\n    const preview = this.peekPreviewBytes(A2, I2);\n    if (preview) return this.position = A2 + preview.byteLength, preview.buffer;',
    ],
    [
        "return this.size > 0 ? Math.min(A3 + C3 - 1, this.size - 1) : A3 + C3 - 1;",
        "return this.previewRangeEnd(A3, this.size > 0 ? Math.min(A3 + C3 - 1, this.size - 1) : A3 + C3 - 1, s2);",
    ],
    [
        'c2 = await fetch(this.url, { headers: await this.buildRequestHeaders({ offset: R2, length: y2 - R2 + 1, openEnded: s2 }), cache: "no-store", signal: this.streamSignal() });',
        "c2 = await this.fetchRangeOrPreview(R2, y2, this.streamSignal(), s2);",
    ],
    [
        'C5 = (async () => fetch(this.url, { headers: await this.buildRequestHeaders({ offset: A5, length: I3 - A5 + 1, openEnded: false }), cache: "no-store", signal: g2 }))();',
        "C5 = this.fetchRangeOrPreview(A5, I3, g2, false);",
    ],
    [
        "          this.totalBytesDownloaded += Q3.length, this.recordDelivery(Q3.length);",
        "          if (!this._tgPreviewResponses.has(c2)) {\n            this.totalBytesDownloaded += Q3.length; this.recordDelivery(Q3.length);\n          }",
    ],
    [
        "    this.closed = true, this.stopStream(), this.lifetimeAbort.abort(), this.wakeBufferWaiters(true), this._netSuspended = false;",
        "    this._tgPreviewBytes.clear();\n    this.closed = true, this.stopStream(), this.lifetimeAbort.abort(), this.wakeBufferWaiters(true), this._netSuspended = false;",
    ],
    [
        "  isInBuffer(A2, I2) {\n    return null !== this.buffer && A2 >= this.bufferStart && A2 + I2 <= this.bufferEnd;\n  }",
        PREVIEW_SHARE_METHOD +
            "  isInBuffer(A2, I2) {\n    return null !== this.buffer && A2 >= this.bufferStart && A2 + I2 <= this.bufferEnd;\n  }",
    ],
    [
        "      if (Q3) return this.position = A2 + I2, g.debug(rA, `Read borrowed from main window: offset=${A2}, length=${I2}`), Q3.buffer;",
        "      if (Q3) return this.position = A2 + I2, g.debug(rA, `Read borrowed from main window: offset=${A2}, length=${I2}`), Q3.buffer;\n      const preview = this.borrowSource.peekPreviewBytes?.(A2, I2);\n      if (preview) return this.position = A2 + preview.byteLength, preview.buffer;",
    ],
    [
        '        const B4 = await fetch(this.url, { headers: { ...this.headers, Range: `bytes=${C2}-${Q2}` }, cache: "no-store", signal: E2.signal });',
        '        const sharedRequest = {source: this.borrowSource, preview: this, url: this.url, headers: JSON.stringify([...new Headers(this.headers)])};\n        const B4 = await fetch(this.url, { headers: { ...this.headers, Range: `bytes=${C2}-${Q2}` }, cache: "no-store", signal: E2.signal });',
    ],
    [
        "        const s2 = B4.body, n2 = Q2 - C2 + 1;",
        "        const sharedEnd = this.previewByteRangeEnd(B4, C2, Q2, sharedRequest);\n        const s2 = B4.body, n2 = Q2 - C2 + 1;",
    ],
    [
        "          const Q3 = Math.min(I2, g2.byteLength), E3 = new Uint8Array(Q3);",
        "          if (sharedEnd > C2) sharedRequest.source.cachePreviewBytes(C2, this.buffer.subarray(0, sharedEnd - C2), sharedRequest);\n          const Q3 = Math.min(I2, g2.byteLength), E3 = new Uint8Array(Q3);",
    ],
    [
        "this.buffer.set(I3.subarray(0, g2), R2), R2 += g2, this.bufferEnd = C2 + R2,",
        "this.buffer.set(I3.subarray(0, g2), R2), sharedEnd > C2 + R2 && sharedRequest.source.cachePreviewBytes(C2 + R2, I3.subarray(0, Math.min(g2, sharedEnd - C2 - R2)), sharedRequest), R2 += g2, this.bufferEnd = C2 + R2,",
    ],
];
