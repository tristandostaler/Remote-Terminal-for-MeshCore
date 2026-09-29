import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { OwnerOutreachView, outreachMessage } from '../components/OwnerOutreachView';
import { NodeStatsView } from '../components/nodeStats/NodeStatsView';
import type {
  ContactOwnerInfo,
  NodeStatsResponse,
  OwnerOutreachItem,
  OwnerOutreachResponse,
} from '../types';

const KEY = 'ab'.repeat(32);

function owner(overrides: Partial<ContactOwnerInfo> = {}): ContactOwnerInfo {
  return {
    public_key: KEY,
    firmware_owner_info: null,
    firmware_version: null,
    fetched_at: null,
    attempted_at: null,
    attempt_status: null,
    notes: '',
    notes_updated_at: null,
    notified_at: null,
    hints: [],
    ...overrides,
  };
}

function item(overrides: Partial<OwnerOutreachItem> = {}): OwnerOutreachItem {
  return {
    public_key: KEY,
    name: 'Hilltop',
    type: 2,
    issue: 'clock_drift',
    drift_seconds: 2700,
    severity: 'major',
    readings: 3,
    last_observed_at: 1_700_000_000,
    recently_notified: false,
    owner: owner({
      firmware_owner_info: 'Bob|VE2XYZ',
      hints: [{ kind: 'callsign', value: 'VE2XYZ', source: 'owner_info' }],
    }),
    ...overrides,
  };
}

function outreach(overrides: Partial<OwnerOutreachResponse> = {}): OwnerOutreachResponse {
  return {
    generated_at: 1_700_000_100,
    lookback_seconds: 7 * 86400,
    threshold_seconds: 300,
    min_readings: 2,
    nodes_measured: 12,
    median_drift_seconds: 3,
    server_clock_suspect: false,
    items: [item()],
    ...overrides,
  };
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function mockFetch(routes: Record<string, (init?: RequestInit) => unknown>) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input);
    for (const [fragment, handler] of Object.entries(routes)) {
      if (url.includes(fragment)) return jsonResponse(handler(init));
    }
    return new Response('not found', { status: 404 });
  });
}

describe('outreachMessage', () => {
  it('describes the drift and mentions clkreboot for a fast clock', () => {
    const text = outreachMessage(item(), 'Hilltop');
    expect(text).toContain('"Hilltop"');
    expect(text).toContain('45m ahead');
    expect(text).toContain('clkreboot');
  });

  it('has its own wording for a clock that was never set', () => {
    const text = outreachMessage(item({ issue: 'unset_clock', drift_seconds: -1.7e9 }), 'X');
    expect(text).toContain('never set');
    expect(text).not.toContain('clkreboot');
  });
});

describe('OwnerOutreachView', () => {
  afterEach(() => vi.restoreAllMocks());

  it('lists flagged nodes with their owner and opens the node', async () => {
    mockFetch({ '/contacts/owner-outreach': () => outreach() });
    const onOpenNodeStats = vi.fn();
    const onSelectConversation = vi.fn();

    render(
      <OwnerOutreachView
        contacts={[]}
        onOpenNodeStats={onOpenNodeStats}
        onSelectConversation={onSelectConversation}
      />
    );

    const card = await screen.findByTestId('owner-outreach-item');
    expect(within(card).getByText('Hilltop')).toBeInTheDocument();
    expect(within(card).getByRole('link', { name: /VE2XYZ/ })).toHaveAttribute(
      'href',
      'https://www.qrz.com/db/VE2XYZ'
    );

    await userEvent.click(within(card).getByRole('button', { name: 'Open repeater' }));
    expect(onSelectConversation).toHaveBeenCalledWith({
      type: 'contact',
      id: KEY,
      name: 'Hilltop',
    });

    await userEvent.click(within(card).getByRole('button', { name: 'Node stats' }));
    expect(onOpenNodeStats).toHaveBeenCalledWith(KEY, 'Hilltop');
  });

  it('warns when the whole mesh points at our own clock', async () => {
    mockFetch({
      '/contacts/owner-outreach': () =>
        outreach({ server_clock_suspect: true, median_drift_seconds: -1800 }),
    });
    render(<OwnerOutreachView contacts={[]} onSelectConversation={vi.fn()} />);

    expect(await screen.findByRole('alert')).toHaveTextContent("this server's clock");
  });

  it('shows an empty state', async () => {
    mockFetch({ '/contacts/owner-outreach': () => outreach({ items: [] }) });
    render(<OwnerOutreachView contacts={[]} onSelectConversation={vi.fn()} />);

    expect(await screen.findByText('Nobody to contact right now.')).toBeInTheDocument();
  });

  it('marks an owner contacted in place', async () => {
    const fetchSpy = mockFetch({
      '/contacts/owner-outreach': () => outreach(),
      [`/contacts/${KEY}/owner`]: () =>
        owner({ notified_at: 1_700_000_200, firmware_owner_info: 'Bob|VE2XYZ' }),
    });
    render(<OwnerOutreachView contacts={[]} onSelectConversation={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: 'Mark contacted' }));

    expect(await screen.findByRole('button', { name: 'Clear contacted mark' })).toBeInTheDocument();
    const patch = fetchSpy.mock.calls.find((call) => call[1]?.method === 'PATCH');
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({ notified: true });
  });
});

