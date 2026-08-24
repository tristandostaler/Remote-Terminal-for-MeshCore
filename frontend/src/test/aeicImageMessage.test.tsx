/**
 * The AEIC image bubble's decode polling.
 *
 * The bubble waits on a ~5 s server-side synthesis pass, so it polls the session
 * once a second for up to a minute. The list is virtualized, which makes the
 * lifetime of that loop the whole problem: bubbles unmount whenever they scroll
 * out of view, and an uncancelled loop kept firing a request a second per
 * off-screen image — then started a second loop when the bubble scrolled back
 * in. Every poll now carries a cancellation token owned by its effect.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MessageList } from '../components/MessageList';
import { api } from '../api';
import type { Message } from '../types';

vi.mock('../api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api')>();
  return {
    ...original,
    api: {
      ...original.api,
      getAeicSessionForMessage: vi.fn(),
      getAeicSession: vi.fn(),
      retryAeicDecode: vi.fn(),
      aeicContentUrl: vi.fn().mockReturnValue('/api/aeic/sessions/x/content'),
    },
  };
});

const mockApi = api as unknown as {
  getAeicSessionForMessage: ReturnType<typeof vi.fn>;
  getAeicSession: ReturnType<typeof vi.fn>;
};

/** A one-chunk `aei1:` image message: prefix, sid, idx 0, tot 1, meta, payload. */
function aeicMessage(): Message {
  return {
    id: 77,
    type: 'PRIV',
    conversation_key: 'aa'.repeat(32),
    // meta byte 32 = aspect 2 (4:3) | resolution 0 (512px) | rate 0 (ft32),
    // which is '0w' in base36. A rate above 1 is rejected by the parser.
    text: 'aei1ab01' + '0w' + 'QRSTUVWX',
    sender_timestamp: 1700000000,
    received_at: 1700000001,
    paths: null,
    txt_type: 0,
    signature: null,
    sender_key: null,
    outgoing: false,
    acked: 0,
    sender_name: null,
  };
}

function undecoded() {
  return {
    session_key: 'aa'.repeat(6) + ':0371',
    message_id: 77,
    state: 'complete',
    square_size: 512,
    aspect_code: 2,
    rate_code: 0,
    total_chunks: 1,
    received_chunks: 1,
    missing_indices: [],
    bitstream_bytes: 156,
    decoded: false,
    decode_error: null,
  };
}

describe('AEIC image bubble polling', () => {
  beforeEach(() => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: vi.fn(),
      writable: true,
    });
    mockApi.getAeicSessionForMessage.mockReset().mockResolvedValue(undecoded());
    mockApi.getAeicSession.mockReset().mockResolvedValue(undecoded());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('stops polling once the bubble unmounts', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const view = render(<MessageList messages={[aeicMessage()]} contacts={[]} loading={false} />);

    await waitFor(() => expect(mockApi.getAeicSessionForMessage).toHaveBeenCalledWith(77));

    // Two turns of the poll loop, so it is demonstrably running.
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(1000);
    const whileMounted = mockApi.getAeicSession.mock.calls.length;
    expect(whileMounted).toBeGreaterThan(0);

    view.unmount();

    // Well past several more turns. Without the cancellation token the loop ran
    // on for the rest of its 60 attempts against an unmounted component.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(mockApi.getAeicSession.mock.calls.length).toBe(whileMounted);
  });

  it('renders the picture once the session reports it decoded', async () => {
    mockApi.getAeicSessionForMessage.mockResolvedValue({ ...undecoded(), decoded: true });
    render(<MessageList messages={[aeicMessage()]} contacts={[]} loading={false} />);
    expect(await screen.findByAltText('AI-reconstructed image message')).toBeVisible();
    // Already decoded, so the poll loop never runs.
    expect(mockApi.getAeicSession).not.toHaveBeenCalled();
  });

  it('surfaces a stored decode reason immediately instead of polling for a minute', async () => {
    // What a server without onnxruntime records on the session: the bubble must
    // say so at once rather than spending 60 s discovering nothing changed.
    mockApi.getAeicSessionForMessage.mockResolvedValue({
      ...undecoded(),
      decode_error: 'The AI image codec needs the optional onnxruntime dependency.',
    });
    render(<MessageList messages={[aeicMessage()]} contacts={[]} loading={false} />);
    expect(await screen.findByText(/needs the optional onnxruntime dependency/)).toBeVisible();
    expect(mockApi.getAeicSession).not.toHaveBeenCalled();
  });
});
