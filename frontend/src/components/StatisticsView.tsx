import { useState, useEffect } from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  ResponsiveContainer,
  AreaChart,
  Area,
  Cell,
} from 'recharts';
import { Separator } from './ui/separator';
import { api } from '../api';
import { DEFAULT_STATS_WINDOW, STATS_WINDOWS } from '../types';
import {
  DRIFT_IN_SYNC_SECONDS,
  driftSeverityTextClass,
  formatDriftMagnitude,
  formatDriftRate,
  formatDriftSigned,
} from '../utils/clockDrift';
import { handleKeyboardActivate } from '../utils/a11y';
import type {
  ClockDriftBucket,
  ClockDriftHistogramBin,
  NoiseFloorHistoryStats,
  PacketsOverTime,
  RegionScopeStats,
  RepeaterClockDriftEntry,
  RepeaterClockDriftStats,
  StatisticsResponse,
  StatsWindow,
} from '../types';

function formatPercent(value: number): string {
  return `${value.toFixed(1)}%`;
}

function windowLabel(window: StatsWindow): string {
  return STATS_WINDOWS.find((w) => w.key === window)?.label ?? window;
}

/** Sentence fragment for empty states: "No packets heard in <phrase>." */
function windowPhrase(window: StatsWindow): string {
  return STATS_WINDOWS.find((w) => w.key === window)?.phrase ?? 'the selected window';
}

/**
 * Window picker. Every bounded panel below reflects the choice, so this is one
 * control rather than a per-chart selector.
 */
