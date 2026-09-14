import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LiveCompareView } from '../components/liveCompare/LiveCompareView';
import type {
  LiveCompareMessage,
  LiveCompareMessagesResponse,
  LiveCompareStats,
  LiveFeedStatus,
} from '../types';

const status: LiveFeedStatus = {
  enabled: true,
  url: 'https://live.meshcore.ca',
  region: 'YUL',
  channels: ['Public'],
  poll_interval: 300,
  syncing: false,
  last_sync_started_at: 1_700_000_000,
  last_sync_completed_at: 1_700_000_010,
  last_success_at: 1_700_000_010,
  last_error: null,
  last_warning: null,
  last_fetched: 3,
  last_changed: 0,
  last_sync_full: false,
  mirrored_messages: 2,
  unresolved_channels: [],
  recent_log: [],
  source: 'packets',
};

const stats: LiveCompareStats = {
  status,
  both: 1,
  node_only: 1,
  live_only: 1,
  node_coverage_pct: 50,
  live_coverage_pct: 50,
  channels: [
    { channel_key: 'AA'.repeat(16), channel_name: 'Public', both: 1, node_only: 1, live_only: 1 },
  ],
  bucket_seconds: 900,
  over_time: [],
};

function message(overrides: Partial<LiveCompareMessage>): LiveCompareMessage {
  return {
    key: 'k',
    source: 'both',
    channel_key: 'AA'.repeat(16),
    channel_name: 'Public',
    sender: 'Alice',
    text: 'Alice: hello',
    sender_timestamp: 1_700_000_000,
    seen_at: 1_700_000_001,
    outgoing: false,
    message_id: 1,
    node_received_at: 1_700_000_001,
    node_path_count: 1,
    live_first_seen: 1_700_000_003,
    live_last_seen: 1_700_000_003,
    live_repeats: 2,
    live_observers: ['obs-a', 'obs-b'],
    live_hops: 1,
    live_snr: 7.5,
    ...overrides,
  };
}

const page: LiveCompareMessagesResponse = {
  window: '1d',
  total: 3,
  counts: { both: 1, node_only: 1, live_only: 1 },
  messages: [
    message({ key: 'm1', source: 'both', text: 'Alice: seen by both', sender: 'Alice' }),
    message({
      key: 'h2',
      source: 'live',
      text: 'Bob: only the mesh heard this',
      sender: 'Bob',
      message_id: null,
      node_received_at: null,
      node_path_count: null,
    }),
    message({
      key: 'm3',
      source: 'node',
      text: 'Carol: only we heard this',
      sender: 'Carol',
      outgoing: true,
      live_first_seen: null,
      live_last_seen: null,
      live_repeats: null,
      live_observers: [],
      live_hops: null,
      live_snr: null,
    }),
  ],
};

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function mockApi(statsBody: LiveCompareStats | null, pageBody: LiveCompareMessagesResponse) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input);
    if (url.includes('/live-feed/stats')) return jsonResponse(statsBody);
    if (url.includes('/live-feed/messages')) return jsonResponse(pageBody);
    return new Response('not found', { status: 404 });
  });
}

describe('LiveCompareView', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders every message once with its source badge and the summary tiles', async () => {
    const fetchSpy = mockApi(stats, page);

    render(<LiveCompareView channels={[]} />);

    await waitFor(() => {
      expect(screen.getAllByTestId('live-compare-row')).toHaveLength(3);
    });
    const rows = screen.getAllByTestId('live-compare-row');
    expect(rows.map((row) => row.getAttribute('data-source'))).toEqual(['both', 'live', 'node']);
    expect(within(rows[0]).getByText('Both')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Live')).toBeInTheDocument();
    expect(within(rows[2]).getByText('Node')).toBeInTheDocument();
    expect(within(rows[2]).getByText('sent')).toBeInTheDocument();
    expect(within(rows[0]).getByText(/2 observers/)).toBeInTheDocument();

    expect(screen.getByText('Node heard of live feed')).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByTestId('live-compare-sync-status')).toHaveTextContent(/Synced/);
    expect(screen.getByRole('link', { name: /live.meshcore.ca/ })).toHaveAttribute(
      'href',
      'https://live.meshcore.ca/#/channels/Public'
    );

    const urls = fetchSpy.mock.calls.map((call) => String(call[0]));
    expect(urls).toContain('./api/live-feed/stats?window=1d');
    expect(urls.some((u) => u.startsWith('./api/live-feed/messages?window=1d'))).toBe(true);
  });

  it('narrows the list by source and forwards the filter to the API', async () => {
    const fetchSpy = mockApi(stats, page);

    render(<LiveCompareView channels={[]} />);
    await waitFor(() => {
      expect(screen.getAllByTestId('live-compare-row')).toHaveLength(3);
    });

    await userEvent.click(screen.getByRole('button', { name: /Live only/ }));

    await waitFor(() => {
      const urls = fetchSpy.mock.calls.map((call) => String(call[0]));
      expect(urls.some((u) => u.includes('/live-feed/messages') && u.includes('source=live'))).toBe(
        true
      );
    });
  });

  it('explains how to enable the feature when nothing is configured', async () => {
    mockApi(null, {
      window: '1d',
      total: 0,
      counts: { both: 0, node_only: 0, live_only: 0 },
      messages: [],
    });
    const onOpenSettings = vi.fn();

    render(<LiveCompareView channels={[]} onOpenSettings={onOpenSettings} />);

    await waitFor(() => {
      expect(screen.getByTestId('live-compare-empty')).toBeInTheDocument();
    });
    await userEvent.click(screen.getByRole('button', { name: /Open Live Compare settings/ }));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it('flags a degraded sync next to the synced time', async () => {
    mockApi(
      {
        ...stats,
        status: { ...status, last_warning: 'Packet feed unavailable (HTTP 404); compared Public' },
      },
      page
    );

    render(<LiveCompareView channels={[]} />);

    await waitFor(() => {
      expect(screen.getByTestId('live-compare-sync-status')).toHaveTextContent(/Synced/);
    });
    expect(screen.getByTestId('live-compare-sync-status')).toHaveTextContent(
      /degraded: Packet feed/
    );
  });

  it('surfaces a failed sync in the status line', async () => {
    mockApi(
      { ...stats, status: { ...status, last_error: 'HTTP 503', last_success_at: null } },
      page
    );

    render(<LiveCompareView channels={[]} />);

    await waitFor(() => {
      expect(screen.getByTestId('live-compare-sync-status')).toHaveTextContent(/HTTP 503/);
    });
  });
});
