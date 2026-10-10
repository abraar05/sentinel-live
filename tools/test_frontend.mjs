/*
 * Frontend bitstream tests, run in Node against real encoder output.
 *
 *   node tools/test_frontend.mjs /tmp/opencode/testclip.h264
 *
 * The browser's WebCodecs path is only correct if the Annex-B re-framing
 * produces exactly one access unit per picture, so that is what we check:
 * the same numbers the Python side reports for the same file.
 */

import { readFileSync } from 'node:fs';
import { AccessUnitAssembler, splitNals, nalType, hasIdr, firstMbInSlice }
  from '../frontend/stream.js';

const path = process.argv[2] || '/tmp/opencode/testclip.h264';
const data = new Uint8Array(readFileSync(path));

let failures = 0;
function check(label, actual, expected) {
  const ok = actual === expected;
  if (!ok) failures += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}: ${actual}${ok ? '' : ` (expected ${expected})`}`);
}

function histogramOf(list, type) {
  return list.filter((n) => nalType(n, 'h264') === type).length;
}

/** Split a complete buffer, including the final unterminated NAL. */
function splitAll(buf) {
  const out = [];
  let i = 0;
  while (i + 2 < buf.length) {
    if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 1) { i += 3; continue; }
    if (buf[i] === 0 && buf[i + 1] === 0 && buf[i + 2] === 0 && buf[i + 3] === 1) { i += 4; continue; }
    i += 1;
  }
  const starts = [];
  let j = 0;
  while (j + 2 < buf.length) {
    if (buf[j] === 0 && buf[j + 1] === 0) {
      if (buf[j + 2] === 1) { starts.push({ pos: j, len: 3 }); j += 3; continue; }
      if (buf[j + 2] === 0 && buf[j + 3] === 1) { starts.push({ pos: j, len: 4 }); j += 4; continue; }
    }
    j += 1;
  }
  for (let k = 0; k < starts.length; k++) {
    const start = starts[k].pos + starts[k].len;
    const end = k + 1 < starts.length ? starts[k + 1].pos : buf.length;
    let stop = end;
    while (stop > start && buf[stop - 1] === 0) stop -= 1;
    if (stop > start) out.push(buf.subarray(start, stop));
  }
  return out;
}

// --- NAL splitting -------------------------------------------------------

// splitNals() is a streaming parser: it withholds the final unterminated
// NAL, so the raw file yields one fewer unit than it contains.
const { nals } = splitNals(data);
check('complete NAL units', nals.length, 2420);
check('SPS count', histogramOf(nals, 7), 10);
check('PPS count', histogramOf(nals, 8), 10);
check('IDR slice count', histogramOf(nals, 5), 80);

// --- access units --------------------------------------------------------

const whole = new AccessUnitAssembler('h264');
let units = [];
for (const unit of whole.push(data)) units.push(unit);
units = units.concat(whole.end());

check('access units', units.length, 300);
check('keyframes', units.filter((u) => u.key).length, 10);
check('first unit is a keyframe', units[0].key, true);

// Byte-for-byte round trip through the assembler.
//
// Reconstruct the NAL sequence from the emitted access units. Validation
// uses splitAll(), not splitNals(), because a streaming splitter correctly
// withholds the last NAL of each independently-parsed buffer.
const rebuiltPayload = [];
for (const au of units) {
  for (const nal of splitAll(au.data)) rebuiltPayload.push(...nal);
}
const originalPayload = [];
for (const nal of nals) originalPayload.push(...nal);
check('access units carry every NAL byte', rebuiltPayload.length, originalPayload.length);
check('round-trip lossless',
  rebuiltPayload.every((b, i) => b === originalPayload[i]), true);

// --- reassembly across arbitrary chunk boundaries -------------------------

for (const size of [7, 61, 512, 4096, 65536]) {
  const asm = new AccessUnitAssembler('h264');
  const got = [];
  for (let i = 0; i < data.length; i += size) {
    got.push(...asm.push(data.subarray(i, i + size)));
  }
  got.push(...asm.end());
  check(`chunked @${size}B -> access units`, got.length, 300);
}

// A viewer joining mid-stream gets delta frames until the next IDR.
const asm2 = new AccessUnitAssembler('h264');
const units2 = asm2.push(data).concat(asm2.end());
let leadingDeltas = 0;
while (leadingDeltas < units2.length && !units2[leadingDeltas].key) leadingDeltas += 1;
check('leading delta frames before first IDR', leadingDeltas, 0);

const midJoin = new AccessUnitAssembler('h264');
const tail = data.subarray(Math.floor(data.length * 0.4));
const units3 = midJoin.push(tail).concat(midJoin.end());
check('mid-stream join produced units', units3.length > 0, true);

// --- slice header parsing -----------------------------------------------

const sliceNals = nals.filter((n) => nalType(n, 'h264') === 1);
const firsts = sliceNals.filter((n) => firstMbInSlice(n, 'h264') === 0);
check('frames starting at mb 0', firsts.length, 290);

console.log(failures === 0 ? '\nall frontend tests passed' : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);