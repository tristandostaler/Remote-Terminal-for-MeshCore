/**
 * The Statistics-page section for the live feed comparison.
 *
 * Reads `stats.live_compare` for the page's window: coverage tiles, a stacked
 * "who heard it" chart across the window, and a per-channel table. Everything
 * here is a summary; the row-by-row view is the Live Compare page it links to.
 */

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { ExternalLink } from 'lucide-react';

import type { LiveCompareStats, StatsWindow } from '../../types';
import {
  ScrollableTable,
  StatSection,
  StatTile,
  StatTileRow,
  TOOLTIP_STYLE,
  windowPhrase,
} from '../nodeStats/nodeStatsShared';
import { SOURCE_META, describeSync, formatPercent, liveHostLabel } from './liveCompareShared';

function bucketLabeller(bucketSeconds: number): (ts: number) => string {
  if (bucketSeconds >= 86400) {
    return (ts) => new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }
  return (ts) => {
    const date = new Date(ts * 1000);
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return bucketSeconds >= 3600 * 6
      ? `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`
      : time;
  };
}

export function LiveComparePanel({
  stats,
  windowKey,
  onOpenLiveCompare,
}: {
  stats: LiveCompareStats;
  windowKey: StatsWindow;
  onOpenLiveCompare?: () => void;
}) {
  const { status } = stats;
  const total = stats.both + stats.node_only + stats.live_only;
  const label = bucketLabeller(stats.bucket_seconds);
  const now = Math.floor(Date.now() / 1000);
  const host = liveHostLabel(status.url);
  const chartData = stats.over_time.map((b) => ({
    timestamp: b.timestamp,
    [SOURCE_META.both.label]: b.both,
    [SOURCE_META.node.label]: b.node_only,
    [SOURCE_META.live.label]: b.live_only,
  }));

  return (
    <StatSection
      title="Live feed comparison"
      description={
        <>
          Channel messages over {windowPhrase(windowKey)} on {status.channels.join(', ') || '—'},
          compared with what the observers feeding {host}
          {status.region ? ` (region ${status.region})` : ''} heard. A message counts once, matched
          on identical decrypted text and sender clock. {describeSync(status, now)}.
        </>
      }
      aside={
        onOpenLiveCompare ? (
          <button
            type="button"
            onClick={onOpenLiveCompare}
            className="inline-flex items-center gap-1 text-primary hover:underline"
          >
            Browse messages <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </button>
        ) : undefined
      }
    >
      <div className="space-y-4" data-testid="live-compare-panel">
        <StatTileRow>
          <StatTile value={stats.both} label={SOURCE_META.both.label} tone="text-success" />
          <StatTile value={stats.node_only} label={SOURCE_META.node.label} tone="text-info" />
          <StatTile value={stats.live_only} label={SOURCE_META.live.label} tone="text-warning" />
          <StatTile
            value={formatPercent(stats.node_coverage_pct)}
            label="Node heard of live feed"
            suffix={
              stats.node_coverage_pct !== null ? (
                <span className="block text-[0.625rem]">
                  {stats.both} of {stats.both + stats.live_only}
                </span>
              ) : undefined
            }
          />
        </StatTileRow>

        {total === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing to compare in {windowPhrase(windowKey)} yet.
          </p>
        ) : (
          <>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>
                Live feed heard{' '}
                <span className="font-medium text-foreground">
                  {formatPercent(stats.live_coverage_pct)}
                </span>{' '}
                of what this node heard ({stats.both} of {stats.both + stats.node_only}).
              </span>
            </div>

            {chartData.length > 1 && (
              <div className="h-40" data-testid="live-compare-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={chartData} margin={{ top: 4, right: 4, bottom: 0, left: -20 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                    <XAxis
                      dataKey="timestamp"
                      tickFormatter={label}
                      tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
                      minTickGap={32}
                    />
                    <YAxis
                      allowDecimals={false}
                      tick={{ fontSize: 10, fill: 'hsl(var(--muted-foreground))' }}
                    />
                    <Tooltip
                      {...TOOLTIP_STYLE}
                      labelFormatter={(ts) => label(Number(ts))}
                      cursor={{ fill: 'hsl(var(--muted))', opacity: 0.4 }}
                    />
                    <Bar
                      dataKey={SOURCE_META.both.label}
                      stackId="a"
                      fill={SOURCE_META.both.fill}
                    />
                    <Bar
                      dataKey={SOURCE_META.node.label}
                      stackId="a"
                      fill={SOURCE_META.node.fill}
                    />
                    <Bar
                      dataKey={SOURCE_META.live.label}
                      stackId="a"
                      fill={SOURCE_META.live.fill}
                      radius={[2, 2, 0, 0]}
                    />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}

            {stats.channels.length > 0 && (
              <ScrollableTable>
                <thead>
                  <tr className="text-left text-[0.625rem] uppercase tracking-wider text-muted-foreground">
                    <th className="py-1 pr-3 font-medium">Channel</th>
                    <th className="py-1 pr-3 text-right font-medium">Both</th>
                    <th className="py-1 pr-3 text-right font-medium">Node only</th>
                    <th className="py-1 pr-3 text-right font-medium">Live only</th>
                    <th className="py-1 text-right font-medium">Node heard</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.channels.map((channel) => {
                    const liveTotal = channel.both + channel.live_only;
                    const pct = liveTotal ? (100 * channel.both) / liveTotal : null;
                    return (
                      <tr
                        key={channel.channel_key ?? channel.channel_name}
                        className="border-t border-border/60"
                      >
                        <td className="py-1 pr-3">{channel.channel_name}</td>
                        <td className="py-1 pr-3 text-right tabular-nums text-success">
                          {channel.both}
                        </td>
                        <td className="py-1 pr-3 text-right tabular-nums text-info">
                          {channel.node_only}
                        </td>
                        <td className="py-1 pr-3 text-right tabular-nums text-warning">
                          {channel.live_only}
                        </td>
                        <td className="py-1 text-right tabular-nums">{formatPercent(pct)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </ScrollableTable>
            )}
          </>
        )}
      </div>
    </StatSection>
  );
}