function WindowSelector({
  value,
  onChange,
  disabled,
}: {
  value: StatsWindow;
  onChange: (window: StatsWindow) => void;
  disabled?: boolean;
}) {
  return (
    <div
      role="group"
      aria-label="Statistics time window"
      className="inline-flex rounded-md border border-border p-0.5"
    >
      {STATS_WINDOWS.map((option) => (
        <button
          key={option.key}
          type="button"
          title={option.title}
          aria-pressed={option.key === value}
          disabled={disabled}
          onClick={() => onChange(option.key)}
          className={`rounded px-2 py-1 text-xs font-medium transition-colors disabled:opacity-60 ${
            option.key === value
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:bg-muted'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

/**
 * Regional flood-scope adoption. Deliberately shows fractions rather than bare
 * percentages: with adoption this sparse, "3 of 117" communicates the sample
 * size that "2.6%" hides.
 */
function RegionScopeStatsPanel({
  stats,
  windowKey,
}: {
  stats: RegionScopeStats;
  windowKey: StatsWindow;
}) {
  // Corrupt RF captures land in the packet table with random headers, some of
  // which claim to be region-scoped. At or below the measured floor there is
  // nothing to report but noise, so withhold the percentage and say so.
  const floor = stats.false_positive_floor;
  const withinNoise = stats.scoped_messages <= floor;
  // Real-world scoping is currently rare enough that the share rounds to "0.0%",
  // which reads as a broken widget. Below that resolution the fraction alone is
  // the honest presentation.
  const showTrafficPct = stats.total_messages > 0 && !withinNoise && stats.scoped_pct >= 0.05;

  return (
    <div>
      <h3 className="text-base font-semibold tracking-tight mb-2">
        Region Scope ({windowLabel(windowKey)})
      </h3>
      <p className="text-[0.8125rem] text-muted-foreground mb-3">
        How much local traffic uses regional flood scoping. Traffic covers all channel messages
        heard, including channels you have no key for; senders only counts channels you can decrypt,
        so the two use different denominators and will not match.
      </p>
      <div className="space-y-2">
        <div className="flex justify-between items-center gap-4">
          <span className="text-sm text-muted-foreground">Scoped messages</span>
          <span className="font-medium text-right">
            {stats.scoped_messages.toLocaleString()} of {stats.total_messages.toLocaleString()}
            {showTrafficPct && (
              <span className="text-muted-foreground"> ({formatPercent(stats.scoped_pct)})</span>
            )}
          </span>
        </div>
        <div className="flex justify-between items-center gap-4">
          <span className="text-sm text-muted-foreground">Senders using regions</span>
          <span className="font-medium text-right">
            {stats.scoped_senders.toLocaleString()} of {stats.total_senders.toLocaleString()}
            {stats.total_senders > 0 && (
              <span className="text-muted-foreground">
                {' '}
                ({formatPercent(stats.scoped_senders_pct)})
              </span>
            )}
          </span>
        </div>
      </div>
      {floor > 0 && stats.scoped_messages > 0 && (
        <p className="text-[0.8125rem] text-muted-foreground mt-2">
          {withinNoise
            ? `Scoped message count is at or below the estimated false-positive floor (${floor.toFixed(0)}) from corrupt packet captures, so it is not evidence of regional adoption.`
            : `Includes an estimated ${floor.toFixed(0)} false positives from corrupt packet captures.`}{' '}
          The sender count is unaffected — it requires successful decryption.
        </p>
      )}
      {stats.truncated && (
        <p className="text-[0.8125rem] text-muted-foreground mt-2">
          This window holds more packets than one pass can parse, so the traffic figures come from
          the most recent slice of it. The sender figures are unaffected.
        </p>
      )}
      {stats.total_messages === 0 && (
        <p className="text-sm text-muted-foreground mt-2">
          No channel messages heard in {windowPhrase(windowKey)}.
        </p>
      )}
    </div>
  );
}

const CHANNEL_BAR_COLORS = ['#0ea5e9', '#10b981', '#f59e0b', '#f43f5e', '#8b5cf6'];

const TOOLTIP_STYLE = {
  contentStyle: {
    backgroundColor: 'hsl(var(--popover))',
    border: '1px solid hsl(var(--border))',
    borderRadius: '6px',
    fontSize: '11px',
    color: 'hsl(var(--popover-foreground))',
  },
  itemStyle: { color: 'hsl(var(--popover-foreground))' },
  labelStyle: { color: 'hsl(var(--muted-foreground))' },
} as const;

// Buckets are never narrower than a minute, so seconds would always read ":00".
function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

/** "6 hours", "15 minutes" — for describing a bucket width in prose. */
function formatDuration(seconds: number): string {
  if (seconds % 86400 === 0) {
    const days = seconds / 86400;
    return days === 1 ? 'day' : `${days} days`;
  }
  if (seconds % 3600 === 0) {
    const hours = seconds / 3600;
    return hours === 1 ? 'hour' : `${hours} hours`;
  }
  const minutes = Math.max(1, Math.round(seconds / 60));
  return minutes === 1 ? 'minute' : `${minutes} minutes`;
}

function formatDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function formatDateTime(ts: number): string {
  const d = new Date(ts * 1000);
  return (
    d.toLocaleDateString([], { month: 'short', day: 'numeric' }) +
    ' ' +
    d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  );
}

/**
 * Axis labels have to follow the bucket width: "Mar 5 14:00" is noise on a
 * year of daily buckets, and a bare date is useless on an hour of minutes.
 */
function bucketLabeller(bucketSeconds: number): (ts: number) => string {
  if (bucketSeconds < 3600) return formatTime;
  if (bucketSeconds < 86400) return formatDateTime;
  return formatDate;
}

function PacketsChart({ series }: { series: PacketsOverTime }) {
  const bucketSeconds = series.bucket_seconds || 3600;
  const label = bucketLabeller(bucketSeconds);

  // Fill gaps so buckets with zero packets still appear on the chart
  const filled: { timestamp: number; count: number }[] = [];
  if (series.buckets.length > 0) {
    const first = series.buckets[0].timestamp;
    const last = series.buckets[series.buckets.length - 1].timestamp;
    const byTs = new Map(series.buckets.map((b) => [b.timestamp, b.count]));
    for (let ts = first; ts <= last; ts += bucketSeconds) {
      filled.push({ timestamp: ts, count: byTs.get(ts) ?? 0 });
    }
  }

  const data = filled.map((b, i) => ({
    idx: i,
    label: label(b.timestamp),
    count: b.count,
  }));

  // Show ~6 evenly-spaced tick labels
  const tickCount = Math.min(6, data.length);
  const tickIndices: number[] = [];
  if (data.length > 1) {
    for (let i = 0; i < tickCount; i++) {
      tickIndices.push(Math.round((i / (tickCount - 1)) * (data.length - 1)));
    }
  }

  return (
    <ResponsiveContainer width="100%" height={140}>
      <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
        <XAxis
          dataKey="idx"
          type="number"
          domain={[0, data.length - 1]}
          tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
          tickLine={false}
          axisLine={false}
          ticks={tickIndices}
          tickFormatter={(idx) => data[idx]?.label ?? ''}
        />
        <YAxis
          tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
          tickLine={false}
          axisLine={false}
          allowDecimals={false}
        />
        <RechartsTooltip
          {...TOOLTIP_STYLE}
          cursor={{
            stroke: 'hsl(var(--muted-foreground))',
            strokeWidth: 1,
            strokeDasharray: '3 3',
          }}
          labelFormatter={(idx) => data[Number(idx)]?.label ?? ''}
          formatter={(value) => [`${Number(value).toLocaleString()} packets`, 'Count']}
        />
        <Area
          type="monotone"
          dataKey="count"
          stroke="#0ea5e9"
          fill="#0ea5e9"
          fillOpacity={0.15}
          strokeWidth={1.5}
          dot={false}
          activeDot={{ r: 4, fill: '#0ea5e9', strokeWidth: 2, stroke: 'hsl(var(--popover))' }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function NoiseFloorChart({ history }: { history: NoiseFloorHistoryStats }) {
  const samples = history.samples;
  const bucketSeconds = history.bucket_seconds || history.sample_interval_seconds || 60;
  // Once buckets are wider than the sample interval each point is an average,
  // so draw the min/max band behind it — otherwise a noisy hour and a quiet one
  // average to the same flat line.
  const aggregated = bucketSeconds > (history.sample_interval_seconds || 60);
  const label = bucketLabeller(bucketSeconds);

  const data = samples.map((s, i) => ({
    idx: i,
    time: label(s.timestamp),
    noise_floor: s.noise_floor_dbm,
    spread:
      aggregated && s.min_dbm != null && s.max_dbm != null
        ? [s.min_dbm, s.max_dbm]
        : [s.noise_floor_dbm, s.noise_floor_dbm],
  }));

  const tickCount = Math.min(6, samples.length);
  const tickIndices: number[] = [];
  if (samples.length > 1) {
    for (let i = 0; i < tickCount; i++) {
      tickIndices.push(Math.round((i / (tickCount - 1)) * (samples.length - 1)));
    }
  }

  return (
    <ResponsiveContainer width="100%" height={120}>
      <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
        <XAxis
          dataKey="idx"
          type="number"
          domain={[0, samples.length - 1]}
          tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
          tickLine={false}
          axisLine={false}
          ticks={tickIndices}
          tickFormatter={(idx) => data[idx]?.time ?? ''}
        />
        <YAxis
          tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
          tickLine={false}
          axisLine={false}
          domain={['dataMin - 5', 'dataMax + 5']}
          tickFormatter={(v) => `${v}`}
        />
        <RechartsTooltip
          {...TOOLTIP_STYLE}
          cursor={{
            stroke: 'hsl(var(--muted-foreground))',
            strokeWidth: 1,
            strokeDasharray: '3 3',
          }}
          labelFormatter={(idx) => data[Number(idx)]?.time ?? ''}
          formatter={(value, name) =>
            name === 'spread'
              ? [
                  `${(value as [number, number])[0]} to ${(value as [number, number])[1]} dBm`,
                  'Range',
                ]
              : [`${value} dBm`, aggregated ? 'Mean' : 'Noise Floor']
          }
        />
        {aggregated && (
          <Area
            type="linear"
            dataKey="spread"
            stroke="none"
            fill="#8b5cf6"
            fillOpacity={0.12}
            dot={false}
            activeDot={false}
            isAnimationActive={false}
          />
        )}
        <Area
          type="linear"
          dataKey="noise_floor"
          stroke="#8b5cf6"
          fill="#8b5cf6"
          fillOpacity={0.15}
          strokeWidth={1.5}
          dot={false}
          activeDot={{ r: 4, fill: '#8b5cf6', strokeWidth: 2, stroke: 'hsl(var(--popover))' }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

/**
 * Repeater clock drift. Measured passively from the signed timestamp inside every
 * advert, so it covers every repeater we hear rather than only the ones we can log
 * in to.
 */
function RepeaterClockDriftPanel({
  stats,
  windowKey,
  onOpenNodeStats,
}: {
  stats: RepeaterClockDriftStats;
  windowKey: StatsWindow;
  onOpenNodeStats?: (publicKey: string) => void;
}) {
  const measured = stats.repeaters_with_samples;

  if (measured === 0) {
    return (
      <div>
        <h3 className="text-base font-semibold tracking-tight mb-2">
          Repeater Clock Drift ({windowLabel(windowKey)})
        </h3>
        <p className="text-sm text-muted-foreground">
          No advert timestamps measured in {windowPhrase(windowKey)}
          {stats.repeaters_total > 0
            ? ` from the ${stats.repeaters_total} repeater${stats.repeaters_total === 1 ? '' : 's'} heard.`
            : '.'}{' '}
          Drift is read from adverts, so a repeater has to advertise within the window to appear
          here.
        </p>
      </div>
    );
  }

  // A large signed median is the tell that the outlier is this server, not the
  // mesh: independent nodes do not agree on being wrong in the same direction.
  const ourClockSuspect = Math.abs(stats.median_drift_seconds) > DRIFT_IN_SYNC_SECONDS;
  const offCount = measured - stats.in_sync;

  return (
    <div>
      <h3 className="text-base font-semibold tracking-tight mb-2">
        Repeater Clock Drift ({windowLabel(windowKey)})
      </h3>
      <p className="text-[0.8125rem] text-muted-foreground mb-3">
        Read from the signed timestamp inside each advert — no login, no request, every repeater
        that advertises. Positive is ahead of this server, negative behind. Mesh airtime only ever
        makes a node look behind, so each reading keeps the least-delayed arrival of its hour.
      </p>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="text-center p-3 bg-muted/50 rounded-md">
          <div className="text-2xl font-bold">
            {measured}
            <span className="text-sm font-normal text-muted-foreground">
              /{stats.repeaters_total}
            </span>
          </div>
          <div className="text-xs text-muted-foreground">Measured</div>
        </div>
        <div className="text-center p-3 bg-muted/50 rounded-md">
          <div className="text-2xl font-bold text-success">{stats.in_sync}</div>
          <div className="text-xs text-muted-foreground">Within 1m</div>
        </div>
        <div className="text-center p-3 bg-muted/50 rounded-md">
          <div className={`text-2xl font-bold ${offCount > 0 ? 'text-warning' : ''}`}>
            {offCount}
          </div>
          <div className="text-xs text-muted-foreground">Off by more</div>
        </div>
        <div className="text-center p-3 bg-muted/50 rounded-md">
          <div className="text-2xl font-bold">
            {formatDriftMagnitude(stats.median_abs_drift_seconds)}
          </div>
          <div className="text-xs text-muted-foreground">Median offset</div>
        </div>
      </div>

      <div className="mt-3 space-y-1.5 text-sm">
        <div className="flex justify-between items-center gap-4">
          <span className="text-muted-foreground">By severity</span>
          <span className="text-right">
            <span className="text-success">{stats.in_sync} in sync</span>
            <span className="text-muted-foreground"> · </span>
            <span>{stats.minor} slightly off</span>
            <span className="text-muted-foreground"> · </span>
            <span className="text-warning">{stats.major} badly off</span>
            <span className="text-muted-foreground"> · </span>
            <span className="text-destructive">{stats.severe} way off</span>
          </span>
        </div>
        <div className="flex justify-between items-center gap-4">
          <span className="text-muted-foreground">
            Mean offset (magnitude)
            {stats.repeaters_unset_clock > 0 && (
              <span className="text-xs"> — clocks that are set</span>
            )}
          </span>
          <span className="font-medium">{formatDriftMagnitude(stats.mean_abs_drift_seconds)}</span>
        </div>
        {stats.repeaters_unset_clock > 0 && (
          <div className="flex justify-between items-center gap-4">
            <span className="text-muted-foreground">Clock never set</span>
            <span className="font-medium text-destructive">
              {stats.repeaters_unset_clock} repeater
              {stats.repeaters_unset_clock === 1 ? '' : 's'}
            </span>
          </div>
        )}
        {stats.oldest_sample_at !== null && (
          <div className="flex justify-between items-center gap-4">
            <span className="text-muted-foreground">Oldest reading held</span>
            <span className="font-medium">{formatDateTime(stats.oldest_sample_at)}</span>
          </div>
        )}
        <div className="flex justify-between items-center gap-4">
          <span className="text-muted-foreground">Adverts measured</span>
          <span className="font-medium">{stats.sample_count.toLocaleString()}</span>
        </div>
      </div>

      {ourClockSuspect && (
        <div className="mt-3 px-3 py-2 rounded-md bg-warning/10 border border-warning/20">
          <p className="text-xs text-warning">
            The median repeater reads {formatDriftSigned(stats.median_drift_seconds)} — half the
            mesh is off in the same direction by about that much. Independent nodes do not drift
            together, so check <strong>this server&apos;s</strong> clock before chasing theirs.
          </p>
        </div>
      )}

      <DriftDistributionChart bins={stats.histogram} />

      <DriftRankingTable
        title="Furthest off"
        caption="Largest offset first. Click a repeater for its full drift history."
        entries={stats.worst_offenders}
        onOpenNodeStats={onOpenNodeStats}
      />

      {stats.fastest_rates.length > 0 && (
        <DriftRankingTable
          title="Clocks still moving"
          caption="Ranked by trend, not offset. These will be worse tomorrow — a one-off resync will not hold."
          entries={stats.fastest_rates}
          showRate
          onOpenNodeStats={onOpenNodeStats}
        />
      )}

      {(stats.unset_clocks?.length ?? 0) > 0 && (
        <DriftRankingTable
          title="Clock never set"
          caption="These report time from boot rather than a date, so they are decades out. Kept out of the figures above, which would otherwise be meaningless."
          entries={stats.unset_clocks}
          onOpenNodeStats={onOpenNodeStats}
        />
      )}

      {stats.over_time.length > 1 && (
        <div className="mt-4">
          <h4 className="text-sm font-medium mb-1">Mesh-wide drift over time</h4>
          <p className="text-xs text-muted-foreground mb-2">
            Mean and worst offset across every repeater measured, one point per{' '}
            {formatDuration(stats.bucket_seconds)}. A rising line is the mesh losing time together;
            a single spike is usually one node rebooting.
          </p>
          <DriftOverTimeChart buckets={stats.over_time} />
        </div>
      )}
    </div>
  );
}

function DriftDistributionChart({ bins }: { bins: ClockDriftHistogramBin[] }) {
  const populated = bins.filter((bin) => bin.count > 0);
  if (populated.length < 2) {
    return null;
  }

  return (
    <div className="mt-4">
      <h4 className="text-sm font-medium mb-2">Distribution</h4>
      <ResponsiveContainer width="100%" height={120}>
        <BarChart data={bins} margin={{ top: 4, right: 4, bottom: 0, left: -24 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
          {/* No interval={0}: nine range labels fit on a desktop pane and turn
              into mush on a phone, so let Recharts thin them. The tooltip still
              names every bin. */}
          <XAxis
            dataKey="label"
            tick={{ fontSize: 9, fill: 'hsl(var(--muted-foreground))' }}
            tickLine={false}
            axisLine={false}
            minTickGap={6}
          />
          <YAxis
            tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
            tickLine={false}
            axisLine={false}
            allowDecimals={false}
          />
          <RechartsTooltip
            {...TOOLTIP_STYLE}
            cursor={{ fill: 'hsl(var(--muted))', opacity: 0.5 }}
            formatter={(value) => [
              `${Number(value)} repeater${Number(value) === 1 ? '' : 's'}`,
              null,
            ]}
          />
          <Bar dataKey="count" radius={[3, 3, 0, 0]} maxBarSize={34}>
            {bins.map((bin) => (
              // The centre bin is the healthy one; everything either side is not.
              <Cell key={bin.label} fill={bin.label === '±1m' ? '#10b981' : '#f59e0b'} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function DriftOverTimeChart({ buckets }: { buckets: ClockDriftBucket[] }) {
  const labelFor = bucketLabeller(
    buckets.length > 1 ? buckets[1].timestamp - buckets[0].timestamp : 3600
  );
  const data = buckets.map((bucket) => ({
    ...bucket,
    tick: labelFor(bucket.timestamp),
  }));

  return (
    <ResponsiveContainer width="100%" height={160}>
      <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -8 }}>
        <defs>
          <linearGradient id="driftMaxFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#f43f5e" stopOpacity={0.18} />
            <stop offset="100%" stopColor="#f43f5e" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
        <XAxis
          dataKey="tick"
          tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
          tickLine={false}
          axisLine={false}
          minTickGap={28}
        />
        <YAxis
          tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
          tickLine={false}
          axisLine={false}
          width={52}
          tickFormatter={(value) => formatDriftMagnitude(Number(value))}
        />
        <RechartsTooltip
          {...TOOLTIP_STYLE}
          cursor={{
            stroke: 'hsl(var(--muted-foreground))',
            strokeWidth: 1,
            strokeDasharray: '3 3',
          }}
          formatter={(value, name) => [
            formatDriftMagnitude(Number(value)),
            name === 'max_abs_drift_seconds' ? 'Worst' : 'Mean',
          ]}
        />
        <Area
          type="monotone"
          dataKey="max_abs_drift_seconds"
          stroke="#f43f5e"
          strokeWidth={1}
          fill="url(#driftMaxFill)"
        />
        <Area
          type="monotone"
          dataKey="mean_abs_drift_seconds"
          stroke="#8b5cf6"
          strokeWidth={1.5}
          fill="none"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function DriftRankingTable({
  title,
  caption,
  entries,
  showRate = false,
  onOpenNodeStats,
}: {
  title: string;
  caption: string;
  entries: RepeaterClockDriftEntry[];
  showRate?: boolean;
  onOpenNodeStats?: (publicKey: string) => void;
}) {
  if (entries.length === 0) {
    return null;
  }

  return (
    <div className="mt-4">
      <h4 className="text-sm font-medium mb-1">{title}</h4>
      <p className="text-xs text-muted-foreground mb-2">{caption}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-muted-foreground">
              <th className="text-left font-normal pb-1">Repeater</th>
              <th className="text-right font-normal pb-1">Offset</th>
              <th className="text-right font-normal pb-1">
                {showRate ? 'Trend' : 'Last measured'}
              </th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.public_key} className="border-t border-border/50">
                <td className="py-1 pr-2 max-w-[14rem]">
                  <span
                    className={
                      onOpenNodeStats
                        ? 'cursor-pointer hover:text-primary hover:underline transition-colors truncate inline-block max-w-full align-bottom'
                        : 'truncate inline-block max-w-full align-bottom'
                    }
                    role={onOpenNodeStats ? 'button' : undefined}
                    tabIndex={onOpenNodeStats ? 0 : undefined}
                    onKeyDown={onOpenNodeStats ? handleKeyboardActivate : undefined}
                    onClick={() => onOpenNodeStats?.(entry.public_key)}
                    title={onOpenNodeStats ? "Open this node's stats page" : undefined}
                  >
                    {entry.name || entry.public_key.slice(0, 12)}
                  </span>
                </td>
                <td
                  className={`text-right py-1 font-medium whitespace-nowrap ${driftSeverityTextClass(entry.severity)}`}
                >
                  {entry.clock_unset ? 'not set' : formatDriftSigned(entry.drift_seconds)}
                </td>
                <td className="text-right py-1 text-xs text-muted-foreground whitespace-nowrap">
                  {showRate
                    ? formatDriftRate(entry.drift_rate_seconds_per_day)
                    : formatDateTime(entry.observed_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function StatisticsView({
  onOpenNodeStats,
}: {
  /** Passed through so a repeater name in the drift tables opens its stats page. */
  onOpenNodeStats?: (publicKey: string) => void;
} = {}) {
  const [stats, setStats] = useState<StatisticsResponse | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const [statsError, setStatsError] = useState(false);
  const [selectedWindow, setSelectedWindow] = useState<StatsWindow>(DEFAULT_STATS_WINDOW);

  useEffect(() => {
    let cancelled = false;
    setStatsLoading(true);
    setStatsError(false);
    api.getStatistics(selectedWindow).then(
      (data) => {
        if (!cancelled) {
          setStats(data);
          setStatsLoading(false);
        }
      },
      () => {
        if (!cancelled) {
          setStatsError(true);
          setStatsLoading(false);
        }
      }
    );
    return () => {
      cancelled = true;
    };
  }, [selectedWindow]);

  // Which window the numbers on screen describe. While a wider window loads, the
  // previous snapshot is still rendered, so headings must follow the data rather
  // than the pending selection.
  const shownWindow = stats?.window ?? selectedWindow;
  // The activity table always shows 1h/24h/7d; a wider selection earns its own
  // column, a narrower one is already in there.
  const showWindowColumn = !['1h', '1d', '1w'].includes(shownWindow);
  const noiseFloor = stats?.noise_floor;
  const noiseFloorAggregated =
    !!noiseFloor && noiseFloor.bucket_seconds > (noiseFloor.sample_interval_seconds || 60);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold">Statistics</h2>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              Read-only mesh network statistics aggregated from stored messages and packets.
            </p>
          </div>
          <WindowSelector
            value={selectedWindow}
            onChange={setSelectedWindow}
            disabled={statsLoading}
          />
        </div>
        {statsLoading && stats && (
          <p className="mt-2 text-xs text-muted-foreground">
            Loading {windowPhrase(selectedWindow)}…
          </p>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[800px] space-y-4 p-4">
          {statsLoading && !stats ? (
            <div className="py-8 text-center text-muted-foreground">
              Loading statistics... this can take a while if you have a lot of stored packets.
            </div>
          ) : stats ? (
            <div className="space-y-6">
              {/* Network */}
              <div>
                <h3 className="text-base font-semibold tracking-tight mb-2">Network</h3>
                <div className="grid grid-cols-3 gap-3">
                  <div className="text-center p-3 bg-muted/50 rounded-md">
                    <div className="text-2xl font-bold">{stats.contact_count}</div>
                    <div className="text-xs text-muted-foreground">Contacts</div>
                  </div>
                  <div className="text-center p-3 bg-muted/50 rounded-md">
                    <div className="text-2xl font-bold">{stats.repeater_count}</div>
                    <div className="text-xs text-muted-foreground">Repeaters</div>
                  </div>
                  <div className="text-center p-3 bg-muted/50 rounded-md">
                    <div className="text-2xl font-bold">{stats.channel_count}</div>
                    <div className="text-xs text-muted-foreground">Channels</div>
                  </div>
                </div>
              </div>

              <Separator />

              {/* Messages */}
              <div>
                <h3 className="text-base font-semibold tracking-tight mb-2">Messages</h3>
                <div className="grid grid-cols-3 gap-3">
                  <div className="text-center p-3 bg-muted/50 rounded-md">
                    <div className="text-2xl font-bold">{stats.total_dms}</div>
                    <div className="text-xs text-muted-foreground">Direct Messages</div>
                  </div>
                  <div className="text-center p-3 bg-muted/50 rounded-md">
                    <div className="text-2xl font-bold">{stats.total_channel_messages}</div>
                    <div className="text-xs text-muted-foreground">Channel Messages</div>
                  </div>
                  <div className="text-center p-3 bg-muted/50 rounded-md">
                    <div className="text-2xl font-bold">{stats.total_outgoing}</div>
                    <div className="text-xs text-muted-foreground">Sent (Outgoing)</div>
                  </div>
                </div>
              </div>

              <Separator />

              {/* Activity */}
              <div>
                <h3 className="text-base font-semibold tracking-tight mb-2">Activity</h3>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-muted-foreground">
                      <th className="text-left font-normal pb-1"></th>
                      <th className="text-right font-normal pb-1">1h</th>
                      <th className="text-right font-normal pb-1">24h</th>
                      <th className="text-right font-normal pb-1">7d</th>
                      {showWindowColumn && (
                        <th className="text-right font-medium pb-1 text-foreground">
                          {windowLabel(shownWindow)}
                        </th>
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td className="py-1">Contacts heard</td>
                      <td className="text-right py-1">{stats.contacts_heard.last_hour}</td>
                      <td className="text-right py-1">{stats.contacts_heard.last_24_hours}</td>
                      <td className="text-right py-1">{stats.contacts_heard.last_week}</td>
                      {showWindowColumn && (
                        <td className="text-right py-1 font-medium">
                          {stats.contacts_heard.window}
                        </td>
                      )}
                    </tr>
                    <tr>
                      <td className="py-1">Repeaters heard</td>
                      <td className="text-right py-1">{stats.repeaters_heard.last_hour}</td>
                      <td className="text-right py-1">{stats.repeaters_heard.last_24_hours}</td>
                      <td className="text-right py-1">{stats.repeaters_heard.last_week}</td>
                      {showWindowColumn && (
                        <td className="text-right py-1 font-medium">
                          {stats.repeaters_heard.window}
                        </td>
                      )}
                    </tr>
                    <tr>
                      <td className="py-1">Known-channels active</td>
                      <td className="text-right py-1">{stats.known_channels_active.last_hour}</td>
                      <td className="text-right py-1">
                        {stats.known_channels_active.last_24_hours}
                      </td>
                      <td className="text-right py-1">{stats.known_channels_active.last_week}</td>
                      {showWindowColumn && (
                        <td className="text-right py-1 font-medium">
                          {stats.known_channels_active.window}
                        </td>
                      )}
                    </tr>
                  </tbody>
                </table>
              </div>

              <Separator />

              {/* Packets */}
              <div>
                <h3 className="text-base font-semibold tracking-tight mb-2">Packets</h3>
                <div className="space-y-2">
                  <div className="flex justify-between items-center">
                    <span className="text-sm text-muted-foreground">Total stored</span>
                    <span className="font-medium">{stats.total_packets}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-sm text-success">Decrypted</span>
                    <span className="font-medium text-success">{stats.decrypted_packets}</span>
                  </div>
                  <div className="flex justify-between items-center">
                    <span className="text-sm text-warning">Undecrypted</span>
                    <span className="font-medium text-warning">{stats.undecrypted_packets}</span>
                  </div>
                </div>
              </div>

              {/* Packet activity over the selected window */}
              {stats.packets_over_time.buckets.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <h3 className="text-base font-semibold tracking-tight mb-2">
                      Packet Activity ({windowLabel(shownWindow)})
                    </h3>
                    <div className="mb-2 text-xs text-muted-foreground">
                      One point per {formatDuration(stats.packets_over_time.bucket_seconds)}.
                    </div>
                    <PacketsChart series={stats.packets_over_time} />
                  </div>
                </>
              )}

              <Separator />

              {/* Path Hash Width */}
              <div>
                <h3 className="text-base font-semibold tracking-tight mb-2">
                  Path Hash Width ({windowLabel(shownWindow)})
                </h3>
                <div className="mb-2 text-xs text-muted-foreground">
                  Parsed stored raw packets from {windowPhrase(shownWindow)}:{' '}
                  {stats.path_hash_width.total_packets.toLocaleString()}
                  {stats.path_hash_width.truncated &&
                    ' (the most recent slice of the window — it holds more than one pass can parse)'}
                </div>
                {stats.path_hash_width.total_packets > 0 ? (
                  <ResponsiveContainer width="100%" height={120}>
                    <BarChart
                      data={[
                        {
                          name: '1-byte',
                          count: stats.path_hash_width.single_byte,
                          pct: stats.path_hash_width.single_byte_pct,
                        },
                        {
                          name: '2-byte',
                          count: stats.path_hash_width.double_byte,
                          pct: stats.path_hash_width.double_byte_pct,
                        },
                        {
                          name: '3-byte',
                          count: stats.path_hash_width.triple_byte,
                          pct: stats.path_hash_width.triple_byte_pct,
                        },
                      ]}
                      margin={{ top: 4, right: 4, bottom: 0, left: -16 }}
                    >
                      <CartesianGrid
                        strokeDasharray="3 3"
                        stroke="hsl(var(--border))"
                        vertical={false}
                      />
                      <XAxis
                        dataKey="name"
                        tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }}
                        tickLine={false}
                        axisLine={false}
                      />
                      <YAxis
                        tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
                        tickLine={false}
                        axisLine={false}
                        allowDecimals={false}
                      />
                      <RechartsTooltip
                        {...TOOLTIP_STYLE}
                        cursor={{ fill: 'hsl(var(--muted))', opacity: 0.5 }}
                        // eslint-disable-next-line @typescript-eslint/no-explicit-any
                        formatter={(value: any, _: any, props: any) => [
                          `${Number(value).toLocaleString()} (${formatPercent(props.payload.pct)})`,
                          'Packets',
                        ]}
                      />
                      <Bar dataKey="count" radius={[4, 4, 0, 0]} maxBarSize={40}>
                        <Cell fill="#0ea5e9" />
                        <Cell fill="#10b981" />
                        <Cell fill="#f59e0b" />
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    No path data in {windowPhrase(shownWindow)}.
                  </p>
                )}
              </div>

              <Separator />

              {/* Multibyte Rollout (node-level adoption; traffic-level shares are above) */}
              <div>
                <h3 className="text-base font-semibold tracking-tight mb-2">Multibyte Rollout</h3>
                <p className="text-[0.8125rem] text-muted-foreground mb-2">
                  Nodes whose known direct route uses multibyte hop identifiers — who has upgraded,
                  as opposed to how much traffic has (shown above).
                </p>
                {stats.multibyte_rollout.contacts_with_route > 0 ? (
                  <div className="space-y-1.5 text-sm">
                    <div>
                      <span className="font-medium">
                        {stats.multibyte_rollout.contacts_multibyte} of{' '}
                        {stats.multibyte_rollout.contacts_with_route}
                      </span>{' '}
                      <span className="text-muted-foreground">
                        nodes with a known route use multibyte hops
                      </span>
                    </div>
                    <div>
                      <span className="font-medium">
                        {stats.multibyte_rollout.repeaters_multibyte} of{' '}
                        {stats.multibyte_rollout.repeaters_with_route}
                      </span>{' '}
                      <span className="text-muted-foreground">repeaters</span>
                    </div>
                    <div className="text-xs text-muted-foreground">
                      By hop width: {stats.multibyte_rollout.single_byte} × 1-byte ·{' '}
                      {stats.multibyte_rollout.double_byte} × 2-byte ·{' '}
                      {stats.multibyte_rollout.triple_byte} × 3-byte
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    No contacts with a known direct-route hop width yet.
                  </p>
                )}
              </div>

              <Separator />

              {/* Region Scope */}
              <RegionScopeStatsPanel stats={stats.region_scope} windowKey={shownWindow} />

              {/* Busiest Channels */}
              {stats.busiest_channels.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <h3 className="text-base font-semibold tracking-tight mb-2">
                      Busiest Channels ({windowLabel(shownWindow)})
                    </h3>
                    <ResponsiveContainer
                      width="100%"
                      height={stats.busiest_channels.length * 28 + 8}
                    >
                      <BarChart
                        data={stats.busiest_channels.map((ch) => ({
                          name: ch.channel_name,
                          messages: ch.message_count,
                        }))}
                        layout="vertical"
                        margin={{ top: 0, right: 4, bottom: 0, left: 0 }}
                        barCategoryGap="20%"
                      >
                        <XAxis type="number" hide />
                        <YAxis
                          type="category"
                          dataKey="name"
                          tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }}
                          tickLine={false}
                          axisLine={false}
                          width={100}
                        />
                        <RechartsTooltip
                          {...TOOLTIP_STYLE}
                          cursor={{ fill: 'hsl(var(--muted))', opacity: 0.5 }}
                          formatter={(value) => [
                            `${Number(value).toLocaleString()} messages`,
                            null,
                          ]}
                        />
                        <Bar dataKey="messages" radius={[0, 4, 4, 0]} maxBarSize={16}>
                          {stats.busiest_channels.map((_, i) => (
                            <Cell
                              key={i}
                              fill={CHANNEL_BAR_COLORS[i % CHANNEL_BAR_COLORS.length]}
                            />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </>
              )}

              {/* Noise Floor */}
              {stats.noise_floor && (
                <>
                  <Separator />
                  <div>
                    <h3 className="text-base font-semibold tracking-tight mb-2">
                      Noise Floor ({windowLabel(shownWindow)})
                    </h3>
                    {stats.noise_floor.latest_noise_floor_dbm != null && (
                      <div className="mb-2 text-xs text-muted-foreground">
                        Latest reading: {stats.noise_floor.latest_noise_floor_dbm} dBm
                        {stats.noise_floor.latest_timestamp != null &&
                          ` at ${new Date(
                            stats.noise_floor.latest_timestamp * 1000
                          ).toLocaleTimeString([], {
                            hour: '2-digit',
                            minute: '2-digit',
                          })}`}
                        {noiseFloorAggregated &&
                          ` · averaged into buckets of ${formatDuration(
                            stats.noise_floor.bucket_seconds
                          )}, shaded band is the min/max`}
                      </div>
                    )}
                    {stats.noise_floor.samples.length > 1 ? (
                      <NoiseFloorChart history={stats.noise_floor} />
                    ) : stats.noise_floor.samples.length === 0 ? (
                      <p className="text-sm text-muted-foreground">
                        No noise floor samples stored for {windowPhrase(shownWindow)}. Samples are
                        collected every minute while the radio is connected and kept for a year.
                      </p>
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        Only one sample so far ({stats.noise_floor.samples[0].noise_floor_dbm} dBm).
                        More data needed for a chart. Samples are collected every minute while the
                        radio is connected and kept for a year.
                      </p>
                    )}
                  </div>
                </>
              )}

              {/* Guarded like noise_floor above: a snapshot from a backend that
                  predates this section must render the rest of the page, not blank it. */}
              {stats.repeater_clock_drift && (
                <>
                  <Separator />
                  <RepeaterClockDriftPanel
                    stats={stats.repeater_clock_drift}
                    windowKey={shownWindow}
                    onOpenNodeStats={onOpenNodeStats}
                  />
                </>
              )}
            </div>
          ) : statsError ? (
            <div className="py-8 text-center text-muted-foreground">Failed to load statistics.</div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
