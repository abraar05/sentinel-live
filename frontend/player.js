/*
 * Live player tile.
 *
 * The gateway ships a raw Annex-B elementary stream, which browsers cannot
 * hand straight to WebCodecs: EncodedVideoChunk boundaries must fall on
 * access units. The bitstream work lives in ./stream.js so it can be tested
 * outside a browser; this file is the DOM and decoder plumbing.
 *
 * Anything unsupported degrades to polling the snapshot endpoint, which works
 * against every device regardless of codec.
 */

import { AccessUnitAssembler, splitNals, nalType, hasIdr } from './stream.js';

export class Player {
  constructor(deviceId, name) {
    this.deviceId = deviceId;
    this.name = name;
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'video-canvas';
    this.ctx = this.canvas.getContext('2d', { alpha: false });

    this.mode = 'idle';
    this.state = 'idle';
    this.detail = '';
    this.codec = null;
    this.decoder = null;
    this.assembler = new AccessUnitAssembler('h264');
    this.awaitingKeyframe = false;
    this.expectGop = 0;
    this.gotGop = 0;
    this.socket = null;
    this.snapshotTimer = null;
    this.lastFrameAt = 0;
    this.fpsWindow = [];
    this.decoderErrors = 0;
    this.onstatus = () => {};
    this.closed = false;
  }

  // ------------------------------------------------------------- lifecycle

