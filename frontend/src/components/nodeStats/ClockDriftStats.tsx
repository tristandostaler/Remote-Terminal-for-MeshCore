/**
 * The clock-drift section of the node stats page.
 *
 * The contact info pane answers "is this clock all right?" in one line. This
 * answers the follow-up questions: how has it behaved, when was it last set,
 * and can I trust the measurement. Sign convention and the two biases baked
 * into every figure live in `app/clock_drift.py`.
 */

import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  XAxis,
  YAxis,
} from 'recharts';

import {
  DRIFT_IN_SYNC_SECONDS,
  driftDiagnosis,
  driftSeverityColor,
  driftSeverityLabel,
  driftSeverityTextClass,
  formatDrift,
  formatDriftMagnitude,
  formatDriftRate,
  formatDriftSigned,
} from '../../utils/clockDrift';
import type { NodeClockDriftStats, StatsWindow } from '../../types';
import {
  formatDateTime,
  formatDuration,
  ScrollableTable,
  StatRow,
  StatSection,
  StatSubheading,
  StatTile,
  StatTileRow,
  TOOLTIP_STYLE,
  windowLabel,
  windowPhrase,
} from './nodeStatsShared';

export function ClockDriftStats({
  drift,
  windowKey,
}: {
  drift: NodeClockDriftStats;
  windowKey: StatsWindow;
}) {
  const spread = drift.max_drift_seconds - drift.min_drift_seconds;
  const directShare =
    drift.bucket_count > 0 ? Math.round((drift.direct_sample_count / drift.bucket_count) * 100) : 0;

  return (
    <StatSection
      title={`Clock Drift (${windowLabel(windowKey)})`}
      description={
        <>
          Measured from the signed timestamp inside each advert, against this server&apos;s clock —
          so a wrong clock here reads as every node drifting the same way. Positive is ahead of this
          server, negative behind. Mesh airtime only ever makes a node look <em>behind</em>, so each
          reading keeps the least-delayed arrival of its hour.
        </>
      }
      aside={`${drift.sample_count.toLocaleString()} adverts · ${drift.bucket_count.toLocaleString()} readings`}
    >
      <div className="space-y-6">
        <div>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span
              className={`text-3xl font-bold leading-none ${driftSeverityTextClass(drift.severity)}`}
            >
              {drift.clock_unset ? 'Clock not set' : formatDrift(drift.latest_drift_seconds)}
            </span>
            {!drift.clock_unset && (
              <span className="text-sm text-muted-foreground">
                {driftSeverityLabel(drift.severity)}
              </span>
            )}
          </div>
          <p className="mt-1.5 text-sm text-muted-foreground">
            {driftDiagnosis(
              drift.latest_drift_seconds,
              drift.drift_rate_seconds_per_day,
              drift.clock_unset,
              drift.step_count
            )}
          </p>
        </div>

        <StatTileRow>
          <StatTile
            value={formatDriftRate(drift.drift_rate_seconds_per_day)}
            label={drift.rate_since_last_step ? 'Trend since last reset' : 'Trend'}
            tone={
              drift.drift_rate_seconds_per_day !== null &&
              Math.abs(drift.drift_rate_seconds_per_day) >= 60
                ? 'text-warning'
                : undefined
            }
          />
          <StatTile value={formatDriftMagnitude(spread)} label="Spread over window" />
          <StatTile
            value={`${directShare}%`}
            label="Direct readings"
            tone={directShare === 0 ? 'text-warning' : undefined}
          />
          <StatTile value={drift.steps.length} label="Step changes" />
        </StatTileRow>

        <div className="space-y-1.5">
          <StatRow
            label="Range"
            value={`${formatDriftSigned(drift.min_drift_seconds)} → ${formatDriftSigned(
              drift.max_drift_seconds
            )}`}
          />
          <StatRow label="Mean" value={formatDriftSigned(drift.mean_drift_seconds)} />
          <StatRow label="Last measured" value={formatDateTime(drift.latest_observed_at)} />
          <StatRow
            label="Last arrival"
            value={
              drift.latest_path_len === 0 ? 'direct (0 hops)' : `${drift.latest_path_len} hops`
            }
          />
          <StatRow label="Watching since" value={formatDateTime(drift.first_observed_at)} />
        </div>

        {directShare === 0 && (
          <div className="rounded-md border border-warning/20 bg-warning/10 px-3 py-2">
            <p className="text-xs text-warning">
              Every reading for this node arrived over at least one hop, so all of them carry some
              airtime that reads as the clock being behind. The offset is a floor on how far behind
              it really is, not an exact figure.
            </p>
          </div>
        )}

        <DriftSeriesChart drift={drift} windowKey={windowKey} />

        <StepChanges drift={drift} windowKey={windowKey} />

        <HopBreakdown drift={drift} />

        <DriftDistribution drift={drift} />
      </div>
    </StatSection>
  );
}

