/**
 * Live Compare: this node's channel messages merged with the ones a public
 * CoreScope instance (live.meshcore.ca) observed, one row per message, each
 * marked with who heard it.
 *
 * The page owns one window, one channel filter, one source filter and a
 * search box; the summary tiles and the list always describe the same
 * selection. Two requests back it: `/live-feed/stats` (status + tiles) and
 * `/live-feed/messages` (the page). Only the cheap `/live-feed/status` is
 * re-polled every `STATUS_POLL_MS`; the stats query and the list are reloaded
 * when it reports a newer completed sync, so the page follows the sync loop
 * without WebSocket plumbing and without re-running the comparison on a timer.
 */

import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowUpRight, ExternalLink, RefreshCw, Route, Settings2 } from 'lucide-react';

import { api, isAbortError } from '../../api';
import { cn } from '@/lib/utils';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { formatTime } from '../../utils/messageParser';
import { DEFAULT_STATS_WINDOW } from '../../types';
import type {
  Channel,
  LiveCompareMessage,
  LiveCompareSource,
  LiveCompareStats,
  StatsWindow,
} from '../../types';
import {
  StatTile,
  WindowSelector,
  formatDateTime,
  windowPhrase,
} from '../nodeStats/nodeStatsShared';
import {
  SOURCE_META,
  SOURCE_ORDER,
  SourceBadge,
  describeSync,
  formatPercent,
  liveChannelUrl,
  liveHostLabel,
} from './liveCompareShared';

const LiveCompareTraceDialog = lazy(() =>
  import('./LiveCompareTraceDialog').then((m) => ({ default: m.LiveCompareTraceDialog }))
);

const PAGE_SIZE = 100;
const STATUS_POLL_MS = 30_000;
const SEARCH_DEBOUNCE_MS = 300;

interface LiveCompareViewProps {
  channels: Channel[];
  /** Opens Settings › Live Compare. Absent when the host cannot open settings. */
  onOpenSettings?: () => void;
}

/** Split the stored "Sender: body" back into its parts for display. */
function splitText(message: LiveCompareMessage): { sender: string | null; body: string } {
  const sender = message.sender;
  if (sender && message.text.startsWith(`${sender}: `)) {
    return { sender, body: message.text.slice(sender.length + 2) };
  }
  const colon = message.text.indexOf(': ');
  if (!sender && colon > 0 && colon < 40) {
    return { sender: message.text.slice(0, colon), body: message.text.slice(colon + 2) };
  }
  return { sender, body: message.text };
}

function MessageRow({
  message,
  showChannel,
  onInspect,
}: {
  message: LiveCompareMessage;
  showChannel: boolean;
  onInspect: (message: LiveCompareMessage) => void;
}) {
  const meta = SOURCE_META[message.source];
  const { sender, body } = splitText(message);
  const details: string[] = [];
  if (message.node_received_at) {
    const paths = message.node_path_count ?? 0;
    details.push(
      `node ${message.outgoing ? 'sent' : 'heard'} ${formatDateTime(message.node_received_at)}` +
        (paths > 1 ? ` · ${paths} paths` : '')
    );
  }
  if (message.live_first_seen) {
    const bits = [`live heard ${formatDateTime(message.live_first_seen)}`];
    if (message.live_observers.length) {
      bits.push(
        `${message.live_observers.length} observer${message.live_observers.length === 1 ? '' : 's'}`
      );
    }
    if ((message.live_repeats ?? 0) > 1) bits.push(`${message.live_repeats} repeats`);
    if (message.live_hops !== null) {
      bits.push(`${message.live_hops} hop${message.live_hops === 1 ? '' : 's'}`);
    }
    if (message.live_snr !== null) bits.push(`SNR ${message.live_snr.toFixed(1)}`);
    details.push(bits.join(' · '));
  }
  const inspectTitle =
    'Show where this message travelled: every hop, relay and observer, on a map' +
    (message.live_observers.length ? ` · observers: ${message.live_observers.join(', ')}` : '');

  return (
    <li
      className={cn('border-l-2 py-2 pl-3 pr-1', meta.border)}
      data-testid="live-compare-row"
      data-source={message.source}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.6875rem] text-muted-foreground">
        <span className="tabular-nums" title={formatDateTime(message.seen_at)}>
          {formatTime(message.seen_at)}
        </span>
        <SourceBadge source={message.source} compact />
        {showChannel && message.channel_name && (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[0.625rem]">
            {message.channel_name}
          </span>
        )}
        {message.outgoing && (
          <span className="inline-flex items-center gap-0.5" title="Sent from this node">
            <ArrowUpRight className="h-3 w-3" aria-hidden="true" /> sent
          </span>
        )}
      </div>
      <div className="mt-0.5 break-words text-sm">
        {sender && <span className="font-medium">{sender}: </span>}
        <span className="whitespace-pre-wrap">{body}</span>
      </div>
      <button
        type="button"
        onClick={() => onInspect(message)}
        className="mt-0.5 inline-flex max-w-full items-start gap-1 text-left text-[0.6875rem] text-muted-foreground hover:text-foreground hover:underline"
        title={inspectTitle}
        aria-label={inspectTitle}
        data-testid="live-compare-inspect"
      >
        <Route className="mt-px h-3 w-3 shrink-0" aria-hidden="true" />
        <span>{details.length > 0 ? details.join('  ·  ') : 'Trace'}</span>
      </button>
    </li>
  );
}

