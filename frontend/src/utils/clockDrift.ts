/**
 * Presentation helpers for clock drift measured from advert timestamps.
 *
 * Sign convention matches the backend (`app/clock_drift.py`): drift is
 * `advert_timestamp - our_receive_time`, so positive means the node's clock runs
 * *ahead* of this server's. Because propagation delay only ever pushes a reading
 * negative, a node with a perfect clock shows up as a small negative rather than
 * exactly zero — which is why the in-sync band is ±1 minute and not ±0.
 */

import type { DriftSeverity } from '../types';

/** Matches the backend bands so both surfaces call the same drift "fine". */
export const DRIFT_IN_SYNC_SECONDS = 60;

const SEVERITY_LABELS: Record<DriftSeverity, string> = {
  in_sync: 'In sync',
  minor: 'Slightly off',
  major: 'Badly off',
  severe: 'Way off',
};

/** Tailwind text colors, ordered so worse is louder. */
const SEVERITY_TEXT: Record<DriftSeverity, string> = {
  in_sync: 'text-success',
  minor: 'text-muted-foreground',
  major: 'text-warning',
  severe: 'text-destructive',
};

/** Chart/pill fills. Hex rather than tokens because Recharts takes raw colors. */
const SEVERITY_COLORS: Record<DriftSeverity, string> = {
  in_sync: '#10b981',
  minor: '#64748b',
  major: '#f59e0b',
  severe: '#f43f5e',
};

export function driftSeverityLabel(severity: DriftSeverity): string {
  return SEVERITY_LABELS[severity] ?? severity;
}

export function driftSeverityTextClass(severity: DriftSeverity): string {
  return SEVERITY_TEXT[severity] ?? 'text-muted-foreground';
}

export function driftSeverityColor(severity: DriftSeverity): string {
  return SEVERITY_COLORS[severity] ?? SEVERITY_COLORS.minor;
}

/**
 * A duration in the largest two units that still say something, e.g. `2m 15s`,
 * `3h 04m`, `12d 6h`. Unsigned — callers add the direction.
 */
export function formatDriftMagnitude(seconds: number): string {
  const total = Math.round(Math.abs(seconds));
  if (total < 60) {
    return `${total}s`;
  }
  if (total < 3600) {
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return rest === 0 ? `${minutes}m` : `${minutes}m ${String(rest).padStart(2, '0')}s`;
  }
  if (total < 86400) {
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return minutes === 0 ? `${hours}h` : `${hours}h ${String(minutes).padStart(2, '0')}m`;
  }
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  if (days >= 365) {
    // A clock that was never set lands here. Years is the only unit that reads.
    const years = (days / 365).toFixed(days >= 3650 ? 0 : 1);
    return `${years}y`;
  }
  return hours === 0 ? `${days}d` : `${days}d ${hours}h`;
}

/**
 * Drift with its direction spelled out, e.g. `2m 15s ahead`, `1h 12m behind`.
 * Inside the in-sync band the direction is noise, so it reads `in sync (-3s)`
 * instead of dressing up a propagation-delay artifact as a finding.
 */
export function formatDrift(seconds: number): string {
  if (Math.abs(seconds) <= DRIFT_IN_SYNC_SECONDS) {
    const signed = seconds > 0 ? `+${Math.round(seconds)}` : `${Math.round(seconds)}`;
    return `in sync (${signed}s)`;
  }
  return `${formatDriftMagnitude(seconds)} ${seconds > 0 ? 'ahead' : 'behind'}`;
}

/** Compact signed form for tables and axis ticks: `+2m 15s`, `-1h 12m`. */
export function formatDriftSigned(seconds: number): string {
  const sign = seconds > 0 ? '+' : seconds < 0 ? '-' : '';
  return `${sign}${formatDriftMagnitude(seconds)}`;
}

/**
 * The trend, which is the part that says whether waiting makes it worse. A clock
 * set wrong once sits near zero here; a bad oscillator or a missing RTC walks.
 */
export function formatDriftRate(secondsPerDay: number | null): string {
  if (secondsPerDay === null) {
    return 'not enough history';
  }
  const magnitude = Math.abs(secondsPerDay);
  if (magnitude < 1) {
    return 'steady';
  }
  const direction = secondsPerDay > 0 ? 'gaining' : 'losing';
  return `${direction} ${formatDriftMagnitude(magnitude)}/day`;
}

/**
 * Why the drift is what it is, in one line. This is the sentence that turns a
 * number into an action: reset the clock once, or fix the node.
 */
/** Above this a trend is worth acting on; below it the clock is holding position. */
const NOTABLE_RATE_SECONDS_PER_DAY = 60;

export function driftDiagnosis(
  driftSeconds: number,
  ratePerDay: number | null,
  clockUnset: boolean,
  /** Step changes in the window — times the clock was *set* rather than drifting. */
  stepCount = 0
): string {
  if (clockUnset) {
    return 'Clock was never set — the node is reporting time from boot, not a real date.';
  }

  const moving = ratePerDay !== null && Math.abs(ratePerDay) >= NOTABLE_RATE_SECONDS_PER_DAY;

  // Resets lead, even from inside the in-sync band: a clock sitting at zero
  // because someone set it an hour ago is a different situation from one that
  // has held there by itself, and only the step count tells them apart.
  if (stepCount > 0) {
    const times = stepCount === 1 ? 'once' : `${stepCount} times`;
    if (moving) {
      return `The clock has been reset ${times} in this window and is ${
        ratePerDay! > 0 ? 'gaining' : 'losing'
      } ${formatDriftMagnitude(ratePerDay!)}/day since the last one — it will drift back. Resyncing is treating the symptom.`;
    }
    return `The clock has been reset ${times} in this window, so a resync does not hold here even though it looks settled right now.`;
  }

  if (Math.abs(driftSeconds) <= DRIFT_IN_SYNC_SECONDS) {
    return 'Within a minute of this server. Nothing to do.';
  }
  if (ratePerDay === null) {
    return 'Offset is real, but there is not enough history yet to say whether it is growing.';
  }
  if (!moving) {
    return 'Offset is holding steady — the clock was set wrong once rather than running badly. One resync should fix it.';
  }
  return `Offset is still moving at ${formatDriftMagnitude(ratePerDay)}/day, so a one-off resync will not hold. The node's timekeeping is the problem.`;
}
