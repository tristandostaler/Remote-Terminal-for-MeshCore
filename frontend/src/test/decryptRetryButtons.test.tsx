import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ChannelInfoPane } from '../components/ChannelInfoPane';
import { MessageList } from '../components/MessageList';
import { clearDecryptProgress } from '../stores/decryptProgressStore';
import type { Channel, ChannelDetail, Message } from '../types';

vi.mock('../api', () => ({
  api: {
    getChannelDetail: vi.fn(),
    decryptHistoricalPackets: vi.fn(),
    decryptHistoricalAllChannels: vi.fn(),
    getDecryptStatus: vi.fn(),
  },
  isAbortError: () => false,
}));

import { api } from '../api';

const mockGetChannelDetail = vi.mocked(api.getChannelDetail);
const mockDecryptHistorical = vi.mocked(api.decryptHistoricalPackets);

function makeChannel(key: string): Channel {
  return {
    key,
    name: '#ops',
    is_hashtag: true,
    on_radio: false,
    last_read_at: null,
    favorite: false,
    muted: false,
  };
}

function makeDetail(channel: Channel): ChannelDetail {
  return {
    channel,
    message_counts: { last_1h: 0, last_24h: 0, last_48h: 0, last_7d: 0, all_time: 0 },
    first_message_at: null,
    unique_sender_count: 0,
    top_senders_24h: [],
    path_hash_width_24h: {
      total_packets: 0,
      single_byte: 0,
      double_byte: 0,
      triple_byte: 0,
      single_byte_pct: 0,
      double_byte_pct: 0,
      triple_byte_pct: 0,
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  clearDecryptProgress();
});

describe('per-channel retry decrypt', () => {
  it('sweeps stored packets with this room key', async () => {
    const key = 'AA'.repeat(16);
    mockGetChannelDetail.mockResolvedValue(makeDetail(makeChannel(key)));
    mockDecryptHistorical.mockResolvedValue({
      started: true,
      total_packets: 1200,
      message: 'Started decrypt of 1200 packets in background',
    });

    render(
      <ChannelInfoPane
        channelKey={key}
        channels={[makeChannel(key)]}
        onClose={() => {}}
        onToggleFavorite={() => {}}
      />
    );

    const button = await screen.findByText('Retry historical decrypt');
    fireEvent.click(button);

    await waitFor(() =>
      expect(mockDecryptHistorical).toHaveBeenCalledWith({
        key_type: 'channel',
        channel_key: key,
      })
    );
  });
});

describe('recovered message badge', () => {
  function makeMessage(overrides: Partial<Message> = {}): Message {
    return {
      id: 1,
      type: 'CHAN',
      conversation_key: 'AA'.repeat(16),
      text: 'Alice: from the past',
      sender_timestamp: 1700000000,
      received_at: 1700000000,
      paths: null,
      txt_type: 0,
      signature: null,
      sender_key: null,
      outgoing: false,
      acked: 0,
      sender_name: 'Alice',
      ...overrides,
    };
  }

  beforeEach(() => {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: vi.fn(),
      writable: true,
    });
  });

  it('marks a message a sweep recovered', () => {
    render(
      <MessageList
        messages={[makeMessage({ recovered_at: 1700009999 })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByText('Recovered')).toBeInTheDocument();
  });

  it('leaves messages heard live unmarked', () => {
    render(<MessageList messages={[makeMessage()]} contacts={[]} loading={false} />);

    expect(screen.queryByText('Recovered')).not.toBeInTheDocument();
  });
});
