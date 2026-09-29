import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RegionToolsPanel } from '../components/settings/SettingsRegionToolsPanel';

const okJson = (body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));

describe('RegionToolsPanel', () => {
  afterEach(() => vi.restoreAllMocks());

  it('lists guessed regions and adds them to known regions', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
      okJson({
        scoped_packets: 12,
        tested_packets: 10,
        candidates_tried: 22284,
        timed_out: false,
        results: [{ region: 'yul', hits: 7, pct_of_tested: 70 }],
      })
    );
    const onAdd = vi.fn();
    render(<RegionToolsPanel onAddRegions={onAdd} />);

    await userEvent.click(screen.getByRole('button', { name: 'Guess Regions' }));
    await waitFor(() => expect(screen.getByText('yul')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Add to Known Regions' }));

    expect(onAdd).toHaveBeenCalledWith(['yul']);
  });

  it('imports names from a URL and verifies them against stored packets', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith('/settings/regions/import')) {
        return okJson({ url: 'https://example.com/r', names: ['onqc', 'qc'], already_known: [] });
      }
      return okJson({
        scoped_packets: 4,
        tested_packets: 4,
        candidates_tried: 3,
        timed_out: false,
        results: [{ region: 'qc', hits: 4, pct_of_tested: 100 }],
      });
    });
    render(<RegionToolsPanel onAddRegions={vi.fn()} />);

    await userEvent.type(screen.getByLabelText('Region list URL'), 'https://example.com/r');
    await userEvent.click(screen.getByRole('button', { name: 'Fetch' }));
    await waitFor(() => expect(screen.getByText(/onqc\s+qc/)).toBeInTheDocument());
    await userEvent.click(screen.getByRole('button', { name: 'Check against packets' }));

    await waitFor(() => {
      const guessCall = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/regions/guess'));
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
