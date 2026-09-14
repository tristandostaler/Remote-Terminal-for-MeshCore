/**
 * Settings › Live Compare: which CoreScope instance to mirror, for which
 * channels, restricted to which of its regions, and how often.
 *
 * Regions are fetched from the instance itself (`/api/config/regions`) so the
 * picker offers the codes that exist there; the text field stays as a fallback
 * for typing several codes or when the list cannot be loaded.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { ExternalLink, RefreshCw } from 'lucide-react';

import { cn } from '@/lib/utils';
import { api } from '../../api';
import type {
  AppSettings,
  AppSettingsUpdate,
  Channel,
  LiveFeedRegion,
  LiveFeedStatus,
} from '../../types';
import {
  DEFAULT_LIVE_FEED_URL,
  MAX_LIVE_FEED_POLL_INTERVAL,
  MIN_LIVE_FEED_POLL_INTERVAL,
} from '../../types';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Label } from '../ui/label';
import { Switch } from '../ui/switch';
import { describeSync, liveChannelUrl, liveHostLabel } from '../liveCompare/liveCompareShared';

const POLL_INTERVAL_OPTIONS: { value: number; label: string }[] = [
  { value: 60, label: 'Every minute' },
  { value: 300, label: 'Every 5 minutes' },
  { value: 900, label: 'Every 15 minutes' },
  { value: 1800, label: 'Every 30 minutes' },
  { value: 3600, label: 'Every hour' },
  { value: 21600, label: 'Every 6 hours' },
];

const CUSTOM_REGION = '__custom__';

const selectClass =
  'h-9 rounded-md border border-input bg-background px-3 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2';

function parseChannelList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseRegionList(text: string): string {
  return text
    .split(',')
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean)
    .join(',');
}

function blurOnEnter(e: React.KeyboardEvent<HTMLInputElement>) {
  if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
}

export function SettingsLiveFeedSection({
  appSettings,
  channels,
  onSaveAppSettings,
  className,
}: {
  appSettings: AppSettings;
  channels: Channel[];
  onSaveAppSettings: (update: AppSettingsUpdate) => Promise<void>;
  className?: string;
}) {
  const [url, setUrl] = useState(appSettings.live_feed_url);
  const [channelsText, setChannelsText] = useState(appSettings.live_feed_channels.join(', '));
  const [regionText, setRegionText] = useState(appSettings.live_feed_region);
  const [regions, setRegions] = useState<LiveFeedRegion[] | null>(null);
  const [regionsError, setRegionsError] = useState<string | null>(null);
  const [regionsLoading, setRegionsLoading] = useState(false);
  const [status, setStatus] = useState<LiveFeedStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));

  // Keep the drafts in step with saved settings (another tab, a reset, ...).
  useEffect(() => setUrl(appSettings.live_feed_url), [appSettings.live_feed_url]);
  useEffect(
    () => setChannelsText(appSettings.live_feed_channels.join(', ')),
    [appSettings.live_feed_channels]
  );
  useEffect(() => setRegionText(appSettings.live_feed_region), [appSettings.live_feed_region]);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.getLiveFeedStatus());
      setNow(Math.floor(Date.now() / 1000));
    } catch {
      // The status line is a convenience; the settings themselves still work.
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    const timer = window.setInterval(() => void refreshStatus(), 15_000);
    return () => window.clearInterval(timer);
  }, [refreshStatus]);

  const loadRegions = useCallback(async () => {
    setRegionsLoading(true);
    setRegionsError(null);
    try {
      setRegions((await api.getLiveFeedRegions()).regions);
    } catch (err) {
      setRegions(null);
      setRegionsError(err instanceof Error ? err.message : 'Could not load regions');
    } finally {
      setRegionsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRegions();
  }, [loadRegions, appSettings.live_feed_url]);

  const save = useCallback(
    async (label: string, update: AppSettingsUpdate) => {
      setBusy(label);
      setError(null);
      try {
        await onSaveAppSettings(update);
        await refreshStatus();
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to save');
      } finally {
        setBusy(null);
      }
    },
    [onSaveAppSettings, refreshStatus]
  );

  const commitUrl = () => {
    const next = url.trim().replace(/\/+$/, '') || DEFAULT_LIVE_FEED_URL;
    setUrl(next);
    if (next !== appSettings.live_feed_url) void save('url', { live_feed_url: next });
  };

  const commitChannels = () => {
    const next = parseChannelList(channelsText);
    const current = appSettings.live_feed_channels;
    const same = next.length === current.length && next.every((name, i) => name === current[i]);
    if (!same) void save('channels', { live_feed_channels: next.length ? next : ['Public'] });
  };

  const commitRegion = (value: string) => {
    const next = parseRegionList(value);
    setRegionText(next);
    if (next !== appSettings.live_feed_region) void save('region', { live_feed_region: next });
  };

  const handleSyncNow = async () => {
    setBusy('sync');
    setError(null);
    try {
      setStatus(await api.syncLiveFeed());
      setNow(Math.floor(Date.now() / 1000));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sync failed');
    } finally {
      setBusy(null);
    }
  };

  // Suggest the channels this node already carries; Public is always first.
  const channelSuggestions = useMemo(() => {
    const names = new Set<string>(['Public']);
    for (const c of channels) if (c.name) names.add(c.name);
    return [...names];
  }, [channels]);

  const selectedRegionCodes = appSettings.live_feed_region
    ? appSettings.live_feed_region.split(',')
    : [];
  let regionSelectValue = CUSTOM_REGION;
  if (selectedRegionCodes.length === 0) regionSelectValue = '';
  else if (
    selectedRegionCodes.length === 1 &&
    regions?.some((r) => r.code === selectedRegionCodes[0])
  ) {
    regionSelectValue = selectedRegionCodes[0];
  }
  const host = liveHostLabel(appSettings.live_feed_url);
  const firstChannel = appSettings.live_feed_channels[0] ?? 'Public';
  const syncBusy = busy === 'sync' || status?.syncing === true;

  return (
    <div className={cn('space-y-6', className)} data-testid="live-feed-section">
      <div>
        <h3 className="text-lg font-semibold">Live Compare</h3>
        <p className="text-sm text-muted-foreground">
          Mirror the channel messages a public CoreScope instance such as{' '}
          <a
            href={liveChannelUrl(DEFAULT_LIVE_FEED_URL, 'Public')}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-0.5 text-primary hover:underline"
          >
            live.meshcore.ca <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </a>{' '}
          decrypts from its observers, and compare them with what this node heard. The Statistics
          page gains a coverage section and the Live Compare tool lists every message once, marked
          as heard by both, by this node only, or by the live feed only.
        </p>
      </div>

      {error ? (
        <div className="rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm">
          {error}
        </div>
      ) : null}

      <div className="flex items-center justify-between gap-4 rounded-md border border-input p-4">
        <div className="space-y-0.5">
          <Label htmlFor="live-feed-enabled" className="text-sm font-medium">
            Compare with the live feed
          </Label>
          <p className="text-[0.8125rem] text-muted-foreground">
            Polls {host} in the background. Off by default; nothing is sent, only read.
          </p>
        </div>
        <Switch
          id="live-feed-enabled"
          checked={appSettings.live_feed_enabled}
          disabled={busy === 'enabled'}
          onCheckedChange={(checked) => void save('enabled', { live_feed_enabled: checked })}
        />
      </div>

      <div className="space-y-4 rounded-md border border-input p-4">
        <div className="space-y-1.5">
          <Label htmlFor="live-feed-url">CoreScope instance</Label>
          <Input
            id="live-feed-url"
            type="url"
            value={url}
            placeholder={DEFAULT_LIVE_FEED_URL}
            onChange={(e) => setUrl(e.target.value)}
            onBlur={commitUrl}
            onKeyDown={blurOnEnter}
            className="h-9 text-sm"
          />
          <p className="text-[0.8125rem] text-muted-foreground">
            Any CoreScope deployment works; only its public read API is used.
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="live-feed-region">Region on {host}</Label>
          <div className="flex flex-wrap items-center gap-2">
            <select
              id="live-feed-region"
              aria-label="Live feed region"
              className={selectClass}
              value={regionSelectValue}
              disabled={regionsLoading || busy === 'region'}
              onChange={(e) => {
                if (e.target.value !== CUSTOM_REGION) commitRegion(e.target.value);
              }}
            >
              <option value="">All regions</option>
              {(regions ?? []).map((region) => (
                <option key={region.code} value={region.code}>
                  {region.label} ({region.code})
                </option>
              ))}
              {regionSelectValue === CUSTOM_REGION && (
                <option value={CUSTOM_REGION}>Custom: {appSettings.live_feed_region}</option>
              )}
            </select>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-9 px-2"
              onClick={() => void loadRegions()}
              disabled={regionsLoading}
              title="Reload the region list from the instance"
            >
              <RefreshCw
                className={cn('h-4 w-4', regionsLoading && 'animate-spin')}
                aria-hidden="true"
              />
            </Button>
            <Input
              aria-label="Region codes"
              value={regionText}
              placeholder="e.g. YUL or YUL,YQB"
              onChange={(e) => setRegionText(e.target.value)}
              onBlur={() => commitRegion(regionText)}
              onKeyDown={blurOnEnter}
              className="h-9 w-40 text-sm"
            />
          </div>
          <p className="text-[0.8125rem] text-muted-foreground">
            CoreScope groups its observers by airport code. Pick one to compare against the mesh
            near you, or type several separated by commas. Unrelated to MeshCore flood-scope
            regions.
            {regionsError ? (
              <span className="block text-warning">Region list unavailable: {regionsError}</span>
            ) : null}
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="live-feed-channels">Channels to compare</Label>
          <Input
            id="live-feed-channels"
            list="live-feed-channel-suggestions"
            value={channelsText}
            placeholder="Public, #montreal"
            onChange={(e) => setChannelsText(e.target.value)}
            onBlur={commitChannels}
            onKeyDown={blurOnEnter}
            className="h-9 text-sm"
          />
          <datalist id="live-feed-channel-suggestions">
            {channelSuggestions.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
          <p className="text-[0.8125rem] text-muted-foreground">
            Comma-separated, named as the instance names them. The instance must hold a channel's
            key to decrypt it; Public and community hashtag channels usually qualify. This node
            needs the key too for a message to count as heard by both.
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="live-feed-interval">Poll interval</Label>
          <select
            id="live-feed-interval"
            className={cn(selectClass, 'block')}
            value={appSettings.live_feed_poll_interval}
            disabled={busy === 'interval'}
            onChange={(e) =>
              void save('interval', {
                live_feed_poll_interval: Math.min(
                  MAX_LIVE_FEED_POLL_INTERVAL,
                  Math.max(MIN_LIVE_FEED_POLL_INTERVAL, Number(e.target.value))
                ),
              })
            }
          >
            {POLL_INTERVAL_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
            {!POLL_INTERVAL_OPTIONS.some(
              (o) => o.value === appSettings.live_feed_poll_interval
            ) && (
              <option value={appSettings.live_feed_poll_interval}>
                Every {appSettings.live_feed_poll_interval} s
              </option>
            )}
          </select>
        </div>
      </div>

      <div
        className="space-y-2 rounded-md border border-input p-4 text-sm"
        data-testid="live-feed-status"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <span className="text-muted-foreground">Status:</span>{' '}
            {status ? describeSync(status, now) : 'Loading…'}
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void handleSyncNow()}
            disabled={syncBusy}
          >
            <RefreshCw className={cn('mr-1.5 h-4 w-4', syncBusy && 'animate-spin')} aria-hidden />
            Sync now
          </Button>
        </div>
        {status ? (
          <>
            <div>
              <span className="text-muted-foreground">Mirrored messages:</span>{' '}
              {status.mirrored_messages.toLocaleString()}
              {status.last_fetched ? ` · ${status.last_fetched} checked on the last sync` : ''}
            </div>
            {status.unresolved_channels.length > 0 && (
              <div className="text-warning">
                This node has no key for {status.unresolved_channels.join(', ')}; those channels can
                only ever show as live-only.
              </div>
            )}
            <a
              href={liveChannelUrl(appSettings.live_feed_url, firstChannel)}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              Open {firstChannel} on {host} <ExternalLink className="h-3 w-3" aria-hidden="true" />
            </a>
          </>
        ) : null}
      </div>
    </div>
  );
}
