/**
 * Reaching a tooltip with a finger.
 *
 * The reported failure: on a phone there was no way to read the explanation
 * behind a message's timestamp, attempt counter or compression badge. Hover does
 * not exist, and the long press that comes closest opens the message-actions
 * dialog -- so the gesture people reached for offered to retry sending instead.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MessageList } from '../components/MessageList';
import { TapTooltipLayer, tapTooltipTarget } from '../components/TapTooltipLayer';
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

/** jsdom has no PointerEvent, and pointerType is the field that matters here. */
function pointer(element: Element, type: 'pointerdown' | 'pointerup', pointerType: string) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  fireEvent(element, event);
}

/**
 * A finger: press, release, and the click a browser sends afterwards -- which
 * iOS Safari withholds for elements nothing considers clickable, so the tooltip
 * has to be open before it arrives.
 */
function tap(element: Element) {
  pointer(element, 'pointerdown', 'touch');
  pointer(element, 'pointerup', 'touch');
  fireEvent.click(element);
}

/** The same tap on a phone that never sends the trailing click. */
function tapWithoutClick(element: Element) {
  pointer(element, 'pointerdown', 'touch');
  pointer(element, 'pointerup', 'touch');
}

/** A finger held down: the press, the contextmenu the OS sends, then release. */
function longPress(element: Element) {
  pointer(element, 'pointerdown', 'touch');
  fireEvent.contextMenu(element);
  pointer(element, 'pointerup', 'touch');
}

/** A mouse. */
function click(element: Element) {
  pointer(element, 'pointerdown', 'mouse');
  pointer(element, 'pointerup', 'mouse');
  fireEvent.click(element);
}

function rightClick(element: Element) {
  pointer(element, 'pointerdown', 'mouse');
  fireEvent.contextMenu(element);
}

function renderMessage(message: Message, props: Record<string, unknown> = {}) {
  return render(
    <>
      <TapTooltipLayer />
      <MessageList messages={[message]} contacts={[]} loading={false} {...props} />
    </>
  );
}

describe('reading a tooltip on a touch screen', () => {
  it('shows the timestamp explanation when the timestamp is tapped', () => {
    const message = createMessage({ received_at: 1700000001 });
    renderMessage(message);
    const stamp = new Date(1700000001 * 1000).toLocaleString();

    tap(screen.getByTitle(`Sent ${stamp}`));

    expect(screen.getByRole('tooltip')).toHaveTextContent(`Sent ${stamp}`);
  });

  it('explains the attempt counter, which is the number nobody can guess', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));

    tap(screen.getByText('2/3'));

    expect(screen.getByRole('tooltip')).toHaveTextContent(
      'Transmitted 2 times, up to 3 allowed for this message'
    );
  });

  it('explains the compression badge', () => {
    renderMessage(
      createMessage({ compression: 'mcmp2', plain_bytes: 100, wire_bytes: 47, payload_bytes: 47 })
    );

    tap(screen.getByText('53% mcmp2'));

    expect(screen.getByRole('tooltip')).toHaveTextContent('Compressed with MCMP v2');
  });

  it('opens on a tap that never sends a click, the way iOS does not', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));

    tapWithoutClick(screen.getByText('2/3'));

    expect(screen.getByRole('tooltip')).toHaveTextContent('Transmitted 2 times');
  });

  it('opens on a mouse click too, for anyone who reaches for one', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));

    click(screen.getByText('2/3'));

    expect(screen.getByRole('tooltip')).toHaveTextContent('Transmitted 2 times');
  });

  it('points the screen reader at the bubble it just opened', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));
    const counter = screen.getByText('2/3');

    tap(counter);

    expect(counter).toHaveAttribute('aria-describedby', screen.getByRole('tooltip').id);
  });
});