  connect() {
    this.close();
    this.closed = false;
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const token = new URLSearchParams(location.search).get('token');
    const url = `${scheme}://${location.host}/ws/stream/${this.deviceId}` +
      (token ? `?token=${encodeURIComponent(token)}` : '');

    this.setStatus('connecting');
    let socket;
    try {
      socket = new WebSocket(url);
    } catch {
      this.fallback('socket error');
      return;
    }
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try { this.onControl(JSON.parse(ev.data)); } catch { /* ignore junk */ }
      } else {
        this.onBytes(new Uint8Array(ev.data));
      }
    };
    socket.onclose = () => {
      if (this.closed) return;
      this.setStatus('offline');
      setTimeout(() => {
        if (!this.closed && this.mode !== 'snapshot') this.fallback('stream offline');
      }, 1500);
    };
    socket.onerror = () => socket.close();
  }

  close() {
    this.closed = true;
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
      this.socket = null;
    }
    if (this.snapshotTimer) {
      clearInterval(this.snapshotTimer);
      this.snapshotTimer = null;
    }
    this.teardownDecoder();
    this.assembler.reset();
  }

  // --------------------------------------------------------------- control

  onControl(msg) {
    if (msg.type === 'key') {
      this.setCodec(msg.codec, msg.codecString, msg.width, msg.height);
      if (msg.gopBytes) {
        // A keyframe-anchored GOP replay follows as binary frames.
        this.expectGop = msg.gopBytes;
        this.gotGop = 0;
      }
    } else if (msg.type === 'status') {
      // A viewer that attached before the parameter sets arrived gets its
      // codec string here instead of via a key message.
      if (this.mode !== 'live' && msg.codecString) {
        this.setCodec(msg.codec, msg.codecString, msg.width, msg.height);
      }
      this.onstatus(msg);
    }
  }

  setCodec(codec, codecString, width, height) {
    if (this.mode === 'live' && this.codec === codecString) return;
    if (!codecString) {
      this.fallback('codec unknown');
      return;
    }
    if (typeof VideoDecoder === 'undefined') {
      this.fallback('WebCodecs unavailable');
      return;
    }
    this.teardownDecoder();
    try {
      this.decoder = new VideoDecoder({
        output: (frame) => this.render(frame),
        error: (err) => this.onDecoderError(err),
      });
      // Annex-B: no description, the decoder infers SPS/PPS from the stream.
      this.decoder.configure({
        codec: codecString,
        optimizeForLatency: true,
      });
    } catch {
      this.decoder = null;
      this.fallback(`${codecString} unsupported`);
      return;
    }
    this.codec = codecString;
    this.assembler = new AccessUnitAssembler(codecString.startsWith('hvc') ? 'h265' : 'h264');
    this.awaitingKeyframe = true;      // until the first IDR shows up
    this.mode = 'live';
    if (width && height && !this.canvas.width) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.setStatus('live');
  }

  teardownDecoder() {
    if (this.decoder && this.decoder.state !== 'closed') {
      try { this.decoder.close(); } catch { /* already closed */ }
    }
    this.decoder = null;
  }

  onDecoderError(err) {
    this.decoderErrors += 1;
    this.teardownDecoder();
    this.fallback(`decoder: ${err && err.message ? err.message : err}`);
  }

  // -------------------------------------------------------------- streaming

  onBytes(bytes) {
    if (this.mode !== 'live') return;
    for (const unit of this.assembler.push(bytes)) this.submit(unit);
  }

  submit(unit) {
    if (!this.decoder || this.decoder.state !== 'configured') return;

    // A viewer may attach mid-GOP. Delta frames before the first IDR cannot
    // be decoded, so hold them rather than wedging the decoder.
    if (this.awaitingKeyframe) {
      if (!unit.key) return;
      this.awaitingKeyframe = false;
    }

    // Never let decode back-pressure stall the socket.
    if (this.decoder.decodeQueueSize > 12) {
      try { this.decoder.flush(); } catch { /* noop */ }
      return;
    }
    try {
      this.decoder.decode(new EncodedVideoChunk({
        type: unit.key ? 'key' : 'delta',
        timestamp: performance.now() * 1000,
        data: unit.data,
      }));
    } catch (err) {
      this.onDecoderError(err);
    }
  }

  render(frame) {
    if (this.closed) return;
    const w = frame.displayWidth || frame.codedWidth;
    const h = frame.displayHeight || frame.codedHeight;
    if (this.canvas.width !== w) this.canvas.width = w;
    if (this.canvas.height !== h) this.canvas.height = h;
    this.ctx.drawImage(frame, 0, 0, w, h);
    frame.close();

    this.lastFrameAt = Date.now();
    this.fpsWindow.push(this.lastFrameAt);
    if (this.fpsWindow.length > 60) this.fpsWindow.shift();
    this.onstatus(this.status());
  }

  // -------------------------------------------------------------- fallback

  fallback(reason) {
    if (this.mode === 'snapshot') return;
    this.mode = 'snapshot';
    this.teardownDecoder();
    this.setStatus('snapshot', reason);
    this.startSnapshotPolling();
  }

  startSnapshotPolling() {
    if (this.snapshotTimer) return;
    const grab = async () => {
      if (this.closed) return;
      try {
        const res = await fetch(`/api/devices/${this.deviceId}/snapshot.jpg`, {
          cache: 'no-store',
        });
        if (!res.ok) throw new Error(res.status);
        const blob = await res.blob();
        if (this.closed) return;
        const bitmap = await createImageBitmap(blob);
        if (this.canvas.width !== bitmap.width) this.canvas.width = bitmap.width;
        if (this.canvas.height !== bitmap.height) this.canvas.height = bitmap.height;
        this.ctx.drawImage(bitmap, 0, 0);
        bitmap.close();
        this.lastFrameAt = Date.now();
        this.fpsWindow.push(this.lastFrameAt);
        if (this.fpsWindow.length > 60) this.fpsWindow.shift();
        this.setStatus('snapshot-live');
        this.onstatus(this.status());
      } catch {
        this.setStatus('snapshot-offline');
        this.onstatus(this.status());
      }
    };
    grab();
    this.snapshotTimer = setInterval(grab, 2000);
  }

  status() {
    const fps = this.fpsWindow.length > 1
      ? ((this.fpsWindow.length - 1) * 1000) /
        Math.max(1, this.fpsWindow[this.fpsWindow.length - 1] - this.fpsWindow[0])
      : 0;
    return {
      mode: this.mode,
      codec: this.codec || '',
      fps: Math.min(99, Math.round(fps)),
      staleMs: this.lastFrameAt ? Date.now() - this.lastFrameAt : null,
      width: this.canvas.width,
      height: this.canvas.height,
    };
  }

  setStatus(state, detail = '') {
    this.state = state;
    if (detail) this.detail = detail;
  }
}

export { splitNals, nalType, hasIdr };