import { render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LiveCompareTraceDialog } from '../components/liveCompare/LiveCompareTraceDialog';
import { buildMapLayers } from '../components/liveCompare/LiveTraceMap';
import { readTrace } from '../components/liveCompare/liveTraceShared';
import type { LiveCompareMessage, LiveCompareTrace, LiveTraceNode } from '../types';

vi.mock('react-leaflet', () => ({
  MapContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="map-container">{children}</div>
  ),
  TileLayer: () => null,
  Marker: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="map-marker">{children}</div>
  ),
  Polyline: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="map-line">{children}</div>
  ),
  Tooltip: ({ children }: { children: React.ReactNode }) => <span>{children}</span>,
  useMap: () => ({ setView: vi.fn(), fitBounds: vi.fn() }),
}));

function node(overrides: Partial<LiveTraceNode>): LiveTraceNode {
  return {
    public_key: null,
    name: null,
    lat: null,
    lon: null,
    known_locally: false,
    direct_neighbour: false,
    ...overrides,
  };
}

const self = node({ public_key: 'ee'.repeat(32), name: 'My radio', lat: 45.5, lon: -73.6 });
const alpha = node({
  public_key: 'aa'.repeat(32),
  name: 'Alpha',
  lat: 45.51,
  lon: -73.61,
  known_locally: true,
  direct_neighbour: true,
});
const delta = node({ public_key: 'dd'.repeat(32), name: 'Delta', lat: 45.9, lon: -74.1 });
const observer = node({ public_key: '0b'.repeat(32), name: 'Obs One', lat: 45.52, lon: -73.58 });

const HASH = 'a1b2c3d4e5f60718';

const liveOnly: LiveCompareMessage = {
  key: `h${HASH}`,
  source: 'live',
  packet_hash: HASH,
  channel_key: 'AA'.repeat(16),
  channel_name: 'Public',
  sender: 'Bob',
  text: 'Bob: anyone copy?',
  sender_timestamp: 1_700_000_000,
  seen_at: 1_700_000_001,
  outgoing: false,
  message_id: null,
  node_received_at: null,
  node_path_count: null,
  live_first_seen: 1_700_000_001,
  live_last_seen: 1_700_000_002,
  live_repeats: 2,
  live_observers: ['Obs One'],
  live_hops: 2,
  live_snr: 8.5,
};

function trace(overrides: Partial<LiveCompareTrace>): LiveCompareTrace {
  return {
    packet_hash: HASH,
    live_url: `https://live.meshcore.ca/#/packets/${HASH}`,
    message_id: null,
    heard_by_node: false,
    outgoing: false,
    sender: null,
    self_node: self,
    live_first_seen: 1_700_000_001,
    live_last_seen: 1_700_000_002,
    live_repeats: 2,
    routes: [
      {
        kind: 'observer',
        receiver: observer,
        region: 'YUL',
        heard_at: 1_700_000_002,
        snr: 8.5,
        rssi: -101,
        hops: [
          { prefix: 'AA', node: alpha, ambiguous: false, candidates: [], identified_by: 'node' },
          {
            prefix: 'DD',
            node: delta,
            ambiguous: true,
            candidates: [delta, node({ public_key: 'dd01'.repeat(16), name: 'Delta Two' })],
            identified_by: 'live',
          },
          { prefix: 'FF', node: null, ambiguous: false, candidates: [], identified_by: null },
        ],
      },
    ],
    live_error: null,
    live_warning: null,
    fetched_at: 1_700_000_100,
    ...overrides,
  };
}

