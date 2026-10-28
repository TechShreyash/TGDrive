// Exact substitutions for the reviewed 0.4.1 slim bundle; no decoder ABI changes.
export const SOFTWARE_FLUSH_BEFORE = `  async flush() {
    this.packetQueue = [], this.isConfigured && this.trackIndex >= 0 && this.bindings.flushDecoder(this.trackIndex);
  }`;
export const SOFTWARE_FLUSH_AFTER = `  _flushGeneration = 0;
  async flush(drain = false) {
    const generation = ++this._flushGeneration;
    if (!drain) {
      this.packetQueue = [];
      this.lastProcessedTimestamp = -1;
      if (this.isConfigured && this.trackIndex >= 0) this.bindings.flushDecoder(this.trackIndex);
      return;
    }
    const deadline = performance.now() + 30000;
    while (this.isProcessingQueue || this.packetQueue.length > 0) {
      if (generation !== this._flushGeneration || !this.isConfigured) return;
      if (performance.now() >= deadline) throw new Error("Software decoder drain timed out");
      if (!this.isProcessingQueue) this.processQueue();
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    if (generation !== this._flushGeneration || !this.isConfigured) return;
    const result = this.bindings.sendPacket(this.trackIndex, new Uint8Array(0), -1, -1, false);
    if (result < 0 && result !== -541478725) throw new Error("Software decoder EOS failed: " + result);
    while (generation === this._flushGeneration && this.isConfigured && this.bindings.receiveFrame(this.trackIndex) === 0) {
      if (performance.now() >= deadline) throw new Error("Software decoder drain timed out");
      const pts = this.bindings.getFramePts(this.trackIndex);
      this.processDecodedFrame(Math.max(0, pts) * 1000000);
    }
    if (generation === this._flushGeneration && this.isConfigured) {
      // EOF has drained; re-arm FFmpeg so a loop can accept the opening packet.
      this.bindings.flushDecoder(this.trackIndex);
      this.lastProcessedTimestamp = -1;
    }
  }`;

export const SOFTWARE_RESET_BEFORE = `  reset() {
    this.packetQueue = [], this.isConfigured && this.trackIndex >= 0 && this.bindings.flushDecoder(this.trackIndex), this.lastProcessedTimestamp = -1;
  }
  close() {
    this.isConfigured = false;
  }`;
export const SOFTWARE_RESET_AFTER = `  reset() {
    ++this._flushGeneration;
    this.packetQueue = [], this.isConfigured && this.trackIndex >= 0 && this.bindings.flushDecoder(this.trackIndex), this.lastProcessedTimestamp = -1;
  }
  close() {
    ++this._flushGeneration;
    this.packetQueue = [];
    this.isConfigured = false;
  }`;

export const SOFTWARE_QUEUE_BEFORE = `  async processQueue() {
    if (!this.isProcessingQueue) {
      this.isProcessingQueue = true;
      try {
        let A2 = performance.now();
        const I2 = new MessageChannel(), g2 = () => new Promise((A3) => {
          I2.port1.onmessage = () => A3(null), I2.port2.postMessage(null);
        });
        for (; this.packetQueue.length > 0; ) {
          performance.now() - A2 > 8 && (await g2(), A2 = performance.now());
          const I3 = this.packetQueue.shift();
          I3 && this.decodeInternal(I3);
        }
      } catch (A2) {
        g.error(uA, "Queue processing error", A2);
      } finally {
        this.isProcessingQueue = false;
      }
    }
  }`;
export const SOFTWARE_QUEUE_AFTER = `  async processQueue() {
    if (this.isProcessingQueue) return;
    this.isProcessingQueue = true;
    let channel;
    try {
      let lastYield = performance.now();
      channel = new MessageChannel();
      const yieldTask = () => new Promise((resolve) => {
        channel.port1.onmessage = resolve;
        channel.port2.postMessage(null);
      });
      while (this.packetQueue.length > 0) {
        if (performance.now() - lastYield > 8) {
          await yieldTask();
          lastYield = performance.now();
        }
        const packet = this.packetQueue.shift();
        if (packet) this.decodeInternal(packet);
      }
    } catch (error) {
      g.error(uA, "Queue processing error", error);
    } finally {
      channel?.port1.close();
      channel?.port2.close();
      this.isProcessingQueue = false;
    }
  }`;

export const FRAME_CAP_BEFORE = `    return Math.round((I2 ? 60 : n2) * g2);
  }
  hasPictureToCarryARewind(A2) {`;
export const FRAME_CAP_AFTER = `    const requested = Math.round((I2 ? 60 : n2) * g2);
    if (!I2 && (t2 || !s2)) return requested;
    const memory = typeof navigator !== "undefined" ? navigator.deviceMemory : undefined;
    const budget = (s2 || memory > 0 && memory <= 4 ? 64 : 128) * 1048576;
    const scale = I2 && C2?.width > 1920 ? 1920 / C2.width : 1;
    const bytesPerFrame = Math.max(1, B2 * scale * scale * 4);
    // Keep the resume cushion attainable even for high-frame-rate sources.
    const cushion = Math.ceil(Q2 * MoviPlayer.BOUND_RESUME_CUSHION_S) + 2;
    return Math.min(requested, Math.max(cushion, Math.floor(budget / bytesPerFrame)));
  }
  hasPictureToCarryARewind(A2) {`;

