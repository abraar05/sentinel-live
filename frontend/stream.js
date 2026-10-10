/*
 * Annex-B parsing for the browser player.
 *
 * Kept separate from player.js (which needs a DOM) so the bitstream logic
 * can be unit-tested in Node against real encoder output. See
 * tools/test_frontend.mjs.
 */

const START_LONG = [0, 0, 0, 1];
const START_SHORT = [0, 0, 1];

export const AUD = 9;
export const NAL_SPS = 7;
export const NAL_IDR = 5;
export const H265_VPS = 32;
export const H265_SPS = 33;

const VCL_H264 = new Set([1, 2, 4, 5]);
const NON_VCL_H264 = new Set([6, 7, 8, 9, 12]);
const H265_VCL = new Set([0, 1, 2, 9, 19, 20, 21, 22, 23]);
const H265_NON_VCL = new Set([32, 33, 34, 35, 39, 40]);

/** Split a growing byte stream into complete NAL units plus an unparsed tail. */
export function splitNals(buf) {
  const n = buf.length;
  const starts = [];
  let i = 0;
  while (i + 2 < n) {
    if (buf[i] === 0 && buf[i + 1] === 0) {
      if (buf[i + 2] === 1) {
        starts.push({ pos: i, len: 3 });
        i += 3;
        continue;
      }
      if (buf[i + 2] === 0 && buf[i + 3] === 1) {
        starts.push({ pos: i, len: 4 });
        i += 4;
        continue;
      }
    }
    i++;
  }

  const nals = [];
  // The last start code has no terminator, so its NAL unit is still
  // incomplete: it must stay in `rest`, not be emitted as well.
  for (let k = 0; k < starts.length - 1; k++) {
    const start = starts[k].pos + starts[k].len;
    const boundary = starts[k + 1].pos;
    let stop = boundary;
    while (stop > start && buf[stop - 1] === 0) stop--;   // trailing_zero_8bits
    if (stop > start) nals.push(buf.subarray(start, stop));
  }

  const rest = starts.length ? buf.subarray(starts[starts.length - 1].pos) : buf;
  return { nals, rest };
}

export function nalType(nal, codec) {
  return codec === 'h265' ? (nal[0] >> 1) & 0x3f : nal[0] & 0x1f;
}

export function firstMbInSlice(nal, codec) {
  if (codec === 'h265') return (nal[1] >> 7) & 1;

  let pos = 0;
  const readBit = () => {
    const byte = nal[1 + (pos >> 3)];
    if (byte === undefined) return 0;
    const bit = (byte >> (7 - (pos & 7))) & 1;
    pos += 1;
    return bit;
  };
  let leading = 0;
  while (readBit() === 0 && leading < 32) leading += 1;
  if (leading === 0) return 0;
  let value = 0;
  for (let k = 0; k < leading; k++) value = (value << 1) | readBit();
  return (1 << leading) - 1 + value;
}

export function hasIdr(nals, codec) {
  for (const nal of nals) {
    const t = nalType(nal, codec);
    if (t === NAL_IDR || t === 19 || t === 20 || t === 21) return true;
    if (codec === 'h265' && nal.length > 1 && ((nal[1] >> 1) & 0x3f) === 19) return true;
  }
  return false;
}

export function concatNals(nals) {
  let total = 0;
  for (const nal of nals) total += 4 + nal.length;
  const out = new Uint8Array(total);
  let offset = 0;
  for (const nal of nals) {
    out.set(START_LONG, offset);
    offset += 4;
    out.set(nal, offset);
    offset += nal.length;
  }
  return out;
}

/**
 * Groups an Annex-B byte stream into access units.
 *
 * Boundaries come from slice headers (first_mb_in_slice === 0). Splitting on
 * IDR alone is wrong - encoders emit ~10 slices per frame by default, so
 * that yields ten "access units" per picture and WebCodecs rejects them.
 */
export class AccessUnitAssembler {
  constructor(codec = 'h264') {
    this.codec = codec;
    this.buffer = new Uint8Array(0);
    this.au = [];
    this.auKey = false;
    this.hasVcl = false;
  }

  reset() {
    this.buffer = new Uint8Array(0);
    this.au = [];
    this.auKey = false;
    this.hasVcl = false;
  }

  /** Feed bytes; returns any access units that became complete. */
  push(bytes) {
    const merged = new Uint8Array(this.buffer.length + bytes.length);
    merged.set(this.buffer, 0);
    merged.set(bytes, this.buffer.length);

    const { nals, rest } = splitNals(merged);
    this.buffer = rest.slice();
    const out = [];

    for (const nal of nals) {
      const t = nalType(nal, this.codec);
      const isH265 = this.codec === 'h265';
      const nonVcl = isH265 ? H265_NON_VCL.has(t) : NON_VCL_H264.has(t);
      const vcl = isH265 ? H265_VCL.has(t) : VCL_H264.has(t);

      if (t === AUD) {
        this._flush(out);
        this.au.push(nal);
        continue;
      }
      if (nonVcl) {
        // Parameter sets / SEI belong to the picture that follows them. Only
        // close an open picture - consecutive header NALs must accumulate into
        // one pending unit, or SPS and PPS each become their own "frame".
        if (this.hasVcl) this._flush(out);
        this.au.push(nal);
        if (t === NAL_SPS || t === H265_SPS) this.auKey = true;
        continue;
      }
      if (vcl && this.hasVcl) {
        const prevVcl = isH265
          ? H265_VCL.has(nalType(this.au[this.au.length - 1], 'h265'))
          : VCL_H264.has(nalType(this.au[this.au.length - 1], 'h264'));
        if (prevVcl && firstMbInSlice(nal, this.codec) === 0) this._flush(out);
        if (t === NAL_IDR || t === 19 || t === 20 || t === 21) this.auKey = true;
      }
      this.au.push(nal);
      if (vcl) this.hasVcl = true;
    }
    return out;
  }

  /** Emit whatever is buffered, e.g. on a stream boundary or teardown. */
  end() {
    const out = [];
    this._flush(out);
    this.buffer = new Uint8Array(0);
    return out;
  }

  _flush(out) {
    if (!this.au.length) return;
    const payload = concatNals(this.au);
    const key = this.auKey || hasIdr(this.au, this.codec);
    this.au = [];
    this.auKey = false;
    this.hasVcl = false;
    if (payload.length) out.push({ data: payload, key });
  }
}

export { START_LONG, START_SHORT };