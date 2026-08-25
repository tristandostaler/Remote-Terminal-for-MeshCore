import { describe, expect, it, vi } from 'vitest';
import {
  MEDIA_POLL_INTERVAL_MS,
  MEDIA_STALL_TIMEOUT_MS,
  awaitMediaTransfer,
  imageSnapshot,
  voiceSnapshot,
  type MediaTransferSnapshot,
} from '../utils/mediaTransfer';
import type { ImageSessionStatus, MediaTransport, VoiceSessionStatus } from '../api';

/**
 * A clock the polling drives itself.
 *
 * `sleep` advances it instead of waiting, so these exercise the real loop -- every
 * poll, every comparison -- with no timers and no wall-clock time. Fake timers
 * would work too, but they test the scheduling rather than the decision, and the
 * decision is the whole of this module.
 */
function clock() {
  let time = 0;
  return {
    now: () => time,
    sleep: async (ms: number) => {
      time += ms;
    },
    advance: (ms: number) => {
      time += ms;
    },
  };
}

function snapshot(
  received: number,
  total: number,
  transport: MediaTransport = 'raw'
): MediaTransferSnapshot {
  return { sessionId: 's1', received, total, complete: received === total, transport };
}

describe('awaitMediaTransfer', () => {
  it('returns as soon as the transfer is already complete', async () => {
    const poll = vi.fn();

    const result = await awaitMediaTransfer({
      start: async () => snapshot(4, 4),
      poll,
      ...clock(),
    });

    expect(result.complete).toBe(true);
    expect(poll).not.toHaveBeenCalled();
  });

  it('polls until the last fragment lands', async () => {
    const received = [1, 2, 3, 4];
    let index = 0;

    const result = await awaitMediaTransfer({
      start: async () => snapshot(0, 4),
      poll: async () => snapshot(received[index++], 4),
      ...clock(),
    });

    expect(result).toMatchObject({ received: 4, complete: true });
    expect(index).toBe(4);
  });

  it('reports every step to onProgress, including the first', async () => {
    const seen: number[] = [];
    const received = [2, 4];
    let index = 0;

    await awaitMediaTransfer({
      start: async () => snapshot(1, 4),
      poll: async () => snapshot(received[index++], 4),
      onProgress: (snap) => seen.push(snap.received),
      ...clock(),
    });

    expect(seen).toEqual([1, 2, 4]);
  });

  it('gives up on a transfer that stops moving, and hands back what arrived', async () => {
    const time = clock();
    let polls = 0;

    const result = await awaitMediaTransfer({
      start: async () => snapshot(3, 10),
      poll: async () => {
        polls += 1;
        return snapshot(3, 10);
      },
      ...time,
    });

    // A stall is a value, not a throw: the caller has 3 of 10 fragments to keep and
    // a retry to offer, neither of which it can do from a rejected promise.
    expect(result).toMatchObject({ received: 3, complete: false });
    expect(polls).toBe(Math.ceil(MEDIA_STALL_TIMEOUT_MS.raw / MEDIA_POLL_INTERVAL_MS));
  });

  it('lets a slow transfer finish as long as it keeps moving', async () => {
    /*
     * The regression this replaced. The old loop polled a fixed 40 times at 750 ms,
     * so any picture needing more than 30 seconds was reported unavailable while its
     * fragments were still arriving -- which over the text transport is every
     * picture. Progress here is slower than the stall window is wide, and still
     * completes.
     */
    const total = 30;
    let received = 0;

    const result = await awaitMediaTransfer({
      start: async () => snapshot(0, total, 'text'),
      poll: async () => snapshot((received += 1), total, 'text'),
      ...clock(),
    });

    expect(result.complete).toBe(true);
  });

  it('waits longer between text fragments than between raw ones', async () => {
    /*
     * One text fragment is two messages about a second apart plus a gap before the
     * next: quiet stretches that would be a dead transfer over raw are normal here.
     * One stall window for both would either cut text transfers short or leave a
     * dead raw one spinning.
     */
    expect(MEDIA_STALL_TIMEOUT_MS.text).toBeGreaterThan(MEDIA_STALL_TIMEOUT_MS.raw);

    const pollsBeforeGivingUp = async (transport: MediaTransport) => {
      let polls = 0;
      await awaitMediaTransfer({
        start: async () => snapshot(1, 10, transport),
        poll: async () => {
          polls += 1;
          return snapshot(1, 10, transport);
        },
        ...clock(),
      });
      return polls;
    };

    expect(await pollsBeforeGivingUp('text')).toBeGreaterThan(await pollsBeforeGivingUp('raw'));
  });

  it('treats a fragment arriving as a reason to keep waiting', async () => {
    const time = clock();
    let polls = 0;

    const result = await awaitMediaTransfer({
      start: async () => snapshot(0, 3),
      poll: async () => {
        polls += 1;
        // One fragment right before the window would have closed, twice over. Each
        // resets the clock, so neither near-stall ends the transfer.
        time.advance(MEDIA_STALL_TIMEOUT_MS.raw - MEDIA_POLL_INTERVAL_MS - 1);
        return snapshot(Math.min(polls, 3), 3);
      },
      ...time,
    });

    expect(result).toMatchObject({ received: 3, complete: true });
  });

  it('does not treat re-reading the same count as activity', async () => {
    /*
     * The subtle way a stall detector fails open. Polling itself is not progress, so
     * only a rising count may reset the clock -- otherwise a session that answers
     * forever with the same numbers is waited on forever.
     */
    let polls = 0;

    const result = await awaitMediaTransfer({
      start: async () => snapshot(5, 9),
      poll: async () => {
        polls += 1;
        return snapshot(5, 9);
      },
      ...clock(),
    });

    expect(result.complete).toBe(false);
    expect(polls).toBeLessThan(1000);
  });

  it('propagates a failed request rather than reporting a stall', async () => {
    await expect(
      awaitMediaTransfer({
        start: async () => {
          throw new Error('sender is not a known contact');
        },
        poll: async () => snapshot(0, 4),
        ...clock(),
      })
    ).rejects.toThrow('sender is not a known contact');
  });
});

describe('session snapshots', () => {
  it('reads an image session', () => {
    const session: ImageSessionStatus = {
      session_id: '0000000a',
      state: 'receiving',
      format: 0,
      width: 256,
      height: 171,
      size_bytes: 2100,
      fragment_count: 14,
      received_count: 4,
      missing_indices: [4, 5],
      transport: 'text',
    };

    expect(imageSnapshot(session)).toEqual({
      sessionId: '0000000a',
      received: 4,
      total: 14,
      complete: false,
      transport: 'text',
    });
  });

  it('reads a voice session, whose count goes by another name', () => {
    const session: VoiceSessionStatus = {
      session_id: '0000000b',
      state: 'complete',
      duration_ms: 4000,
      packet_count: 6,
      received_count: 6,
      missing_indices: [],
      transport: 'raw',
    };

    expect(voiceSnapshot(session)).toEqual({
      sessionId: '0000000b',
      received: 6,
      total: 6,
      complete: true,
      transport: 'raw',
    });
  });
});