function DriftSeriesChart({
  drift,
  windowKey,
}: {
  drift: NodeClockDriftStats;
  windowKey: StatsWindow;
}) {
  if (drift.series.length < 2) {
    return (
      <div>
        <StatSubheading title="Drift over time" />
        <p className="text-sm text-muted-foreground">
          Only {drift.series.length === 1 ? 'one reading' : 'no readings'} in{' '}
          {windowPhrase(windowKey)} — not enough for a line. Try a wider window.
        </p>
      </div>
    );
  }

  // The band is drawn as two stacked areas rather than a range series, which
  // Recharts has no primitive for: a transparent floor up to the minimum, then
  // the spread on top of it.
  const data = drift.series.map((band) => ({
    ...band,
    base: band.min_drift_seconds,
    band: band.max_drift_seconds - band.min_drift_seconds,
    tick: formatDateTime(band.bucket_start),
  }));
  const bandIsVisible = drift.series.some(
    (band) => band.max_drift_seconds !== band.min_drift_seconds
  );

  return (
    <div>
      <StatSubheading
        title="Drift over time"
        description={
          <>
            One point per {formatDuration(drift.bucket_seconds)}
            {bandIsVisible
              ? ', with the shaded band showing the spread of readings inside it. A wide band is a jittery clock or a mix of path lengths; a thin one that climbs is a steady drift.'
              : '. Each point is a single reading, so there is no spread to shade.'}
          </>
        }
      />
      <ResponsiveContainer width="100%" height={220}>
        <ComposedChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
          {/* Zero is the only meaningful reference here, so it gets its own line
              rather than being wherever the axis happens to land. */}
          <ReferenceLine y={0} stroke="hsl(var(--muted-foreground))" strokeDasharray="2 2" />
          <XAxis
            dataKey="tick"
            tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
            tickLine={false}
            axisLine={false}
            minTickGap={40}
          />
          <YAxis
            tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
            tickLine={false}
            axisLine={false}
            width={62}
            allowDecimals={false}
            tickFormatter={(value) => formatDriftSigned(Number(value))}
          />
          <RechartsTooltip
            {...TOOLTIP_STYLE}
            cursor={{
              stroke: 'hsl(var(--muted-foreground))',
              strokeWidth: 1,
              strokeDasharray: '3 3',
            }}
            formatter={(value, name, props) => {
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              const point = (props as any)?.payload;
              if (name === 'base') return [null, null];
              if (name === 'band') {
                return [
                  `${formatDriftSigned(point.min_drift_seconds)} → ${formatDriftSigned(
                    point.max_drift_seconds
                  )}`,
                  'Spread',
                ];
              }
              const hops = point?.direct_reading_count
                ? `${point.direct_reading_count} of ${point.reading_count} direct`
                : `${point.reading_count} reading${point.reading_count === 1 ? '' : 's'}`;
              return [`${formatDriftSigned(Number(value))} (${hops})`, 'Best reading'];
            }}
          />
          <Area
            dataKey="base"
            stackId="band"
            stroke="none"
            fill="none"
            isAnimationActive={false}
            legendType="none"
          />
          <Area
            dataKey="band"
            stackId="band"
            stroke="none"
            fill="#8b5cf6"
            fillOpacity={0.18}
            isAnimationActive={false}
          />
          <Line
            type="linear"
            dataKey="drift_seconds"
            stroke="#8b5cf6"
            strokeWidth={1.5}
            dot={false}
            activeDot={{ r: 4, strokeWidth: 2, stroke: 'hsl(var(--popover))' }}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function StepChanges({ drift, windowKey }: { drift: NodeClockDriftStats; windowKey: StatsWindow }) {
  return (
    <div>
      <StatSubheading
        title="Step changes"
        description="Moments the clock was set rather than drifting — a reboot, a manual sync, a GPS fix landing. Only jumps well past what this node's own trend accounts for are listed."
      />
      {drift.steps.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No step changes in {windowPhrase(windowKey)}. Whatever this clock is doing, it is doing it
          smoothly.
        </p>
      ) : (
        <ScrollableTable>
          <thead>
            <tr className="text-muted-foreground">
              <th className="pb-1 text-left font-normal">When</th>
              <th className="pb-1 text-right font-normal">Jump</th>
              <th className="pb-1 text-right font-normal">From → to</th>
              <th className="pb-1 text-right font-normal">Across</th>
            </tr>
          </thead>
          <tbody>
            {drift.steps.map((step) => (
              <tr key={`${step.at}-${step.delta_seconds}`} className="border-t border-border/50">
                <td className="py-1 pr-2 whitespace-nowrap">{formatDateTime(step.at)}</td>
                <td
                  className={`py-1 text-right font-medium whitespace-nowrap ${
                    step.delta_seconds > 0 ? 'text-success' : 'text-warning'
                  }`}
                >
                  {formatDriftSigned(step.delta_seconds)}
                </td>
                <td className="py-1 text-right text-xs whitespace-nowrap text-muted-foreground">
                  {formatDriftSigned(step.from_drift_seconds)} →{' '}
                  {formatDriftSigned(step.to_drift_seconds)}
                </td>
                <td
                  className="py-1 text-right text-xs whitespace-nowrap text-muted-foreground"
                  title={
                    step.gap_seconds > 6 * 3600
                      ? 'A long gap between readings — the clock could have moved at any point inside it'
                      : undefined
                  }
                >
                  {formatDuration(step.gap_seconds)}
                  {step.gap_seconds > 6 * 3600 && ' *'}
                </td>
              </tr>
            ))}
          </tbody>
        </ScrollableTable>
      )}
      {drift.steps.some((step) => step.gap_seconds > 6 * 3600) && (
        <p className="mt-2 text-xs text-muted-foreground">
          * Readings either side are more than six hours apart, so the jump is dated only to
          somewhere inside that gap.
        </p>
      )}
    </div>
  );
}

function HopBreakdown({ drift }: { drift: NodeClockDriftStats }) {
  if (drift.hop_breakdown.length < 2) {
    return null;
  }

  const direct = drift.hop_breakdown.find((bucket) => bucket.path_len === 0);
  const furthest = drift.hop_breakdown[drift.hop_breakdown.length - 1];
  // If the mean falls away with distance, that is airtime showing up in the
  // measurement — worth naming, because it bounds how precise any of this is.
  const airtimeVisible =
    direct !== undefined &&
    furthest.path_len > 0 &&
    direct.mean_drift_seconds - furthest.mean_drift_seconds > 1;

  return (
    <div>
      <StatSubheading
        title="Readings by hop count"
        description="Airtime only ever makes a node look behind, so a mean that falls away as hops rise is that bias made visible — and confirms which readings to trust."
      />
      <ScrollableTable>
        <thead>
          <tr className="text-muted-foreground">
            <th className="pb-1 text-left font-normal">Hops</th>
            <th className="pb-1 text-right font-normal">Readings</th>
            <th className="pb-1 text-right font-normal">Mean</th>
            <th className="pb-1 text-right font-normal">Range</th>
          </tr>
        </thead>
        <tbody>
          {drift.hop_breakdown.map((bucket) => (
            <tr key={bucket.path_len} className="border-t border-border/50">
              <td className="py-1">
                {bucket.path_len === 0 ? (
                  <span className="text-success">direct</span>
                ) : (
                  `${bucket.path_len} hop${bucket.path_len === 1 ? '' : 's'}`
                )}
              </td>
              <td className="py-1 text-right">{bucket.reading_count.toLocaleString()}</td>
              <td className="py-1 text-right font-medium whitespace-nowrap">
                {formatDriftSigned(bucket.mean_drift_seconds)}
              </td>
              <td className="py-1 text-right text-xs whitespace-nowrap text-muted-foreground">
                {formatDriftSigned(bucket.min_drift_seconds)} →{' '}
                {formatDriftSigned(bucket.max_drift_seconds)}
              </td>
            </tr>
          ))}
        </tbody>
      </ScrollableTable>
      {airtimeVisible && direct && (
        <p className="mt-2 text-xs text-muted-foreground">
          Direct arrivals read{' '}
          {formatDriftMagnitude(direct.mean_drift_seconds - furthest.mean_drift_seconds)} less
          behind than {furthest.path_len}-hop ones. That difference is airtime, not the clock, so
          the direct figure is the honest one.
        </p>
      )}
    </div>
  );
}

function DriftDistribution({ drift }: { drift: NodeClockDriftStats }) {
  const populated = drift.histogram.filter((bin) => bin.count > 0);
  if (populated.length < 2) {
    return null;
  }

  return (
    <div>
      <StatSubheading
        title="Distribution"
        description="Where this node's readings landed. A single tall bar is a stable offset; a spread means the clock has been in several different states."
      />
      <ResponsiveContainer width="100%" height={140}>
        <BarChart data={drift.histogram} margin={{ top: 4, right: 4, bottom: 0, left: -20 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
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
              `${Number(value).toLocaleString()} reading${Number(value) === 1 ? '' : 's'}`,
              null,
            ]}
          />
          <Bar dataKey="count" radius={[3, 3, 0, 0]} maxBarSize={34}>
            {drift.histogram.map((bin) => (
              <Cell
                key={bin.label}
                fill={
                  bin.label === '±1m' ? driftSeverityColor('in_sync') : driftSeverityColor('major')
                }
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <p className="mt-1 text-xs text-muted-foreground">
        The centre bin is the ±{DRIFT_IN_SYNC_SECONDS}s band this app treats as in sync.
      </p>
    </div>
  );
}
