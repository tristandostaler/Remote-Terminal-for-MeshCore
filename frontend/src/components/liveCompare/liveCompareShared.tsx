/**
 * Vocabulary shared by the Live Compare page and the Statistics section.
 *
 * Three verdicts exist and each keeps one colour everywhere it appears: green
 * for "both" (the mesh and this node agree), blue for "node only" (we heard it,
 * the live observers did not), amber for "live only" (the mesh heard it and we
 * missed it — the number an operator placing an antenna cares about).
 */

import type { ReactNode } from 'react';
import { Check, Radio, RadioTower } from 'lucide-react';

import type { LiveCompareSource, LiveFeedStatus } from '../../types';

export interface SourceMeta {
  label: string;
  shortLabel: string;
  description: string;
  /** Text colour for numbers and badges. */
  text: string;
  /** Solid fill for legends and chart bars (a CSS colour, recharts takes raw values). */
  fill: string;
  /** Tinted badge background + border. */
  badge: string;
  /** Row accent, applied as a left border. */
  border: string;
  icon: typeof Check;
}

export const SOURCE_META: Record<LiveCompareSource, SourceMeta> = {
  both: {
    label: 'Seen by both',
    shortLabel: 'Both',
    description: 'Heard by this node and by the live feed observers.',
    text: 'text-success',
    fill: 'hsl(var(--success))',
    badge: 'bg-success/15 text-success border-success/30',
    border: 'border-success/60',
    icon: Check,
  },
  node: {
    label: 'Node only',
    shortLabel: 'Node',
    description: 'Heard by this node but by none of the live feed observers.',
    text: 'text-info',
    fill: 'hsl(var(--info))',
    badge: 'bg-info/15 text-info border-info/30',
    border: 'border-info/60',
    icon: Radio,
  },
  live: {
    label: 'Live only',
    shortLabel: 'Live',
    description: 'Heard by the live feed observers but missed by this node.',
    text: 'text-warning',
    fill: 'hsl(var(--warning))',
    badge: 'bg-warning/15 text-warning border-warning/30',
    border: 'border-warning/60',
    icon: RadioTower,
  },
};

export const SOURCE_ORDER: LiveCompareSource[] = ['both', 'node', 'live'];

export function SourceBadge({
  source,
  compact = false,
}: {
  source: LiveCompareSource;
  compact?: boolean;
}) {
  const meta = SOURCE_META[source];
  const Icon = meta.icon;
  return (
    <span
      title={meta.description}
      className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[0.625rem] font-medium uppercase tracking-wider ${meta.badge}`}
    >
      <Icon className="h-3 w-3" aria-hidden="true" />
      {compact ? meta.shortLabel : meta.label}
    </span>
  );
}

/** Where the remote instance shows the same channel, for a one-click cross-check. */
export function liveChannelUrl(baseUrl: string, channelName: string): string {
  return `${baseUrl.replace(/\/+$/, '')}/#/channels/${encodeURIComponent(channelName)}`;
}

export function liveHostLabel(baseUrl: string): string {
  try {
    return new URL(baseUrl).host || baseUrl;
  } catch {
    return baseUrl;
  }
}

export function formatRelativeAge(epochSeconds: number | null | undefined, now: number): string {
  if (!epochSeconds) return 'never';
  const delta = Math.max(0, now - epochSeconds);
  if (delta < 45) return 'just now';
  if (delta < 3600) return `${Math.round(delta / 60)} min ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)} h ago`;
  return `${Math.round(delta / 86400)} d ago`;
}

/**
 * One line describing the sync loop: what it is comparing against and when it
 * last managed to. The error, when there is one, replaces the happy path
 * rather than sitting next to it — a red sentence under a green "synced 2 min
 * ago" reads as a contradiction.
 */
export function describeSync(status: LiveFeedStatus, now: number): ReactNode {
  if (status.syncing) return 'Syncing…';
  if (status.last_error) {
    return (
      <span className="text-warning">
        Last sync failed {formatRelativeAge(status.last_sync_completed_at, now)}:{' '}
        {status.last_error}
      </span>
    );
  }
  if (!status.last_success_at) return status.enabled ? 'Waiting for the first sync' : 'Not syncing';
  return `Synced ${formatRelativeAge(status.last_success_at, now)}`;
}