function mockTrace(body: LiveCompareTrace | { status: number; detail: string }) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input);
    if (!url.includes('/live-feed/trace')) return new Response('not found', { status: 404 });
    if ('status' in body && 'detail' in body) {
      return new Response(JSON.stringify({ detail: body.detail }), {
        status: body.status,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
}

describe('LiveCompareTraceDialog', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('asks for the row by hash, then lists every route hop by hop with a map', async () => {
    const fetchSpy = mockTrace(trace({}));
    render(<LiveCompareTraceDialog message={liveOnly} open onClose={() => {}} />);

    const summary = await screen.findByTestId('live-trace-summary');
    expect(fetchSpy.mock.calls[0][0]).toContain(`packet_hash=${HASH}`);
    expect(fetchSpy.mock.calls[0][0]).not.toContain('message_id');

    expect(summary).toHaveTextContent('Your node missed this message.');
    expect(summary).toHaveTextContent(
      '1 live feed observer heard it through 2 relays, 1 of them in your contacts'
    );
    // Alpha is a direct neighbour: the one fact that matters most is the hint.
    expect(summary).toHaveTextContent('Alpha, a direct neighbour of your node, repeated it.');

    const routes = screen.getAllByTestId('live-trace-route');
    expect(routes).toHaveLength(1);
    expect(routes[0]).toHaveAttribute('data-kind', 'observer');
    expect(routes[0]).toHaveTextContent('Obs One');
    expect(routes[0]).toHaveTextContent('YUL');
    expect(routes[0]).toHaveTextContent('SNR 8.5');
    expect(routes[0]).toHaveTextContent('RSSI -101');
    const hops = within(routes[0]).getAllByTestId('live-trace-hop');
    expect(hops.map((h) => h.textContent)).toEqual(['AAAlphaneighbour', 'DDDelta?', 'FFunknown']);
    expect(hops[0]).toHaveAttribute('aria-label', expect.stringContaining('from your contacts'));
    expect(hops[1]).toHaveAttribute(
      'aria-label',
      expect.stringContaining('could also be: Delta Two')
    );
    expect(hops[1]).toHaveAttribute('aria-label', expect.stringContaining('from your node'));
    expect(hops[2]).toHaveAttribute('aria-label', 'Hop FF: no known node matches this hash');

    // The map is lazy; wait for it. Four located nodes, one route line.
    await waitFor(() => expect(screen.getByTestId('live-trace-map')).toBeInTheDocument());
    expect(screen.getAllByTestId('map-marker')).toHaveLength(4);
    expect(screen.getAllByTestId('map-line')).toHaveLength(1);
    expect(screen.getByText(/1 hop without a known location skipped/)).toBeInTheDocument();

    expect(
      screen.getByRole('link', { name: /Open this packet on live.meshcore.ca/ })
    ).toHaveAttribute('href', `https://live.meshcore.ca/#/packets/${HASH}`);
  });

  it('sends the message id for a node-only row and reads its own routes', async () => {
    const fetchSpy = mockTrace(
      trace({
        packet_hash: null,
        live_url: null,
        message_id: 7,
        heard_by_node: true,
        routes: [
          {
            kind: 'node',
            receiver: self,
            region: null,
            heard_at: 1_700_000_004,
            snr: 6,
            rssi: -95,
            hops: [],
          },
        ],
      })
    );
    render(
      <LiveCompareTraceDialog
        message={{ ...liveOnly, key: 'm7', source: 'node', packet_hash: null, message_id: 7 }}
        open
        onClose={() => {}}
      />
    );

    const summary = await screen.findByTestId('live-trace-summary');
    expect(fetchSpy.mock.calls[0][0]).toContain('message_id=7');
    expect(fetchSpy.mock.calls[0][0]).not.toContain('packet_hash');
    expect(summary).toHaveTextContent('Your node heard this message directly.');
    expect(summary).toHaveTextContent('No live feed observer recorded it.');
    const route = screen.getByTestId('live-trace-route');
    expect(route).toHaveAttribute('data-kind', 'node');
    expect(route).toHaveTextContent('My radio (this node)');
    expect(route).toHaveTextContent('direct');
    expect(screen.queryByRole('link', { name: /Open this packet/ })).not.toBeInTheDocument();
  });

  it('shows a live feed failure without hiding the node side', async () => {
    mockTrace(
      trace({
        heard_by_node: true,
        message_id: 3,
        live_error: 'https://live.meshcore.ca/api/packets/x: HTTP 404',
        routes: [
          {
            kind: 'node',
            receiver: self,
            region: null,
            heard_at: 1_700_000_004,
            snr: null,
            rssi: null,
            hops: [
              {
                prefix: 'AA',
                node: alpha,
                ambiguous: false,
                candidates: [],
                identified_by: 'node',
              },
            ],
          },
        ],
      })
    );
    render(
      <LiveCompareTraceDialog message={{ ...liveOnly, source: 'both' }} open onClose={() => {}} />
    );

    const summary = await screen.findByTestId('live-trace-summary');
    expect(summary).toHaveTextContent('Your node heard this message.');
    expect(summary).toHaveTextContent(
      'Live feed: https://live.meshcore.ca/api/packets/x: HTTP 404'
    );
    expect(screen.getAllByTestId('live-trace-route')).toHaveLength(1);
  });

  it('reports a request failure', async () => {
    mockTrace({ status: 404, detail: 'Unknown message' });
    render(<LiveCompareTraceDialog message={liveOnly} open onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/Unknown message/)).toBeInTheDocument());
    expect(screen.queryByTestId('live-trace-summary')).not.toBeInTheDocument();
  });
});

