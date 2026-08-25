import type { ImageSessionStatus, MediaTransport, VoiceSessionStatus } from '../api';

/**
 * Waiting for a media transfer that arrives in pieces, without a fixed deadline.
 *
 * Both media fetches used to poll a fixed number of times -- 40 for a picture, 20
 * for a voice note -- which fixed the ceiling at 30 and 15 seconds. That was
 * already tight for raw packets and is hopeless for the `rmt1:` text transport,
 * where one image fragment is two messages about a second apart: a 20-fragment
 * picture needs minutes, so the poll gave up while fragments were still arriving
 * and reported a working transfer as unavailable.
 *
 * A deadline is the wrong shape for this. What matters is not how long a transfer
 * takes but whether it is still moving, so this waits on *progress* instead:
 * fragments arriving reset the clock, and only silence ends it. A slow transfer
 * finishes, a dead one is given up on quickly, and neither needs its duration
 * predicted in advance.
 */

export const MEDIA_POLL_INTERVAL_MS = 750;

/**
 * How long to wait with no new fragment before calling a transfer stalled.
 *
 * Per transport, because the quiet gap between two fragments differs by an order
 * of magnitude. Raw is one packet per fragment. Text is two messages roughly a
 * second apart plus a gap before the next fragment, so several seconds of silence
 * is normal there and would be a stall in raw.
 */
export const MEDIA_STALL_TIMEOUT_MS: Record<MediaTransport, number> = {
  raw: 8_000,
  text: 25_000,
};

export interface MediaTransferSnapshot {
  sessionId: string;
  received: number;
  total: number;
  complete: boolean;
  transport: MediaTransport;
}

export interface AwaitMediaTransferOptions {
  /** Ask for the transfer. Returns the session as it stands right after asking. */
  start: () => Promise<MediaTransferSnapshot>;
  /** Re-read the session. */
  poll: (sessionId: string) => Promise<MediaTransferSnapshot>;
  onProgress?: (snapshot: MediaTransferSnapshot) => void;
  /** Injectable for tests, so this is exercised without leaning on fake timers. */
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number) =>
  new Promise<void>((resolve) => {
    window.setTimeout(resolve, ms);
  });

/**
 * Poll a media session until it completes or goes quiet.
 *
 * Resolves with the last snapshot either way -- a stall is a normal outcome here,
 * not an error, because a transfer that stopped halfway is exactly the case the
 * caller has to show and offer to retry. Only the underlying requests reject.
 */
export async function awaitMediaTransfer({
  start,
  poll,
  onProgress,
  now = () => Date.now(),
  sleep = defaultSleep,
}: AwaitMediaTransferOptions): Promise<MediaTransferSnapshot> {
  let snapshot = await start();
  onProgress?.(snapshot);
  let lastProgressAt = now();
  let bestReceived = snapshot.received;

  while (!snapshot.complete) {
    if (now() - lastProgressAt >= MEDIA_STALL_TIMEOUT_MS[snapshot.transport]) return snapshot;
    await sleep(MEDIA_POLL_INTERVAL_MS);
    snapshot = await poll(snapshot.sessionId);
    onProgress?.(snapshot);
    if (snapshot.received > bestReceived) {
      // Only forward movement resets the clock. Re-reading the same count is what
      // a stall looks like, so treating any poll as activity would never time out.
      bestReceived = snapshot.received;
      lastProgressAt = now();
    }
  }
  return snapshot;
}

/**
 * The two session shapes reduced to what waiting needs.
 *
 * They count the same thing under different names -- `fragment_count` for a
 * picture, `packet_count` for a recording -- so the waiting logic takes neither
 * and these adapt at the edge.
 */
export function imageSnapshot(session: ImageSessionStatus): MediaTransferSnapshot {
  return {
    sessionId: session.session_id,
    received: session.received_count,
    total: session.fragment_count,
    complete: session.state === 'complete',
    transport: session.transport,
  };
}

export function voiceSnapshot(session: VoiceSessionStatus): MediaTransferSnapshot {
  return {
    sessionId: session.session_id,
    received: session.received_count,
    total: session.packet_count,
    complete: session.state === 'complete',
    transport: session.transport,
  };
}
