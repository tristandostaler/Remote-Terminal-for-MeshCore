// Parser for the `aei1:` text framing that carries AEIC neural-codec images.
//
// Mirror of `app/imaging/aeic/text_transport.py`; see that module for the format
// rationale. Fixed-width header, no delimiters — the basE91 alphabet contains
// ':' so a delimited header would be ambiguous, and at 156 bytes per message
// every character counts.
//
//     aei1<sid:2><idx:1><tot:1>[<meta:2> only when idx === 0]<basE91 payload>
//
// A 512x512 colour photo is 117–209 bytes of bitstream, so one or two messages.

export const AEIC_PREFIX = 'aei1';

const SESSION_ID_CHARS = 2;
const META_CHARS = 2;
const HEADER_CHARS = AEIC_PREFIX.length + SESSION_ID_CHARS + 2;

/** Source aspect ratios addressable by the 4-bit aspect code, as [w, h]. */
export const AEIC_ASPECT_CODES: readonly (readonly [number, number])[] = [
  [1, 1], // 0  square
  [5, 4], // 1  landscape
  [4, 3], // 2
  [3, 2], // 3
  [16, 10], // 4
  [16, 9], // 5
  [2, 1], // 6
  [21, 9], // 7
  [4, 5], // 8  portrait
  [3, 4], // 9
  [2, 3], // 10
  [10, 16], // 11
  [9, 16], // 12
  [1, 2], // 13
  [9, 21], // 14
  [1, 1], // 15 unknown -> render square
];

export const AEIC_ASPECT_UNKNOWN = 15;

/** Square sizes addressable by the 2-bit resolution code. Index === wire code. */
const RESOLUTION_CODES = [512, 256, 768, 1024];

export interface AeicChunk {
  sessionId: number;
  index: number;
  total: number;
  /** Present on chunk 0 only. */
  squareSize: number | null;
  aspectCode: number | null;
  rateCode: number | null;
  /** Length of this chunk's basE91 slice, for the airtime hint. */
  payloadChars: number;
}

const HEADER_RE = /^aei1([0-9a-z]{2})([0-9a-z])([0-9a-z])(.*)$/s;

/** Parse one `aei1:` message body, or null when the text is not one. */
export function parseAeicChunk(text: string): AeicChunk | null {
  const match = HEADER_RE.exec(text);
  if (!match) return null;
  const [, sid, idx, tot, rest] = match;
  const sessionId = Number.parseInt(sid, 36);
  const index = Number.parseInt(idx, 36);
  const total = Number.parseInt(tot, 36);
  if (!Number.isSafeInteger(sessionId) || total < 1 || index >= total) return null;

  let squareSize: number | null = null;
  let aspectCode: number | null = null;
  let rateCode: number | null = null;
  let payload = rest;
  if (index === 0) {
    if (rest.length <= META_CHARS) return null;
    const metaByte = Number.parseInt(rest.slice(0, META_CHARS), 36);
    if (!Number.isSafeInteger(metaByte) || metaByte < 0 || metaByte > 255) return null;
    const resolution = (metaByte >> 2) & 0x03;
    // An unknown rate code means the sender is running a format we cannot
    // decode; refusing beats guessing and rendering garbage.
    rateCode = metaByte & 0x03;
    if (rateCode > 1) return null;
    squareSize = RESOLUTION_CODES[resolution];
    aspectCode = (metaByte >> 4) & 0x0f;
    payload = rest.slice(META_CHARS);
  }
  if (!payload) return null;
  return {
    sessionId,
    index,
    total,
    squareSize,
    aspectCode,
    rateCode,
    payloadChars: payload.length,
  };
}

/** True when this text is any chunk of an AEIC image. */
export function isAeicChunk(text: string): boolean {
  return parseAeicChunk(text) !== null;
}

/**
 * CSS aspect ratio to render a decoded image at.
 *
 * The codec always produces a square with the frame stretched to fit, so the
 * receiver has to undo that using the shape the sender named. Code 0 and code 15
 * ("unknown") both render square, unstretched.
 */
export function aeicAspectRatio(aspectCode: number | null): number {
  if (aspectCode === null || aspectCode === AEIC_ASPECT_UNKNOWN) return 1;
  const entry = AEIC_ASPECT_CODES[aspectCode & 0x0f];
  return entry[0] / entry[1];
}

/** Bytes of bitstream a chunk count implies, for the size hint. */
export function aeicApproxBitstreamBytes(payloadChars: number): number {
  // basE91 expands by ~23%; this is only ever shown as an approximation.
  return Math.round(payloadChars / 1.2308);
}

/** Header cost, exported so the compose counter can agree with the chunker. */
export function aeicHeaderChars(isFirst: boolean): number {
  return isFirst ? HEADER_CHARS + META_CHARS : HEADER_CHARS;
}

/**
 * Marker for an image that arrived over the binary GRP_DATA transport.
 *
 * That transport carries no text at all — the picture is raw chunk blobs — so
 * unlike an `aei1:` message there is no body to keep. The backend writes this
 * marker as the message text purely to give the image a place in the
 * conversation. It is a LOCAL convention between server and UI and never goes
 * on air; see `app/imaging/aeic/channel_data_ingest.py`.
 */
const BINARY_MARKER_PREFIX = 'aeib:';

/** The session key of a binary-transport image, or null. */
export function parseAeicBinaryRef(text: string): string | null {
  if (!text.startsWith(BINARY_MARKER_PREFIX)) return null;
  const key = text.slice(BINARY_MARKER_PREFIX.length).trim();
  return key.length > 0 ? key : null;
}

const UNSUPPORTED_MARKER_PREFIX = 'mediax:';

/**
 * A marker row for media this server kept but cannot decode.
 *
 * Like `aeib:`, it never goes on air -- the server mints it so the arrival has a
 * place in the conversation. It carries only the id; the wording lives in the UI,
 * where it can change without a migration.
 */
export function parseUnsupportedMediaRef(text: string): number | null {
  if (!text.startsWith(UNSUPPORTED_MARKER_PREFIX)) return null;
  const id = Number(text.slice(UNSUPPORTED_MARKER_PREFIX.length).trim());
  return Number.isInteger(id) && id > 0 ? id : null;
}
