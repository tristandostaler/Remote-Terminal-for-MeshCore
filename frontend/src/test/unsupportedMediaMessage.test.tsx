/**
 * The box for a picture this server kept but cannot decode.
 *
 * It exists because the alternative was silence: an image someone sent on a
 * channel was identified, refused and dropped, and nothing in the conversation
 * said a picture had been sent at all. So the box has two jobs — say a picture
 * arrived and why it is not shown, and make clear the data is still here, which is
 * what makes the retry worth offering before any decoder exists.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MessageList } from '../components/MessageList';
import { api } from '../api';
import { parseUnsupportedMediaRef } from '../utils/aeicEnvelope';
import type { Message } from '../types';

vi.mock('../api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api')>();
  return {
    ...original,
    api: {
      ...original.api,
      getUnsupportedMedia: vi.fn(),
      retryUnsupportedMediaDecode: vi.fn(),
    },
  };
});

const mockApi = api as unknown as {
  getUnsupportedMedia: ReturnType<typeof vi.fn>;
  retryUnsupportedMediaDecode: ReturnType<typeof vi.fn>;
};

const REASON =
  'This is an MCOimg picture. RemoteTerm has no decoder for that codec, so it cannot be ' +
  'shown here yet. The data has been kept: if MCOimg support is added, this picture will ' +
  'open without the sender resending it.';

function keptMedia(overrides: Record<string, unknown> = {}) {
  return {
    id: 55,
    conversation_key: 'ab'.repeat(16),
    data_type: 0xfff0,
    codec_label: 'MCOimg image (codec not supported here)',
    received_at: 1700000000,
    blob_count: 3,
    total_bytes: 402,
    decoded: false,
    reason: REASON,
    ...overrides,
  };
}

/** The marker row the server mints for a kept arrival. Never goes on air. */
function markerMessage(): Message {
  return {
    id: 91,
    type: 'CHAN',
    conversation_key: 'ab'.repeat(16),
    text: 'mediax:55',
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

describe('parseUnsupportedMediaRef', () => {
  it('reads the id out of a marker row', () => {
    expect(parseUnsupportedMediaRef('mediax:55')).toBe(55);
  });

  it('ignores anything that is not one', () => {
    // Notably the other marker and ordinary chat, which share the conversation.
    expect(parseUnsupportedMediaRef('aeib:grp:92e4d63e')).toBeNull();
    expect(parseUnsupportedMediaRef('hello there')).toBeNull();
    expect(parseUnsupportedMediaRef('mediax:')).toBeNull();
    expect(parseUnsupportedMediaRef('mediax:nope')).toBeNull();
    expect(parseUnsupportedMediaRef('mediax:0')).toBeNull();
  });
});

describe('the kept-but-undecodable picture box', () => {
  beforeEach(() => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: vi.fn(),
      writable: true,
    });
    mockApi.getUnsupportedMedia.mockReset().mockResolvedValue(keptMedia());
    mockApi.retryUnsupportedMediaDecode.mockReset().mockResolvedValue(keptMedia());
  });

  it('says a picture arrived and why it is not shown', async () => {
    render(<MessageList messages={[markerMessage()]} contacts={[]} loading={false} />);

    expect(await screen.findByText('Picture not shown')).toBeVisible();
    expect(screen.getByText(/no decoder for that codec/)).toBeVisible();
    expect(mockApi.getUnsupportedMedia).toHaveBeenCalledWith(55);
  });

  it('says the data is still here, which is what makes retrying worth offering', async () => {
    render(<MessageList messages={[markerMessage()]} contacts={[]} loading={false} />);

    expect(await screen.findByText(/402 bytes kept in 3 packets/)).toBeVisible();
  });

  it('offers a retry and reports that there is still no decoder', async () => {
    render(<MessageList messages={[markerMessage()]} contacts={[]} loading={false} />);
    const retry = await screen.findByRole('button', { name: /Retry decoding/ });

    await userEvent.click(retry);

    await waitFor(() => expect(mockApi.retryUnsupportedMediaDecode).toHaveBeenCalledWith(55));
  });

  it('still says something useful when the detail cannot be fetched', async () => {
    /*
     * The row is the durable part; the detail is a request that can fail. Falling
     * back to nothing would put an empty box in the conversation, which is worse
     * than the silence this replaced.
     */
    mockApi.getUnsupportedMedia.mockRejectedValue(new Error('offline'));
    render(<MessageList messages={[markerMessage()]} contacts={[]} loading={false} />);

    expect(await screen.findByText('Picture not shown')).toBeVisible();
    expect(screen.getByText(/cannot decode/)).toBeVisible();
  });
});
