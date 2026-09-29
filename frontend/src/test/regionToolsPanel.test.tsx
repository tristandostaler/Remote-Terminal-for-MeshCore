import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RegionToolsPanel } from '../components/settings/SettingsRegionToolsPanel';

const okJson = (body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));

const job = (overrides: Record<string, unknown> = {}) => ({
  job_id: 'job1',
  status: 'completed',
  error: null,
  elapsed_seconds: 3,
  max_seconds: 60,
  scoped_packets: 12,
  tested_packets: 10,
  candidates_tried: 18_252,
  candidates_total: 18_252,
  results: [{ region: 'yul', hits: 7, pct_of_tested: 70 }],
  ...overrides,
});

describe('RegionToolsPanel', () => {
  afterEach(() => vi.restoreAllMocks());

  it('starts a background brute force, polls it, and adds the results', async () => {
    let polls = 0;
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => {
      if ((init as RequestInit | undefined)?.method === 'POST') {
        return okJson(job({ status: 'running', candidates_tried: 0, results: [] }));
      }
      polls += 1;
      // First GET is the on-mount resume check (nothing running), then progress polls.
      if (polls === 1) return okJson(null);
      return okJson(job());
    });
    const onAdd = vi.fn();
    render(<RegionToolsPanel onAddRegions={onAdd} />);

    await userEvent.clear(screen.getByLabelText(/Time limit/));
    await userEvent.type(screen.getByLabelText(/Time limit/), '0');
    await userEvent.click(screen.getByRole('button', { name: 'Brute Force Regions' }));
    await waitFor(() => expect(screen.getByText('yul')).toBeInTheDocument(), { timeout: 4000 });
    await userEvent.click(screen.getByRole('button', { name: 'Add to Known Regions' }));

    expect(onAdd).toHaveBeenCalledWith(['yul']);
    const start = fetchMock.mock.calls.find(([, i]) => (i as RequestInit)?.method === 'POST');
    expect(JSON.parse(String((start?.[1] as RequestInit).body))).toMatchObject({
      min_letters: 2,
      max_letters: 3,
      max_seconds: 0,
    });
  });

  it('resumes a sweep that is already running when the panel opens', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      okJson(job({ status: 'running', candidates_tried: 500, results: [] }))
    );
    render(<RegionToolsPanel onAddRegions={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument());
  });

  it('imports names from a URL and verifies them against stored packets', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith('/settings/regions/import')) {
        return okJson({ url: 'https://example.com/r', names: ['onqc', 'qc'], already_known: [] });
      }
      return okJson(job({ results: [{ region: 'qc', hits: 4, pct_of_tested: 100 }] }));
    });
    render(<RegionToolsPanel onAddRegions={vi.fn()} />);

    await userEvent.type(screen.getByLabelText('Region list URL'), 'https://example.com/r');
    await userEvent.click(screen.getByRole('button', { name: 'Fetch' }));
    await waitFor(() => expect(screen.getByText(/onqc\s+qc/)).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Check against packets' }));

    await waitFor(() => {
      const guessCall = fetchMock.mock.calls.find(
        ([u, i]) => String(u).endsWith('/regions/guess') && (i as RequestInit)?.method === 'POST'
      );
      expect(JSON.parse(String((guessCall?.[1] as RequestInit).body)).candidates).toEqual([
        'onqc',
        'qc',
      ]);
    });
  });

  it('loads region codes from the live feed and previews them', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      okJson({
        url: 'https://live.meshcore.ca',
        regions: [
          { code: 'YUL', label: 'Montreal' },
          { code: 'YYZ', label: 'Toronto' },
          { code: 'yul', label: 'Montreal again' },
        ],
      })
    );
    render(<RegionToolsPanel onAddRegions={vi.fn()} />);

    await userEvent.click(screen.getByRole('button', { name: 'Load from live feed' }));

    await waitFor(() => expect(screen.getByText(/yul\s+yyz/)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Check against packets' })).toBeInTheDocument();
  });
});