describe('readTrace', () => {
  it('names the closest located relay when none is a neighbour', () => {
    const far = node({ public_key: 'ff'.repeat(32), name: 'Far', lat: 46.8, lon: -71.2 });
    const near = node({ public_key: 'cc'.repeat(32), name: 'Near', lat: 45.6, lon: -73.7 });
    const reading = readTrace(
      trace({
        routes: [
          {
            kind: 'observer',
            receiver: observer,
            region: 'YUL',
            heard_at: 1,
            snr: null,
            rssi: null,
            hops: [
              { prefix: 'FF', node: far, ambiguous: false, candidates: [], identified_by: 'live' },
              { prefix: 'CC', node: near, ambiguous: false, candidates: [], identified_by: 'live' },
            ],
          },
        ],
      }),
      'metric'
    );
    expect(reading.headline).toBe('Your node missed this message.');
    expect(reading.detail).toBe('1 live feed observer heard it through 2 relays');
    expect(reading.hint).toBe(
      'The closest located relay that carried it, Near, is 13.6km from your node. None of the relays are in your contacts.'
    );
  });

  it('says so when the observers heard the sender directly', () => {
    const reading = readTrace(
      trace({
        routes: [
          {
            kind: 'observer',
            receiver: observer,
            region: null,
            heard_at: 1,
            snr: null,
            rssi: null,
            hops: [],
          },
          {
            kind: 'observer',
            receiver: observer,
            region: null,
            heard_at: 2,
            snr: null,
            rssi: null,
            hops: [],
          },
        ],
      }),
      'metric'
    );
    expect(reading.detail).toBe('2 live feed observers heard it straight from the sender');
    expect(reading.hint).toBe('No relay was involved: every observer heard the sender directly.');
  });

  it('explains an empty trace by the live feed error', () => {
    const reading = readTrace(trace({ routes: [], live_error: 'boom' }), 'metric');
    expect(reading.detail).toBe(
      'The live feed instance could not be consulted for its observations.'
    );
    expect(reading.hint).toBeNull();
  });
});

describe('buildMapLayers', () => {
  it('draws one marker per node across routes and dashes lines with gaps', () => {
    const { points, lines } = buildMapLayers(
      trace({
        sender: node({ public_key: 'b0'.repeat(32), name: 'Bob', lat: 45.4, lon: -73.5 }),
        routes: [
          {
            kind: 'node',
            receiver: self,
            region: null,
            heard_at: 1,
            snr: null,
            rssi: null,
            hops: [
              {
                prefix: 'AA',
                node: alpha,
                ambiguous: false,
                candidates: [],
                identified_by: 'node',
              },
            ],
          },
          {
            kind: 'observer',
            receiver: observer,
            region: null,
            heard_at: 2,
            snr: null,
            rssi: null,
            hops: [
              {
                prefix: 'AA',
                node: alpha,
                ambiguous: false,
                candidates: [],
                identified_by: 'node',
              },
              { prefix: 'FF', node: null, ambiguous: false, candidates: [], identified_by: null },
            ],
          },
        ],
      })
    );
    // self, sender, Alpha (once), observer
    expect(points.map((p) => p.label)).toEqual(['me', 'S', '1', 'O']);
    const alphaPoint = points.find((p) => p.label === '1')!;
    expect(alphaPoint.roles).toEqual(['hop 1 of 1 towards My radio', 'hop 1 of 2 towards Obs One']);
    expect(lines).toHaveLength(2);
    expect(lines[0]).toMatchObject({ dashed: false, title: 'Heard by this node' });
    expect(lines[0].positions).toHaveLength(3);
    expect(lines[1]).toMatchObject({ dashed: true, title: 'Heard by Obs One' });
    expect(lines[1].positions).toHaveLength(3);
  });
});