export function LiveCompareView({ channels, onOpenSettings }: LiveCompareViewProps) {
  const [statsWindow, setStatsWindow] = useState<StatsWindow>(DEFAULT_STATS_WINDOW);
  const [channelKey, setChannelKey] = useState<string>('');
  const [source, setSource] = useState<LiveCompareSource | null>(null);
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');

  const [stats, setStats] = useState<LiveCompareStats | null | undefined>(undefined);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [messages, setMessages] = useState<LiveCompareMessage[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState({ both: 0, node_only: 0, live_only: 0 });
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  const [inspecting, setInspecting] = useState<LiveCompareMessage | null>(null);

  // The last completed sync we have rendered; a newer one reloads the list.
  const lastSyncRef = useRef<number | null>(null);
  const [syncGeneration, setSyncGeneration] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  const loadStats = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const next = await api.getLiveCompareStats(statsWindow, signal);
        if (signal?.aborted) return;
        setStats(next);
        setStatsError(null);
        setNow(Math.floor(Date.now() / 1000));
        lastSyncRef.current = next?.status.last_sync_completed_at ?? null;
      } catch (err) {
        if (isAbortError(err)) return;
        setStatsError(err instanceof Error ? err.message : 'Failed to load the comparison');
      }
    },
    [statsWindow]
  );

  // The comparison itself: on window change and whenever a sync completed.
  useEffect(() => {
    const controller = new AbortController();
    void loadStats(controller.signal);
    return () => controller.abort();
  }, [loadStats, syncGeneration]);

  // Follow the sync loop through the cheap status endpoint; a newer completed
  // sync bumps the generation, which reloads the stats and the list above.
  useEffect(() => {
    const controller = new AbortController();
    const poll = async () => {
      try {
        const next = await api.getLiveFeedStatus(controller.signal);
        if (controller.signal.aborted) return;
        setNow(Math.floor(Date.now() / 1000));
        setStats((prev) => (prev ? { ...prev, status: next } : prev));
        // Any change -- including the very first completed sync on a page that
        // loaded while the feature was off -- reloads the comparison.
        const completed = next.last_sync_completed_at ?? null;
        if (completed !== lastSyncRef.current) {
          lastSyncRef.current = completed;
          setSyncGeneration((g) => g + 1);
        }
      } catch (err) {
        if (isAbortError(err)) return;
        // A failed status poll is not worth an error banner; the next tick retries.
      }
    };
    const timer = setInterval(() => void poll(), STATUS_POLL_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, []);

  // The first page whenever a filter changes or a sync completes.
  useEffect(() => {
    const controller = new AbortController();
    setListLoading(true);
    setListError(null);
    api
      .getLiveCompareMessages(
        {
          window: statsWindow,
          channelKey: channelKey || null,
          source,
          q: debouncedQuery || undefined,
          limit: PAGE_SIZE,
          offset: 0,
        },
        controller.signal
      )
      .then((page) => {
        if (controller.signal.aborted) return;
        setMessages(page.messages);
        setTotal(page.total);
        setCounts(page.counts);
      })
      .catch((err) => {
        if (isAbortError(err)) return;
        setListError(err instanceof Error ? err.message : 'Failed to load messages');
      })
      .finally(() => {
        if (!controller.signal.aborted) setListLoading(false);
      });
    return () => controller.abort();
  }, [statsWindow, channelKey, source, debouncedQuery, syncGeneration]);

  const loadMore = useCallback(async () => {
    setListLoading(true);
    try {
      const page = await api.getLiveCompareMessages({
        window: statsWindow,
        channelKey: channelKey || null,
        source,
        q: debouncedQuery || undefined,
        limit: PAGE_SIZE,
        offset: messages.length,
      });
      setMessages((prev) => {
        const seen = new Set(prev.map((m) => m.key));
        return [...prev, ...page.messages.filter((m) => !seen.has(m.key))];
      });
      setTotal(page.total);
      setCounts(page.counts);
    } catch (err) {
      setListError(err instanceof Error ? err.message : 'Failed to load more messages');
    } finally {
      setListLoading(false);
    }
  }, [statsWindow, channelKey, source, debouncedQuery, messages.length]);

  const handleSyncNow = useCallback(async () => {
    setSyncing(true);
    try {
      const next = await api.syncLiveFeed();
      setStats((prev) => (prev ? { ...prev, status: next } : prev));
      // A long first walk answers while still syncing; the status poll picks
      // up its completion. A quick one is done: reload now.
      if (!next.syncing) {
        lastSyncRef.current = next.last_sync_completed_at ?? null;
        setSyncGeneration((g) => g + 1);
      }
    } catch (err) {
      setStatsError(err instanceof Error ? err.message : 'Sync failed');
    } finally {
      setSyncing(false);
    }
  }, []);

  const status = stats?.status ?? null;

  // Channels worth filtering on: the ones the comparison actually covers.
  const channelOptions = useMemo(() => {
    const byKey = new Map<string, string>();
    for (const c of stats?.channels ?? []) {
      if (c.channel_key) byKey.set(c.channel_key, c.channel_name);
    }
    for (const c of channels) {
      if (byKey.has(c.key.toUpperCase()) && c.name) byKey.set(c.key.toUpperCase(), c.name);
    }
    return [...byKey.entries()].map(([key, name]) => ({ key, name }));
  }, [stats, channels]);

  // The remote instance names only Public and hashtag channels; a private
  // channel's local name means nothing there, so the link falls back to Public.
  const selectedName = channelOptions.find((c) => c.key === channelKey)?.name;
  const selectedChannelName = selectedName?.startsWith('#') ? selectedName : 'Public';
  const liveUrl = status ? liveChannelUrl(status.url, selectedChannelName) : null;
  const host = status ? liveHostLabel(status.url) : 'live.meshcore.ca';
  const allCount = counts.both + counts.node_only + counts.live_only;
  const chipCount: Record<LiveCompareSource, number> = {
    both: counts.both,
    node: counts.node_only,
    live: counts.live_only,
  };

  const notConfigured = stats === null || (status !== null && !status.enabled);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="live-compare-view">
      <div className="shrink-0 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-base font-semibold">Live Compare</h2>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              Channel messages this node heard, merged with what the observers feeding{' '}
              {liveUrl ? (
                <a
                  href={liveUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-0.5 text-primary hover:underline"
                >
                  {host}
                  <ExternalLink className="h-3 w-3" aria-hidden="true" />
                </a>
              ) : (
                host
              )}{' '}
              heard
              {status?.region ? (
                <>
                  {' '}
                  in region <span className="font-medium text-foreground">{status.region}</span>
                </>
              ) : (
                ''
              )}
              . Each message appears once and says who heard it.
            </p>
          </div>
          <WindowSelector
            value={statsWindow}
            onChange={setStatsWindow}
            ariaLabel="Live compare window"
          />
        </div>
        {status && (
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span data-testid="live-compare-sync-status">{describeSync(status, now)}</span>
            {status.mirrored_messages > 0 && (
              <span>{status.mirrored_messages.toLocaleString()} mirrored</span>
            )}
            {status.unresolved_channels.length > 0 && (
              <span
                className="text-warning"
                title="These configured entries match no channel key on this node, so they are not compared"
              >
                Not compared (no key here): {status.unresolved_channels.join(', ')}
              </span>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs"
              onClick={() => void handleSyncNow()}
              disabled={syncing || status.syncing}
            >
              <RefreshCw
                className={cn('mr-1 h-3 w-3', (syncing || status.syncing) && 'animate-spin')}
                aria-hidden="true"
              />
              Sync now
            </Button>
            {onOpenSettings && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-xs"
                onClick={onOpenSettings}
              >
                <Settings2 className="mr-1 h-3 w-3" aria-hidden="true" />
                Configure
              </Button>
            )}
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[800px] space-y-4 p-4">
          {statsError && (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm">
              {statsError}
            </div>
          )}

          {stats === undefined && !statsError ? (
            <div className="py-8 text-center text-muted-foreground">Loading comparison…</div>
          ) : notConfigured && allCount === 0 ? (
            <div
              className="rounded-md border border-input bg-muted/20 p-4 text-sm"
              data-testid="live-compare-empty"
            >
              <p className="font-medium">The live feed comparison is off.</p>
              <p className="mt-1 text-muted-foreground">
                Turn it on to mirror the channel messages {host} observers hear and see, message by
                message, what this node caught and what it missed. You can restrict the feed to one
                CoreScope region.
              </p>
              {onOpenSettings && (
                <Button type="button" size="sm" className="mt-3" onClick={onOpenSettings}>
                  <Settings2 className="mr-1.5 h-4 w-4" aria-hidden="true" />
                  Open Live Compare settings
                </Button>
              )}
            </div>
          ) : (
            <>
              {stats && (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <StatTile value={stats.both} label={SOURCE_META.both.label} tone="text-success" />
                  <StatTile
                    value={stats.node_only}
                    label={SOURCE_META.node.label}
                    tone="text-info"
                  />
                  <StatTile
                    value={stats.live_only}
                    label={SOURCE_META.live.label}
                    tone="text-warning"
                  />
                  <StatTile
                    value={formatPercent(stats.node_coverage_pct)}
                    label="Node heard of live feed"
                  />
                </div>
              )}

              <div className="flex flex-wrap items-center gap-2">
                <div
                  role="group"
                  aria-label="Message source"
                  className="inline-flex flex-wrap gap-1 rounded-md border border-border p-0.5"
                >
                  <button
                    type="button"
                    aria-pressed={source === null}
                    onClick={() => setSource(null)}
                    className={cn(
                      'rounded px-2 py-1 text-xs font-medium transition-colors',
                      source === null
                        ? 'bg-primary text-primary-foreground'
                        : 'text-muted-foreground hover:bg-muted'
                    )}
                  >
                    All <span className="tabular-nums opacity-70">{allCount}</span>
                  </button>
                  {SOURCE_ORDER.map((key) => (
                    <button
                      key={key}
                      type="button"
                      aria-pressed={source === key}
                      title={SOURCE_META[key].description}
                      onClick={() => setSource(source === key ? null : key)}
                      className={cn(
                        'rounded px-2 py-1 text-xs font-medium transition-colors',
                        source === key
                          ? 'bg-primary text-primary-foreground'
                          : cn('hover:bg-muted', SOURCE_META[key].text)
                      )}
                    >
                      {SOURCE_META[key].label}{' '}
                      <span className="tabular-nums opacity-70">{chipCount[key]}</span>
                    </button>
                  ))}
                </div>
                {channelOptions.length > 1 && (
                  <select
                    aria-label="Channel"
                    value={channelKey}
                    onChange={(e) => setChannelKey(e.target.value)}
                    className="h-8 rounded-md border border-input bg-background px-2 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
                  >
                    <option value="">All channels</option>
                    {channelOptions.map((c) => (
                      <option key={c.key} value={c.key}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                )}
                <Input
                  type="search"
                  placeholder="Filter text…"
                  aria-label="Filter messages"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  className="h-8 w-full text-xs sm:w-48"
                />
              </div>

              {listError && (
                <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm">
                  {listError}
                </div>
              )}

              {messages.length === 0 && !listLoading ? (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  {allCount === 0
                    ? `Nothing to compare in ${windowPhrase(statsWindow)} yet.`
                    : 'No messages match these filters.'}
                </div>
              ) : (
                <ul className="divide-y divide-border/60" data-testid="live-compare-list">
                  {messages.map((message) => (
                    <MessageRow
                      key={message.key}
                      message={message}
                      showChannel={!channelKey && channelOptions.length > 1}
                      onInspect={setInspecting}
                    />
                  ))}
                </ul>
              )}

              {(messages.length < total || listLoading) && (
                <div className="flex justify-center pb-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={listLoading}
                    onClick={() => void loadMore()}
                  >
                    {listLoading
                      ? 'Loading…'
                      : `Load more (${(total - messages.length).toLocaleString()} left)`}
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
      {inspecting && (
        <Suspense fallback={null}>
          <LiveCompareTraceDialog message={inspecting} open onClose={() => setInspecting(null)} />
        </Suspense>
      )}
    </div>
  );
}
