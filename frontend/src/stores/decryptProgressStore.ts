import { useSyncExternalStore } from 'react';

import type { DecryptSweepProgress, DecryptSweepStatus } from '../types';

/**
 * Historical decrypt sweeps report progress over the WebSocket while they scan.
 *
 * The stream lives outside React, like raw packets do: a sweep ticks several
 * times a second and only the two panels that show it (settings, and the info
 * pane of whatever is being swept) should re-render for that. Putting it in App
 * state would re-render the message list on every tick.
 *
 * The server already throttles the ticks, so nothing is debounced here.
 */

const IDLE: DecryptSweepStatus = { active: null, last: null, queued: 0 };

let state: DecryptSweepStatus = IDLE;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function isTerminal(progress: DecryptSweepProgress): boolean {
  return progress.status === 'complete' || progress.status === 'failed';
}

/**
 * Fold one progress event into the store.
 *
 * A finished sweep moves from `active` to `last` so the panel can keep showing
 * what it recovered instead of blanking the moment it ends.
 */
export function recordDecryptProgress(progress: DecryptSweepProgress): void {
  if (isTerminal(progress)) {
    // A late tick from an already-finished sweep must not resurrect it as active.
    const active = state.active?.job_id === progress.job_id ? null : state.active;
    state = { active, last: progress, queued: progress.queued };
  } else {
    state = { ...state, active: progress, queued: progress.queued };
  }
  emit();
}

/** Seed from GET /packets/decrypt/status, so a reload lands mid-sweep correctly. */
export function primeDecryptStatus(status: DecryptSweepStatus): void {
  state = status;
  emit();
}

export function clearDecryptProgress(): void {
  state = IDLE;
  emit();
}

function getSnapshot(): DecryptSweepStatus {
  return state;
}

/** Subscribe a component to sweep progress. */
export function useDecryptStatus(): DecryptSweepStatus {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

function sweepTouchesKey(progress: DecryptSweepProgress | null, key: string): boolean {
  return !!progress && progress.targets.some((target) => target.key.toLowerCase() === key);
}

/**
 * The sweep to show in one conversation's info pane.
 *
 * A channel sweep lists no targets until it finds something, so a running one
 * is shown in every room's pane — it genuinely is working on their behalf, and
 * "nothing yet" is the honest answer for all of them. A DM sweep names its one
 * contact from the start, so it only ever shows in that contact's pane, and
 * never in a room's. Once a sweep has finished, only the conversations it
 * actually recovered messages for keep showing it.
 */
export function selectSweepForKey(
  status: DecryptSweepStatus,
  key: string | null,
  kind: 'channel' | 'contact' = 'channel'
): DecryptSweepProgress | undefined {
  if (!key) {
    return undefined;
  }
  const needle = key.toLowerCase();

  if (status.active) {
    const sweepsThisKind = status.active.kind === (kind === 'contact' ? 'contact' : 'channels');
    if (!sweepsThisKind) {
      return undefined;
    }
    // A DM sweep is for exactly one contact; a room sweep may be for any of them.
    if (status.active.kind === 'contact' && !sweepTouchesKey(status.active, needle)) {
      return undefined;
    }
    return status.active;
  }

  return sweepTouchesKey(status.last, needle) ? (status.last ?? undefined) : undefined;
}