describe('NodeStatsView owner section', () => {
  afterEach(() => vi.restoreAllMocks());

  const stats: NodeStatsResponse = {
    public_key: KEY,
    name: 'Hilltop',
    type: 2,
    window: '1M',
    window_seconds: 30 * 86400,
    generated_at: 1_700_000_000,
    clock_drift: null,
    owner: owner({ firmware_owner_info: 'Bob', fetched_at: 1_700_000_000 }),
  };

  it('shows the owner, saves edited notes, and links back to the repeater', async () => {
    const fetchSpy = mockFetch({
      [`/contacts/${KEY}/stats`]: () => stats,
      [`/contacts/${KEY}/owner`]: (init) => ({
        ...stats.owner,
        notes: JSON.parse(String(init?.body)).notes,
        notes_updated_at: 1_700_000_300,
      }),
    });
    const onBack = vi.fn();

    render(<NodeStatsView publicKey={KEY} contacts={[]} onBack={onBack} />);

    expect(await screen.findByText('Bob')).toBeInTheDocument();

    const notes = screen.getByRole('textbox', { name: 'Notes about the owner' });
    await userEvent.type(notes, 'Met at the club');
    await userEvent.click(screen.getByRole('button', { name: 'Save notes' }));

    await waitFor(() => expect(screen.getByRole('button', { name: 'Save notes' })).toBeDisabled());
    const patch = fetchSpy.mock.calls.find((call) => call[1]?.method === 'PATCH');
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({ notes: 'Met at the club' });
    expect(notes).toHaveValue('Met at the club');

    await userEvent.click(screen.getByRole('button', { name: 'Open repeater' }));
    expect(onBack).toHaveBeenCalled();
  });
});

describe('Fetch owner info button', () => {
  afterEach(() => vi.restoreAllMocks());

  it('fetches from the outreach card and shows the answer in place', async () => {
    const fetchSpy = mockFetch({
      [`/contacts/${KEY}/owner/refresh`]: () =>
        owner({
          firmware_owner_info: 'Alice|VA2NEW',
          attempt_status: 'ok',
          fetched_at: 1_700_000_500,
          hints: [{ kind: 'callsign', value: 'VA2NEW', source: 'owner_info' }],
        }),
      '/contacts/owner-outreach': () => outreach(),
    });
    render(<OwnerOutreachView contacts={[]} onSelectConversation={vi.fn()} />);

    const card = await screen.findByTestId('owner-outreach-item');
    await userEvent.click(within(card).getByRole('button', { name: 'Fetch owner info' }));

    expect(await within(card).findByText(/VA2NEW/, { selector: 'p' })).toBeInTheDocument();
    const post = fetchSpy.mock.calls.find((call) => String(call[0]).includes('/owner/refresh'));
    expect(post?.[1]?.method).toBe('POST');
  });

  it('is not offered for chat nodes', async () => {
    mockFetch({ '/contacts/owner-outreach': () => outreach({ items: [item({ type: 1 })] }) });
    render(<OwnerOutreachView contacts={[]} onSelectConversation={vi.fn()} />);

    const card = await screen.findByTestId('owner-outreach-item');
    expect(within(card).queryByRole('button', { name: 'Fetch owner info' })).toBeNull();
  });

  it('fetches from the node stats owner section', async () => {
    mockFetch({
      [`/contacts/${KEY}/owner/refresh`]: () =>
        owner({ firmware_owner_info: 'Fresh owner', attempt_status: 'ok', fetched_at: 1 }),
      [`/contacts/${KEY}/stats`]: () => ({
        public_key: KEY,
        name: 'Hilltop',
        type: 2,
        window: '1M',
        window_seconds: 30 * 86400,
        generated_at: 1_700_000_000,
        clock_drift: null,
        owner: owner(),
      }),
    });
    render(<NodeStatsView publicKey={KEY} contacts={[]} />);

    await userEvent.click(await screen.findByRole('button', { name: 'Fetch owner info' }));

    expect(await screen.findByText('Fresh owner')).toBeInTheDocument();
  });
});