describe('the long press that used to offer a retry', () => {
  it('shows the tooltip instead of the message-actions dialog', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }), {
      onRetryMessage: vi.fn(),
    });

    longPress(screen.getByText('2/3'));

    expect(screen.getByRole('tooltip')).toHaveTextContent('Transmitted 2 times');
    expect(screen.queryByText('Message actions')).not.toBeInTheDocument();
  });

  it('still opens the dialog when the press lands on the message text', () => {
    renderMessage(createMessage({ text: 'hello world' }), { onRetryMessage: vi.fn() });

    longPress(screen.getByText('hello world'));

    expect(screen.getByText('Message actions')).toBeInTheDocument();
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('leaves the desktop right-click habit alone', () => {
    // Right-clicking anywhere on a bubble opens the actions dialog, and a mouse
    // already has hover for the tooltip -- so a mouse keeps the dialog.
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }), {
      onRetryMessage: vi.fn(),
    });

    rightClick(screen.getByText('2/3'));

    expect(screen.getByText('Message actions')).toBeInTheDocument();
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });
});

describe('what a tap must not interrupt', () => {
  it('leaves the actions button to open the dialog', async () => {
    renderMessage(createMessage(), { onDeleteMessage: vi.fn() });

    await userEvent.click(screen.getByLabelText('Message actions'));

    expect(screen.getByText('Message actions')).toBeInTheDocument();
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('leaves a hop badge to open the route it describes', () => {
    renderMessage(createMessage({ paths: [{ path: '11', received_at: 1, path_len: 1 }] }));

    // The badge is a role="button" that opens the path detail: pressing it must
    // show the route, not a bubble repeating the label.
    tap(screen.getByText('(1)'));

    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });
});

describe('dismissing', () => {
  it('closes when the same element is clicked again', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));
    const counter = screen.getByText('2/3');

    click(counter);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();

    click(counter);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('closes when the same element is tapped again', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));
    const counter = screen.getByText('2/3');

    tap(counter);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();

    tap(counter);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('closes when something else is pressed', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));

    tap(screen.getByText('2/3'));
    expect(screen.getByRole('tooltip')).toBeInTheDocument();

    fireEvent.pointerDown(screen.getByText('hello world'));

    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('closes on Escape', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));

    tap(screen.getByText('2/3'));
    expect(screen.getByRole('tooltip')).toBeInTheDocument();

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('closes when the list scrolls, since the anchor has moved', () => {
    renderMessage(createMessage({ send_attempts: 2, send_max_attempts: 3 }));

    tap(screen.getByText('2/3'));
    expect(screen.getByRole('tooltip')).toBeInTheDocument();

    fireEvent.scroll(screen.getByText('hello world'));

    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });
});

describe('which presses get a tooltip', () => {
  function element(html: string): HTMLElement {
    const host = document.createElement('div');
    host.innerHTML = html;
    document.body.appendChild(host);
    return host.firstElementChild as HTMLElement;
  }

  it('takes the nearest title above the press', () => {
    const outer = element('<div title="outer"><span>text</span></div>');
    expect(tapTooltipTarget(outer.querySelector('span'))).toBe(outer);
  });

  it('ignores an empty title', () => {
    expect(tapTooltipTarget(element('<span title="   ">x</span>'))).toBeNull();
  });

  it('ignores anything with no title at all', () => {
    expect(tapTooltipTarget(element('<span>x</span>'))).toBeNull();
  });

  it('ignores a titled control, whose press is its own', () => {
    for (const html of [
      '<button title="Send">x</button>',
      '<a href="#x" title="Open">x</a>',
      '<span role="button" title="Show route">x</span>',
      '<span tabindex="0" title="Mention someone">x</span>',
      '<input title="Name" />',
    ]) {
      expect(tapTooltipTarget(element(html))).toBeNull();
    }
  });

  it('ignores a title wrapped around a control, when the control is pressed', () => {
    const wrapper = element('<div title="row"><button>press</button></div>');
    expect(tapTooltipTarget(wrapper.querySelector('button'))).toBeNull();
  });

  it('lets a surface opt out entirely', () => {
    const opted = element('<div data-no-tap-tooltip><span title="chart point">x</span></div>');
    expect(tapTooltipTarget(opted.querySelector('span'))).toBeNull();
  });
});
