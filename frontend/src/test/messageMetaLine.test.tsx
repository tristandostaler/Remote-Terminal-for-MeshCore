import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MessageList } from '../components/MessageList';
import type { Message } from '../types';

const scrollIntoViewMock = vi.fn();

function createMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 1,
    type: 'PRIV',
    conversation_key: 'ab'.repeat(32),
    text: 'hello world',
    sender_timestamp: 1700000000,
    received_at: 1700000001,
    paths: null,
    txt_type: 0,
    signature: null,
    sender_key: null,
    outgoing: true,
    acked: 0,
    sender_name: null,
    ...overrides,
  };
}

beforeEach(() => {
  scrollIntoViewMock.mockReset();
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: scrollIntoViewMock,
    writable: true,
  });
});

describe('message meta line: compression', () => {
  it('shows the ratio and codec for a compressed message', () => {
    render(
      <MessageList
        messages={[
          createMessage({
            compression: 'mcmp2',
            plain_bytes: 100,
            wire_bytes: 47,
            payload_bytes: 47,
          }),
        ]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByText('53% mcmp2')).toBeInTheDocument();
  });

  it('measures a v3 ratio against the compressed text, not the container', () => {
    // 100 B of text in a 41 B payload of which 23 B is compressed text: the
    // percentage covers the text segment (77%), matching MCO Advanced.
    render(
      <MessageList
        messages={[
          createMessage({
            compression: 'mcmp3',
            plain_bytes: 100,
            wire_bytes: 41,
            payload_bytes: 23,
          }),
        ]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByText('77% mcmp3')).toBeInTheDocument();
  });

  it('reports the true on-air size in the tooltip, where it cannot be read as the ratio', () => {
    render(
      <MessageList
        messages={[
          createMessage({
            compression: 'mcmp3',
            plain_bytes: 100,
            wire_bytes: 41,
            payload_bytes: 23,
          }),
        ]}
        contacts={[]}
        loading={false}
      />
    );

    const badge = screen.getByText('77% mcmp3');
    expect(badge.getAttribute('title')).toContain('100 B of text went out as 41 B on air');
    expect(badge.getAttribute('title')).toContain('excluding the v3 container');
  });

  it('shows no badge when the body rode as plain text', () => {
    render(
      <MessageList
        messages={[createMessage({ compression: null })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.queryByText(/mcmp/)).not.toBeInTheDocument();
  });

  it('shows no badge for messages stored before compression tracking existed', () => {
    render(<MessageList messages={[createMessage()]} contacts={[]} loading={false} />);

    expect(screen.queryByText(/mcmp/)).not.toBeInTheDocument();
  });
});

describe('message meta line: send progress', () => {
  it('shows the attempt count once a retry has happened', () => {
    render(
      <MessageList
        messages={[createMessage({ send_attempts: 2, send_max_attempts: 5 })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByText('2/5')).toBeInTheDocument();
  });

  it('stays quiet on a first attempt, where there is nothing to report', () => {
    render(
      <MessageList
        messages={[createMessage({ send_attempts: 1, send_max_attempts: 5 })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.queryByText('1/5')).not.toBeInTheDocument();
  });

  it.each([
    ['sending', 'Sending — still retrying'],
    ['sent', 'Sent — no acknowledgement yet'],
    ['failed', 'Out of attempts without an acknowledgement'],
    ['canceled', 'Sending cancelled'],
  ] as const)('labels the %s state', (state, label) => {
    render(
      <MessageList
        messages={[createMessage({ send_state: state })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByLabelText(label)).toBeInTheDocument();
  });

  it('shows delivered once acked, even after the attempts ran out', () => {
    render(
      <MessageList
        messages={[createMessage({ send_state: 'failed', acked: 1 })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByLabelText('Delivered — acknowledged by the recipient')).toBeInTheDocument();
    expect(screen.queryByLabelText('Out of attempts without an acknowledgement')).toBeNull();
  });

  it('counts the echoes on a delivered channel message', () => {
    render(
      <MessageList
        messages={[createMessage({ type: 'CHAN', acked: 3 })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByText('✓✓3')).toBeInTheDocument();
  });

  it.each([
    ['CHAN', 'No repeater echoes heard yet'],
    ['PRIV', 'No acknowledgement heard yet'],
  ] as const)('names the right confirmation for an untracked %s message', (type, label) => {
    // Legacy rows carry no send state; a channel message is confirmed by echoes,
    // a direct one by an ACK, so "?" must not claim the wrong one.
    render(
      <MessageList
        messages={[createMessage({ type, send_state: null })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByLabelText(label)).toBeInTheDocument();
  });

  it('shows no send status on an incoming message', () => {
    render(
      <MessageList
        messages={[createMessage({ outgoing: false, send_state: null })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.queryByLabelText(/^Sent/)).toBeNull();
    expect(screen.queryByLabelText(/heard yet$/)).toBeNull();
  });
});

describe('message meta line: every glyph explains itself', () => {
  it('labels the time by direction, spelled out in full', () => {
    render(
      <MessageList
        messages={[createMessage({ outgoing: true, received_at: 1700000001 })]}
        contacts={[]}
        loading={false}
      />
    );

    const stamp = new Date(1700000001 * 1000).toLocaleString();
    expect(screen.getByTitle(`Sent ${stamp}`)).toBeInTheDocument();
  });

  it('says "Received" on an incoming message', () => {
    render(
      <MessageList
        messages={[createMessage({ outgoing: false, received_at: 1700000001 })]}
        contacts={[]}
        loading={false}
      />
    );

    const stamp = new Date(1700000001 * 1000).toLocaleString();
    expect(screen.getByTitle(`Received ${stamp}`)).toBeInTheDocument();
  });

  it('says what the number stuck to a delivered channel message counted', () => {
    render(
      <MessageList
        messages={[createMessage({ type: 'CHAN', acked: 3 })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByText('✓✓3').getAttribute('title')).toContain('3 repeater echoes heard');
  });

  it('distinguishes a direct acknowledgement from channel echoes', () => {
    render(
      <MessageList
        messages={[createMessage({ type: 'PRIV', acked: 1 })]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByText('✓✓').getAttribute('title')).toContain('acknowledged by the recipient');
  });

  it('explains both halves of the attempt counter and where the limit lives', () => {
    render(
      <MessageList
        messages={[createMessage({ send_attempts: 2, send_max_attempts: 3 })]}
        contacts={[]}
        loading={false}
      />
    );

    const title = screen.getByText('2/3').getAttribute('title');
    expect(title).toContain('Transmitted 2 times');
    expect(title).toContain('up to 3 allowed for this message');
    expect(title).toContain('Direct Message Send Attempts');
  });

  it('names every path in a multi-path hop badge', () => {
    render(
      <MessageList
        messages={[
          createMessage({
            outgoing: false,
            paths: [
              { path: '', path_len: 0, received_at: 1700000001 },
              { path: 'AABB', path_len: 2, received_at: 1700000002 },
            ],
          }),
        ]}
        contacts={[]}
        loading={false}
      />
    );

    expect(
      screen.getByTitle('Heard 2 times: direct, 2 hops — click to see the route')
    ).toBeInTheDocument();
  });

  it("words our own message's paths as echoes coming back", () => {
    render(
      <MessageList
        messages={[
          createMessage({
            outgoing: true,
            paths: [{ path: 'AABB', path_len: 2, received_at: 1700000002 }],
          }),
        ]}
        contacts={[]}
        loading={false}
      />
    );

    expect(
      screen.getByTitle('Echoed back over 2 hops — click to see the route')
    ).toBeInTheDocument();
  });

  it('pluralises a single hop correctly', () => {
    render(
      <MessageList
        messages={[
          createMessage({
            outgoing: false,
            paths: [{ path: 'AA', path_len: 1, received_at: 1700000002 }],
          }),
        ]}
        contacts={[]}
        loading={false}
      />
    );

    expect(screen.getByTitle('Arrived over 1 hop — click to see the route')).toBeInTheDocument();
  });

  it('says what the region badge means, not just its name', () => {
    render(
      <MessageList messages={[createMessage({ region: 'nl-gr' })]} contacts={[]} loading={false} />
    );

    expect(screen.getByText('nl-gr').getAttribute('title')).toContain(
      'only repeaters configured for "nl-gr" relay this'
    );
  });

  it('says what the actions button opens', () => {
    render(
      <MessageList
        messages={[createMessage()]}
        contacts={[]}
        loading={false}
        onDeleteMessage={vi.fn()}
      />
    );

    expect(
      screen.getByTitle('Message actions — copy, retry, cancel or delete')
    ).toBeInTheDocument();
  });
});

describe('message actions', () => {
  it('offers no menu when the host wired no actions', () => {
    render(<MessageList messages={[createMessage()]} contacts={[]} loading={false} />);

    expect(screen.queryByLabelText('Message actions')).toBeNull();
  });

  it('copies the message text', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    render(
      <MessageList
        messages={[createMessage({ text: 'copy me' })]}
        contacts={[]}
        loading={false}
        onDeleteMessage={vi.fn()}
      />
    );

    await userEvent.click(screen.getByLabelText('Message actions'));
    await userEvent.click(screen.getByRole('button', { name: 'Copy text' }));

    expect(writeText).toHaveBeenCalledWith('copy me');
  });

  it('retries a direct message under its original timestamp', async () => {
    const onRetryMessage = vi.fn();
    const message = createMessage({ type: 'PRIV', send_state: 'failed' });

    render(
      <MessageList
        messages={[message]}
        contacts={[]}
        loading={false}
        onRetryMessage={onRetryMessage}
      />
    );

    await userEvent.click(screen.getByLabelText('Message actions'));
    await userEvent.click(screen.getByRole('button', { name: 'Retry sending' }));

    // false = reuse the timestamp, which is what makes it a retry not a duplicate.
    expect(onRetryMessage).toHaveBeenCalledWith(message, false);
  });

  it('retries a channel message under a fresh timestamp', async () => {
    const onRetryMessage = vi.fn();
    const message = createMessage({ type: 'CHAN', send_state: 'sent' });

    render(
      <MessageList
        messages={[message]}
        contacts={[]}
        loading={false}
        onRetryMessage={onRetryMessage}
      />
    );

    await userEvent.click(screen.getByLabelText('Message actions'));
    await userEvent.click(screen.getByRole('button', { name: 'Retry sending' }));

    expect(onRetryMessage).toHaveBeenCalledWith(message, true);
  });

  it('offers cancel only while transmissions are still scheduled', async () => {
    render(
      <MessageList
        messages={[createMessage({ send_state: 'sending' })]}
        contacts={[]}
        loading={false}
        onCancelMessage={vi.fn()}
      />
    );

    await userEvent.click(screen.getByLabelText('Message actions'));
    expect(screen.getByRole('button', { name: 'Cancel sending' })).toBeInTheDocument();
  });

  it('hides cancel once the send has finished', async () => {
    render(
      <MessageList
        messages={[createMessage({ send_state: 'sent' })]}
        contacts={[]}
        loading={false}
        onCancelMessage={vi.fn()}
      />
    );

    await userEvent.click(screen.getByLabelText('Message actions'));
    expect(screen.queryByRole('button', { name: 'Cancel sending' })).toBeNull();
  });

  it('offers retry on our own messages but not on incoming ones', async () => {
    render(
      <MessageList
        messages={[createMessage({ outgoing: false })]}
        contacts={[]}
        loading={false}
        onRetryMessage={vi.fn()}
        onDeleteMessage={vi.fn()}
      />
    );

    await userEvent.click(screen.getByLabelText('Message actions'));
    expect(screen.queryByRole('button', { name: 'Retry sending' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Delete from history' })).toBeInTheDocument();
  });

  it('deletes a message', async () => {
    const onDeleteMessage = vi.fn();
    const message = createMessage();

    render(
      <MessageList
        messages={[message]}
        contacts={[]}
        loading={false}
        onDeleteMessage={onDeleteMessage}
      />
    );

    await userEvent.click(screen.getByLabelText('Message actions'));
    await userEvent.click(screen.getByRole('button', { name: 'Delete from history' }));

    expect(onDeleteMessage).toHaveBeenCalledWith(message);
  });
});
