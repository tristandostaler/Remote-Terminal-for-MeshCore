import { act, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { DecryptProgressPanel } from '../components/DecryptProgressPanel';
import {
  clearDecryptProgress,
  primeDecryptStatus,
  recordDecryptProgress,
  selectSweepForKey,
  useDecryptStatus,
} from '../stores/decryptProgressStore';
import { advanceLastMessageTime, initLastMessageTimes } from '../utils/conversationState';
import type { DecryptSweepProgress } from '../types';

function progress(overrides: Partial<DecryptSweepProgress> = {}): DecryptSweepProgress {
  return {
    job_id: 'job-1',
    kind: 'channels',
    label: 'All rooms (3 keys)',
    status: 'running',
    total: 10_000,
    processed: 2_500,
    decrypted: 0,
    target_count: 3,
    targets: [],
    started_at: 1700000000,
    finished_at: null,
    queued: 0,
    ...overrides,
  };
}

beforeEach(() => {
  clearDecryptProgress();
});

describe('DecryptProgressPanel', () => {
  it('shows how far through the stored packets the sweep is', () => {
    render(<DecryptProgressPanel progress={progress()} />);

    expect(screen.getByText('2,500 / 10,000')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '25');
    expect(screen.getByText('No messages recovered yet')).toBeInTheDocument();
  });

  it('names the conversations it recovered messages for', () => {
    render(
      <DecryptProgressPanel
        progress={progress({
          status: 'complete',
          processed: 10_000,
          decrypted: 7,
          finished_at: 1700000100,
          targets: [
            { kind: 'channel', key: 'AA', name: '#ops', decrypted: 5 },
            { kind: 'channel', key: 'BB', name: '#weather', decrypted: 2 },
          ],
        })}
      />
    );

    expect(screen.getByText('Recovered 7 messages')).toBeInTheDocument();
    expect(screen.getByText('#ops')).toBeInTheDocument();
    expect(screen.getByText('5 messages')).toBeInTheDocument();
    expect(screen.getByText('#weather')).toBeInTheDocument();
    expect(screen.getByText('2 messages')).toBeInTheDocument();
  });

  it('says plainly when the keys decrypted nothing', () => {
    render(
      <DecryptProgressPanel
        progress={progress({ status: 'complete', processed: 10_000, decrypted: 0 })}
      />
    );

    expect(screen.getByText('Nothing decrypted with these keys')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100');
  });

  it('drops the per-conversation breakdown in the narrow panes', () => {
    render(
      <DecryptProgressPanel
        compact
        progress={progress({
          status: 'complete',
          decrypted: 5,
          targets: [{ kind: 'channel', key: 'AA', name: '#ops', decrypted: 5 }],
        })}
      />
    );

    expect(screen.getByText('Recovered 5 messages')).toBeInTheDocument();
    expect(screen.queryByText('#ops')).not.toBeInTheDocument();
  });

  it('reports a sweep waiting its turn', () => {
    render(<DecryptProgressPanel progress={progress({ queued: 2 })} />);

    expect(screen.getByText(/2 sweeps queued/)).toBeInTheDocument();
  });
});

function StatusProbe() {
  const status = useDecryptStatus();
  return (
    <div>
      <span data-testid="active">{status.active ? status.active.job_id : 'none'}</span>
      <span data-testid="last">
        {status.last ? `${status.last.job_id}:${status.last.decrypted}` : 'none'}
      </span>
    </div>
  );
}

describe('decrypt progress store', () => {
  it('keeps a finished sweep on screen instead of blanking it', () => {
    render(<StatusProbe />);

    act(() => recordDecryptProgress(progress()));
    expect(screen.getByTestId('active')).toHaveTextContent('job-1');

    act(() =>
      recordDecryptProgress(progress({ status: 'complete', processed: 10_000, decrypted: 3 }))
    );
    expect(screen.getByTestId('active')).toHaveTextContent('none');
    expect(screen.getByTestId('last')).toHaveTextContent('job-1:3');
  });

  it('a late tick from a finished sweep does not resurrect it', () => {
    render(<StatusProbe />);

    act(() => recordDecryptProgress(progress({ status: 'complete', decrypted: 3 })));
    act(() => recordDecryptProgress(progress({ status: 'complete', decrypted: 3 })));

    expect(screen.getByTestId('active')).toHaveTextContent('none');
  });

  it('picks up a sweep that was already running when the client loaded', () => {
    render(<StatusProbe />);

    act(() => primeDecryptStatus({ active: progress({ job_id: 'older' }), last: null, queued: 1 }));

    expect(screen.getByTestId('active')).toHaveTextContent('older');
  });

  it('does not attribute a finished sweep to a conversation it never touched', () => {
    const status = {
      active: null,
      last: progress({
        status: 'complete',
        decrypted: 3,
        targets: [{ kind: 'channel' as const, key: 'AA', name: '#ops', decrypted: 3 }],
      }),
      queued: 0,
    };

    expect(selectSweepForKey(status, 'BB')).toBeUndefined();
    expect(selectSweepForKey(status, null)).toBeUndefined();
  });

  it('matches a finished sweep to the conversation it recovered, ignoring key case', () => {
    const status = {
      active: null,
      last: progress({
        status: 'complete',
        decrypted: 3,
        targets: [{ kind: 'channel' as const, key: 'AA', name: '#ops', decrypted: 3 }],
      }),
      queued: 0,
    };

    expect(selectSweepForKey(status, 'aa')).toBe(status.last);
  });

  it('shows a running room sweep in every room pane, since it may still find their messages', () => {
    const status = { active: progress(), last: null, queued: 0 };

    expect(selectSweepForKey(status, 'ZZ')).toBe(status.active);
  });

  it('keeps a running DM sweep out of room panes', () => {
    const status = {
      active: progress({
        kind: 'contact' as const,
        label: 'Alice',
        target_count: 1,
        targets: [{ kind: 'contact' as const, key: 'cc'.repeat(32), name: 'Alice', decrypted: 0 }],
      }),
      last: null,
      queued: 0,
    };

    expect(selectSweepForKey(status, 'AA')).toBeUndefined();
    expect(selectSweepForKey(status, 'cc'.repeat(32), 'contact')).toBe(status.active);
  });

  it('keeps a running room sweep out of a contact pane', () => {
    const status = { active: progress(), last: null, queued: 0 };

    expect(selectSweepForKey(status, 'cc'.repeat(32), 'contact')).toBeUndefined();
  });

  it('shows a DM sweep only in the contact it is for', () => {
    const status = {
      active: progress({
        kind: 'contact' as const,
        targets: [{ kind: 'contact' as const, key: 'cc'.repeat(32), name: 'Alice', decrypted: 0 }],
      }),
      last: null,
      queued: 0,
    };

    expect(selectSweepForKey(status, 'dd'.repeat(32), 'contact')).toBeUndefined();
  });
});

describe('sidebar ordering during a sweep', () => {
  it('does not drag a conversation back to the age of a recovered message', () => {
    initLastMessageTimes({ 'channel-AA': 1_700_000_000 });

    // A sweep replays a three-week-old packet over the live socket.
    const times = advanceLastMessageTime('channel-AA', 1_698_000_000);

    expect(times['channel-AA']).toBe(1_700_000_000);
  });

  it('still advances for genuinely newer traffic', () => {
    initLastMessageTimes({ 'channel-AA': 1_700_000_000 });

    expect(advanceLastMessageTime('channel-AA', 1_700_000_500)['channel-AA']).toBe(1_700_000_500);
  });
});
