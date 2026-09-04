import { describe, expect, it } from 'vitest';

import {
  AEIC_ASPECT_CODES,
  AEIC_ASPECT_UNKNOWN,
  aeicApproxBitstreamBytes,
  aeicAspectRatio,
  aeicHeaderChars,
  isAeicChunk,
  isLocalMarkerText,
  parseAeicChunk,
} from '../utils/aeicEnvelope';

/**
 * This parser has to agree exactly with `app/imaging/aeic/text_transport.py`.
 * The fixtures below are hand-built against the documented layout:
 *
 *     aei1 | sid(2) | idx(1) | tot(1) | meta(2) on chunk 0 | basE91 payload
 */
const PAYLOAD = 'ABCDEFGHIJ';
const chunk0 = (meta = '00', sid = '0z', total = '1') => `aei1${sid}0${total}${meta}${PAYLOAD}`;
const laterChunk = (index = '1', total = '2', sid = '0z') => `aei1${sid}${index}${total}${PAYLOAD}`;

describe('aei1 chunk parsing', () => {
  it('parses chunk 0 including its metadata byte', () => {
    // The metadata byte is written in BASE36, not hex: '11' is 1*36+1 = 37,
    // which is aspect 2 (4:3) | resolution code 1 (256px) | rate 1.
    expect(parseAeicChunk(chunk0('11'))).toEqual({
      sessionId: 35, // '0z' base36
      index: 0,
      total: 1,
      squareSize: 256,
      aspectCode: 2,
      rateCode: 1,
      payloadChars: PAYLOAD.length,
    });
  });

  it('parses a later chunk, which carries no metadata', () => {
    expect(parseAeicChunk(laterChunk())).toEqual({
      sessionId: 35,
      index: 1,
      total: 2,
      squareSize: null,
      aspectCode: null,
      rateCode: null,
      payloadChars: PAYLOAD.length,
    });
  });

  it('defaults the shipping metadata byte to a 512px square at rate 0', () => {
    const parsed = parseAeicChunk(chunk0('00'));
    expect(parsed?.squareSize).toBe(512);
    expect(parsed?.aspectCode).toBe(0);
    expect(parsed?.rateCode).toBe(0);
  });

  it.each([
    ['ordinary text', 'hello world'],
    ['the prefix alone', 'aei1'],
    ['a truncated header', 'aei10'],
    ['no payload after the header', 'aei10z01'],
    ['metadata but no payload', 'aei10z0100'],
    ['an MCMP body', 'mcmp2:abcdef'],
    ['an IE4 envelope', 'IE4:a:0:e:74:4r:1mc'],
  ])('returns null for %s', (_label, text) => {
    expect(parseAeicChunk(text)).toBeNull();
  });

  it('rejects an index at or past the total', () => {
    expect(parseAeicChunk(laterChunk('2', '2'))).toBeNull();
    expect(parseAeicChunk(laterChunk('5', '2'))).toBeNull();
    expect(parseAeicChunk(chunk0('00', '0z', '0'))).toBeNull();
  });

  it('rejects a rate code this build cannot decode', () => {
    // Guessing a rate would hand the wrong model a bitstream it would happily
    // turn into a plausible, wrong picture.
    expect(parseAeicChunk(chunk0('02'))).toBeNull();
    expect(parseAeicChunk(chunk0('03'))).toBeNull();
    expect(parseAeicChunk(chunk0('01'))).not.toBeNull();
  });

  it('recognises any chunk of an image', () => {
    expect(isAeicChunk(chunk0())).toBe(true);
    expect(isAeicChunk(laterChunk())).toBe(true);
    expect(isAeicChunk('hello')).toBe(false);
  });
});

describe('aei1 header costs', () => {
  it('matches the Python chunker so the two agree on capacity', () => {
    expect(aeicHeaderChars(true)).toBe(10);
    expect(aeicHeaderChars(false)).toBe(8);
  });
});

describe('aspect handling', () => {
  it('renders square for both "square" and "unknown"', () => {
    expect(aeicAspectRatio(0)).toBe(1);
    expect(aeicAspectRatio(AEIC_ASPECT_UNKNOWN)).toBe(1);
    expect(aeicAspectRatio(null)).toBe(1);
  });

  it('undoes the stretch for a landscape source', () => {
    expect(aeicAspectRatio(2)).toBeCloseTo(4 / 3);
    expect(aeicAspectRatio(5)).toBeCloseTo(16 / 9);
  });

  it('undoes the stretch for a portrait source', () => {
    expect(aeicAspectRatio(9)).toBeCloseTo(3 / 4);
    expect(aeicAspectRatio(12)).toBeCloseTo(9 / 16);
  });

  it('mirrors landscape and portrait entries', () => {
    // The table has to be symmetric or a rotated photo letterboxes wrongly.
    for (const [landscape, portrait] of [
      [1, 8],
      [2, 9],
      [3, 10],
      [4, 11],
      [5, 12],
      [6, 13],
      [7, 14],
    ]) {
      expect(AEIC_ASPECT_CODES[landscape]).toEqual([...AEIC_ASPECT_CODES[portrait]].reverse());
    }
  });

  it('has 16 entries so the 4-bit code is total', () => {
    expect(AEIC_ASPECT_CODES).toHaveLength(16);
  });
});

describe('size hints', () => {
  it('undoes the ~23% basE91 expansion', () => {
    // 144 chars is what a 117-byte bitstream encodes to.
    expect(aeicApproxBitstreamBytes(144)).toBe(117);
  });
});

describe('local marker rows', () => {
  it('recognises a binary image marker', () => {
    expect(isLocalMarkerText('aeib:grp:1c1e08f41fd4dd96')).toBe(true);
    expect(isLocalMarkerText('aeib:out:m42')).toBe(true);
  });

  it('recognises a kept-but-undecodable media marker', () => {
    expect(isLocalMarkerText('mediax:17')).toBe(true);
  });

  it('leaves anything someone actually wrote alone', () => {
    expect(isLocalMarkerText('aeib is a nice word')).toBe(false);
    expect(isLocalMarkerText('hello')).toBe(false);
    expect(isLocalMarkerText('')).toBe(false);
    expect(isLocalMarkerText(null)).toBe(false);
  });
});