export const DECODING_PATCHES = [
    [
        "E3 = !this._audioOnly && (!this.isBackgrounded || this.isPiPActive), t3 = this._bindAV",
        "E3 = this.hasPicture && !this._audioOnly && (!this.isBackgrounded || this.isPiPActive), t3 = this._bindAV",
    ],
    [
        "D3 = !this.videoRenderer || this.isInAudioOnlyTail() || this._soundCarryingAlone || this.videoRenderer.getQueueSize() >= h3",
        "D3 = !this.hasPicture || this.isInAudioOnlyTail() || this._soundCarryingAlone || this.videoRenderer.getQueueSize() >= h3",
    ],
    [
        "s2 = this._bindAV && !this.disableAudio && !this._audioOnly && !this.audioRenderer.isDroppingAudio()",
        "s2 = this.hasPicture && this._bindAV && !this.disableAudio && !this._audioOnly && !this.audioRenderer.isDroppingAudio()",
    ],
    [
        `  sendPacket(A2, I2, g2, C2, B2) {
    if (!this.contextPtr) return -1;
    const Q2 = this.module._malloc(I2.byteLength);`,
        `  sendPacket(A2, I2, g2, C2, B2) {
    if (!this.contextPtr) return -1;
    if (I2.byteLength === 0) return this.module._movi_send_packet(this.contextPtr, A2, 0, 0, g2, C2, B2 ? 1 : 0);
    const Q2 = this.module._malloc(I2.byteLength);`,
    ],
    [SOFTWARE_FLUSH_BEFORE, SOFTWARE_FLUSH_AFTER],
    [SOFTWARE_RESET_BEFORE, SOFTWARE_RESET_AFTER],
    [
        `  async configure(A2, I2 = 0) {
    this.trackIndex = A2.id, this.targetFps = I2, this.lastProcessedTimestamp = -1;`,
        `  async configure(A2, I2 = 0) {
    ++this._flushGeneration;
    this.packetQueue = [];
    this.isConfigured = false;
    this.trackIndex = A2.id, this.targetFps = I2, this.lastProcessedTimestamp = -1;`,
    ],
    [SOFTWARE_QUEUE_BEFORE, SOFTWARE_QUEUE_AFTER],
    [
        `this.setWaitingForKeyframe(true, "flush() called"), this.swDecoder) return this.swDecoder.flush();`,
        `this.setWaitingForKeyframe(true, "flush() called"), this.swDecoder) return this.swDecoder.flush(drain);`,
    ],
    [FRAME_CAP_BEFORE, FRAME_CAP_AFTER],
    [
        "static PAUSE_BUFFER_AUDIO_SECONDS = 180;",
        "static PAUSE_BUFFER_AUDIO_SECONDS = 5;",
    ],
    [
        "static PAUSE_BUFFER_VIDEO_FRAMES = 5400;",
        "static PAUSE_BUFFER_VIDEO_FRAMES = 60;",
    ],
    [
        "if (!q2 && !W2 && this.videoDecoder.queueSize > l2 || !B4 && !this.disableAudio && this.audioDecoder.queueSize > S2)",
        "if (!q2 && !W2 && (this.videoDecoder.queueSize > l2 || (M2 || MoviPlayer._isMobileDevice && (this.trackManager.getActiveVideoTrack()?.width ?? 0) * (this.trackManager.getActiveVideoTrack()?.height ?? 0) < 8294400) && ((this.videoRenderer?.getQueueSize() ?? 0) + this.videoDecoder.queueSize) > N2) || !B4 && !this.disableAudio && this.audioDecoder.queueSize > S2)",
    ],
    [
        `            A5.port1.onmessage = I3, A5.port2.postMessage(null);`,
        `            A5.port1.onmessage = (event) => {
              A5.port1.close();
              A5.port2.close();
              I3(event);
            }, A5.port2.postMessage(null);`,
    ],
    [
        `    if (this.eofReached) return void this.stopPauseBuffering();
    const A2 = this.pendingPrebufferPackets.length;`,
        `    if (this.eofReached) return void this.stopPauseBuffering();
    let pausedBytes = this.pendingPrebufferPackets.reduce((bytes, packet) => bytes + packet.data.byteLength, 0);
    if (pausedBytes >= 16777216) return void this.stopPauseBuffering();
    const A2 = this.pendingPrebufferPackets.length;`,
    ],
    [
        `for (let I3 = 0; I3 < A3 && "paused" === this.stateManager.getState() && !(this.pendingPrebufferPackets.length >= MoviPlayer.PAUSE_BUFFER_MAX_PACKETS); I3++) {`,
        `for (let I3 = 0; I3 < A3 && pausedBytes < 16777216 && "paused" === this.stateManager.getState() && !(this.pendingPrebufferPackets.length >= MoviPlayer.PAUSE_BUFFER_MAX_PACKETS); I3++) {`,
    ],
    [
        `        this.trackManager.isActiveStream(A4.streamIndex) && this.pendingPrebufferPackets.push(A4);`,
        `        if (this.trackManager.isActiveStream(A4.streamIndex)) {
          // Retain the crossing packet; stopping reads must never lose demuxed data.
          this.pendingPrebufferPackets.push(A4);
          pausedBytes += A4.data.byteLength;
        }`,
    ],
    [
        `return { decoderType: this.useSoftware ? "Software (FFmpeg)" : "Hardware (WebCodecs)", queueSize: this.queueSize, errorCount: this.errorCount };`,
        `return { decoderType: this.useSoftware ? "Software (FFmpeg)" : this.hardwareUnavailable ? "Software (WebCodecs)" : "WebCodecs (acceleration unverified)", queueSize: this.queueSize, errorCount: this.errorCount };`,
    ],
    [
        `return { decoderType: this.useSoftware ? "Software (FFmpeg)" : "Hardware (WebCodecs)", queueSize: this.queueSize };`,
        `return { decoderType: this.useSoftware ? "Software (FFmpeg)" : "WebCodecs", queueSize: this.queueSize };`,
    ],
];
