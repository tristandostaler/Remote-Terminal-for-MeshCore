/**
 * Tests for MessageInput component.
 *
 * Verifies character/byte limit calculation, warning states, and send button
 * behavior for both DM and channel conversations.
 */

import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { MessageInput } from '../components/MessageInput';
import { RichPayloadProvider } from '../contexts/RichPayloadContext';
import { api } from '../api';
import { toast } from '../components/ui/sonner';
import { encodeMeshImage, prepareAeicImage } from '../services/imageCodec';
import type { ReplyContext } from '../types';

const voiceCapture = vi.hoisted(() => ({
  start: vi.fn().mockResolvedValue(undefined),
  stop: vi.fn().mockResolvedValue({
    pcm: new Blob(['voice']),
    durationMs: 500,
  }),
  cancel: vi.fn().mockResolvedValue(undefined),
}));
const encodedImage = vi.hoisted(() => ({
  blob: new Blob(['encoded-image'], { type: 'image/jpeg' }),
  format: 1 as const,
  width: 128,
  height: 96,
}));
const preparedAeicImage = vi.hoisted(() => ({
  rgb: new Uint8Array(12),
  sourceWidth: 4032,
  sourceHeight: 3024,
  previewBlob: new Blob(['aeic-preview'], { type: 'image/png' }),
}));

vi.mock('../services/voiceCapture', () => ({
  VoiceCapture: vi.fn(function VoiceCapture() {
    return voiceCapture;
  }),
}));

vi.mock('../services/imageCodec', () => ({
  encodeMeshImage: vi.fn().mockResolvedValue(encodedImage),
  prepareAeicImage: vi.fn().mockResolvedValue(preparedAeicImage),
  AEIC_SQUARE_SIZE: 512,
}));

// Mock sonner (toast)
vi.mock('../components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

// ONE api mock. There must not be a second vi.mock('../api') in this file: the
// last one registered wins outright, so a second partial mock silently replaces
// these spies with undefined and every assertion on them fails as "not a spy".
vi.mock('../api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api')>();
  return {
    ...original,
    api: {
      ...original.api,
      estimateMcmp: vi.fn(),
      sendVoice: vi.fn().mockResolvedValue(undefined),
      sendImage: vi.fn().mockResolvedValue(undefined),
      sendAeicImage: vi.fn().mockResolvedValue({
        session_key: 'self:0001',
        bitstream_bytes: 156,
        chunk_count: 2,
        messages: [],
      }),
    },
  };
});

const mockApi = api as unknown as { estimateMcmp: ReturnType<typeof vi.fn> };

const mockToast = toast as unknown as {
  success: ReturnType<typeof vi.fn>;
  error: ReturnType<typeof vi.fn>;
  info: ReturnType<typeof vi.fn>;
};

const textEncoder = new TextEncoder();

function byteLen(s: string): number {
  return textEncoder.encode(s).length;
}

describe('MessageInput', () => {
  const onSend = vi.fn().mockResolvedValue(undefined);

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, 'PointerEvent', { configurable: true, value: MouseEvent });
    voiceCapture.start.mockResolvedValue(undefined);
    voiceCapture.stop.mockResolvedValue({ pcm: new Blob(['voice']), durationMs: 500 });
    voiceCapture.cancel.mockResolvedValue(undefined);
  });

  function renderInput(props: {
    conversationType?: 'contact' | 'channel' | 'raw';
    senderName?: string;
    disabled?: boolean;
    voice?: boolean;
    mcmpEnabled?: boolean;
    imageCodec?: 'ie4' | 'aeic';
    replyContext?: ReplyContext | null;
    onCancelReply?: () => void;
    mentionCandidates?: string[];
    gifs?: boolean;
  }) {
    const input = (
      <MessageInput
        onSend={onSend}
        disabled={props.disabled ?? false}
        conversationType={props.conversationType}
        senderName={props.senderName}
        mcmpEnabled={props.mcmpEnabled}
        imageCodec={props.imageCodec}
        replyContext={props.replyContext}
        onCancelReply={props.onCancelReply}
        mentionCandidates={props.mentionCandidates}
        placeholder="Type a message..."
        voiceConversation={props.voice ? { type: 'PRIV', key: 'aa'.repeat(32) } : undefined}
      />
    );
    return render(
      props.gifs ? (
        <RichPayloadProvider renderRichPayloads setRenderRichPayloads={() => {}}>
          {input}
        </RichPayloadProvider>
      ) : (
        input
      )
    );
  }

  function getInput() {
    return screen.getByPlaceholderText('Type a message...') as HTMLTextAreaElement;
  }

  /** Emoji, photo and voice live behind the composer's "+" tray. */
  function openActions() {
    fireEvent.click(screen.getByRole('button', { name: 'Show message options' }));
  }

  function getSendButton() {
    return screen.getByRole('button', { name: /send/i }) as HTMLButtonElement;
  }

  describe('send button state', () => {
    it('is disabled when text is empty', () => {
      renderInput({ conversationType: 'contact' });
      expect(getSendButton()).toBeDisabled();
    });

    it('is enabled when text is entered', () => {
      renderInput({ conversationType: 'contact' });
      fireEvent.change(getInput(), { target: { value: 'Hello' } });
      expect(getSendButton()).toBeEnabled();
    });

    it('is disabled when whitespace-only', () => {
      renderInput({ conversationType: 'contact' });
      fireEvent.change(getInput(), { target: { value: '   ' } });
      expect(getSendButton()).toBeDisabled();
    });

    it('is disabled when disabled prop is true', () => {
      renderInput({ conversationType: 'contact', disabled: true });
      fireEvent.change(getInput(), { target: { value: 'Hello' } });
      expect(getSendButton()).toBeDisabled();
    });
  });

  describe('byte counter display', () => {
    it('shows byte counter for DM conversations', () => {
      renderInput({ conversationType: 'contact' });
      fireEvent.change(getInput(), { target: { value: 'Hello' } });

      // Should show "5/156" somewhere (DM hard limit = 156)
      expect(screen.getByText(/5\/156/)).toBeTruthy();
    });

    it('shows byte counter for channel conversations', () => {
      renderInput({ conversationType: 'channel', senderName: 'MyNode' });
      fireEvent.change(getInput(), { target: { value: 'Hello' } });

      // Channel hard limit = 156 - byteLen("MyNode") - 2 = 156 - 6 - 2 = 148
      expect(screen.getByText(/5\/148/)).toBeTruthy();
    });

    it('does not show byte counter for raw conversations', () => {
      renderInput({ conversationType: 'raw' });
      fireEvent.change(getInput(), { target: { value: 'Hello' } });

      // No counter should be visible
      expect(screen.queryByText(/\/\d+/)).toBeNull();
    });

    it('accounts for multi-byte characters in byte count', () => {
      renderInput({ conversationType: 'contact' });
      // Emoji: "🥝" is 4 bytes in UTF-8
      fireEvent.change(getInput(), { target: { value: '🥝' } });
      const bytes = byteLen('🥝'); // Should be 4
      expect(bytes).toBe(4);
      expect(screen.getByText(new RegExp(`${bytes}/156`))).toBeTruthy();
    });
  });

  describe('channel limit adjusts for sender name', () => {
    it('reduces limit based on sender name byte length', () => {
      // Sender name "LongNodeName" = 12 bytes + 2 for ": " = 14 overhead
      // Hard limit = 156 - 14 = 142
      renderInput({ conversationType: 'channel', senderName: 'LongNodeName' });
      fireEvent.change(getInput(), { target: { value: 'x' } });
      expect(screen.getByText(/1\/142/)).toBeTruthy();
    });

    it('uses default 10-byte name when sender name is absent', () => {
      // Default: 10 bytes + 2 = 12 overhead. Hard limit = 156 - 12 = 144
      renderInput({ conversationType: 'channel' });
      fireEvent.change(getInput(), { target: { value: 'x' } });
      expect(screen.getByText(/1\/144/)).toBeTruthy();
    });

    it('handles multi-byte sender names correctly', () => {
      // "🥝Node" = 4 + 4 = 8 bytes name + 2 separator = 10 overhead
      // Hard limit = 156 - 10 = 146
      const senderName = '🥝Node';
      const nameBytes = byteLen(senderName);
      const expectedLimit = 156 - nameBytes - 2;
      renderInput({ conversationType: 'channel', senderName });
      fireEvent.change(getInput(), { target: { value: 'x' } });
      expect(screen.getByText(new RegExp(`1/${expectedLimit}`))).toBeTruthy();
    });
  });

  describe('warning states', () => {
    it('shows warning text when exceeding DM warning threshold', () => {
      renderInput({ conversationType: 'contact' });
      // DM warning threshold = 140 bytes
      const text = 'x'.repeat(141);
      fireEvent.change(getInput(), { target: { value: text } });
      // Rendered in both desktop and mobile variants
      expect(screen.getAllByText(/may impact multi-repeater hop delivery/).length).toBeGreaterThan(
        0
      );
    });

    it('shows truncation warning when exceeding DM hard limit', () => {
      renderInput({ conversationType: 'contact' });
      // DM hard limit = 156 bytes
      const text = 'x'.repeat(157);
      fireEvent.change(getInput(), { target: { value: text } });
      // Rendered in both desktop and mobile variants
      expect(screen.getAllByText(/likely truncated by radio/).length).toBeGreaterThan(0);
    });

    it('shows no warning for short messages', () => {
      renderInput({ conversationType: 'contact' });
      fireEvent.change(getInput(), { target: { value: 'Hello' } });
      expect(screen.queryByText(/truncated/)).toBeNull();
      expect(screen.queryByText(/may impact/)).toBeNull();
    });
  });

  describe('send button remains enabled past hard limit (current behavior)', () => {
    it('does not disable send button when over hard limit', () => {
      // NOTE: This documents the current behavior where canSubmit only checks
      // text.trim().length > 0, NOT the limit state. This is related to
      // hitlist item 1.1 — the send button stays enabled even over the limit.
      renderInput({ conversationType: 'contact' });
      const text = 'x'.repeat(200); // Well over 156 byte limit
      fireEvent.change(getInput(), { target: { value: text } });

      // Button is still enabled — canSubmit only checks non-empty text
      expect(getSendButton()).toBeEnabled();
    });
  });

  describe('MCMP compressed counter', () => {
    it('shows the compressed wire size against the budget when enabled', async () => {
      // 200 raw chars would be over the 156 budget, but they compress to 40
      // bytes — the counter must reflect the compressed size so more fits.
      mockApi.estimateMcmp.mockResolvedValue({ wire_bytes: 40, compressed: true });
      renderInput({ conversationType: 'contact', mcmpEnabled: true });

      const text = 'x'.repeat(200);
      fireEvent.change(getInput(), { target: { value: text } });

      // Rendered in both desktop and mobile counter variants.
      await waitFor(() => expect(screen.getAllByText(/40\/156/).length).toBeGreaterThan(0));
      expect(mockApi.estimateMcmp).toHaveBeenCalledWith(text, 2);
      // The raw over-budget count is not shown.
      expect(screen.queryAllByText(/200\/156/).length).toBe(0);
    });

    it('does not query the backend when compression is off', () => {
      renderInput({ conversationType: 'contact', mcmpEnabled: false });
      fireEvent.change(getInput(), { target: { value: 'Hello there' } });
      // Raw byte count is shown; no estimate call.
      expect(screen.getByText(/11\/156/)).toBeTruthy();
      expect(mockApi.estimateMcmp).not.toHaveBeenCalled();
    });

    it('does not flash "too long" while the compressed estimate is pending', () => {
      // Estimate never resolves within the test: while pending, a long-but-
      // compressible draft must NOT show the raw over-budget error.
      mockApi.estimateMcmp.mockReturnValue(new Promise(() => {}));
      renderInput({ conversationType: 'contact', mcmpEnabled: true });

      fireEvent.change(getInput(), { target: { value: 'x'.repeat(200) } });

      // Without MCMP this 200-byte draft would be "likely truncated by radio";
      // with MCMP on and the estimate pending, we stay neutral.
      expect(screen.queryAllByText(/likely truncated by radio/).length).toBe(0);
      expect(screen.queryAllByText(/may impact multi-repeater/).length).toBe(0);
    });

    it('does show "too long" once the compressed size itself exceeds the budget', async () => {
      // A genuinely over-budget compressed size must still warn.
      mockApi.estimateMcmp.mockResolvedValue({ wire_bytes: 200, compressed: true });
      renderInput({ conversationType: 'contact', mcmpEnabled: true });

      fireEvent.change(getInput(), { target: { value: 'x'.repeat(400) } });
      await waitFor(() =>
        expect(screen.getAllByText(/likely truncated by radio/).length).toBeGreaterThan(0)
      );
    });
  });

  describe('send failure toasts', () => {
    it('shows the radio no-response toast when the send outcome is unknown', async () => {
      onSend.mockRejectedValueOnce(
        new Error(
          'Send command was issued to the radio, but no response was heard back. The message may or may not have sent successfully.'
        )
      );
      renderInput({ conversationType: 'contact' });

      fireEvent.change(getInput(), { target: { value: 'Hello' } });
      fireEvent.click(getSendButton());

      expect(await screen.findByDisplayValue('Hello')).toBeTruthy();
      expect(mockToast.error).toHaveBeenCalledWith('Radio did not confirm send', {
        description:
          'Send command was issued to the radio, but no response was heard back. The message may or may not have sent successfully.',
      });
    });
  });

  describe('voice recording', () => {
    it('places media controls left of the text field and always keeps send visible', () => {
      renderInput({ conversationType: 'contact', voice: true });

      // At rest the three are collapsed behind "+", so the text field gets the width.
      expect(screen.queryByRole('button', { name: /attach image/i })).toBeNull();
      expect(screen.queryByRole('button', { name: /hold to record voice/i })).toBeNull();

      openActions();
      const toggle = screen.getByRole('button', { name: 'Hide message options' });
      const emoji = screen.getByRole('button', { name: 'Add emoji' });
      const image = screen.getByRole('button', { name: /attach image/i });
      const microphone = screen.getByRole('button', { name: /hold to record voice/i });
      const input = getInput();
      // Left to right: the toggle, then the three it reveals, then the field.
      expect(toggle.compareDocumentPosition(emoji) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(emoji.compareDocumentPosition(image) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(
        image.compareDocumentPosition(microphone) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy();
      expect(
        microphone.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy();
      expect(screen.getByRole('button', { name: /^send$/i })).toBeVisible();
      expect(screen.getByRole('button', { name: /^send$/i })).toBeDisabled();
    });

    it('preserves text send and keeps image attachment available when text is entered', () => {
      renderInput({ conversationType: 'contact', voice: true });
      fireEvent.change(getInput(), { target: { value: 'Hello' } });
      openActions();

      expect(screen.getByRole('button', { name: /^send$/i })).toBeVisible();
      expect(screen.getByRole('button', { name: /attach image/i })).toBeVisible();
      expect(screen.getByRole('button', { name: /hold to record voice/i })).toBeVisible();
    });

    it('shows recording state on pointer down and sends on pointer up', async () => {
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
      renderInput({ conversationType: 'contact', voice: true });
      openActions();
      const microphone = screen.getByRole('button', { name: /hold to record voice/i });

      fireEvent.pointerDown(microphone, { pointerId: 1 });
      expect(await screen.findByText('Release to send')).toBeVisible();
      fireEvent.pointerUp(screen.getByRole('button', { name: /release to send voice/i }), {
        pointerId: 1,
      });

      await waitFor(() => expect(api.sendVoice).toHaveBeenCalledTimes(1));
    });

    it('does not send after the slide-up cancel gesture', async () => {
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
      renderInput({ conversationType: 'contact', voice: true });
      openActions();
      const microphone = screen.getByRole('button', { name: /hold to record voice/i });

      fireEvent.pointerDown(microphone, { pointerId: 1 });
      await screen.findByText('Release to send');
      const recordingMicrophone = screen.getByRole('button', { name: /release to send voice/i });
      fireEvent.pointerMove(recordingMicrophone, { pointerId: 1, clientY: -100 });
      expect(screen.getByText('Release to cancel')).toBeVisible();
      fireEvent.pointerUp(recordingMicrophone, { pointerId: 1 });

      await waitFor(() => expect(voiceCapture.cancel).toHaveBeenCalledTimes(1));
      expect(api.sendVoice).not.toHaveBeenCalled();
    });

    it('keeps the same mic element when recording starts, so the pointer stays captured', async () => {
      // The regression this guards: the old code unmounted the pressed button
      // and mounted a second one elsewhere in the row. Disconnecting the element
      // a pointer is captured to releases the capture, and on touch that loses
      // the pointerup which stops the recording -- leaving the 10s cap as the
      // only terminator, or a pointercancel that discards the take outright.
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
      renderInput({ conversationType: 'contact', voice: true });
      openActions();
      const before = screen.getByRole('button', { name: /hold to record voice/i });

      fireEvent.pointerDown(before, { pointerId: 1 });
      await screen.findByText('Release to send');

      expect(screen.getByRole('button', { name: /release to send voice/i })).toBe(before);
    });

    it('acknowledges the press before the microphone actually opens', async () => {
      // getUserMedia takes hundreds of ms on a phone, and prompts for permission
      // the first time. Until it resolves the composer must not look dead.
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
      let openMicrophone!: () => void;
      voiceCapture.start.mockReturnValue(
        new Promise<void>((resolve) => {
          openMicrophone = resolve;
        })
      );
      renderInput({ conversationType: 'contact', voice: true });
      openActions();

      fireEvent.pointerDown(screen.getByRole('button', { name: /hold to record voice/i }), {
        pointerId: 1,
      });

      expect(await screen.findByText('Starting microphone...')).toBeVisible();
      // No clock or level meter yet: nothing is being captured to time.
      expect(screen.queryByText('Release to send')).toBeNull();

      openMicrophone();
      expect(await screen.findByText('Release to send')).toBeVisible();
    });

    it('says a too-short press captured nothing instead of failing silently', async () => {
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
      voiceCapture.stop.mockResolvedValue({ pcm: new Blob(['x']), durationMs: 50 });
      renderInput({ conversationType: 'contact', voice: true });
      openActions();
      const microphone = screen.getByRole('button', { name: /hold to record voice/i });

      fireEvent.pointerDown(microphone, { pointerId: 1 });
      await screen.findByText('Release to send');
      fireEvent.pointerUp(microphone, { pointerId: 1 });

      await waitFor(() => expect(mockToast.info).toHaveBeenCalledOnce());
      expect(api.sendVoice).not.toHaveBeenCalled();
    });

    it('ignores a second press while a recording is already live', async () => {
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
      renderInput({ conversationType: 'contact', voice: true });
      openActions();
      const microphone = screen.getByRole('button', { name: /hold to record voice/i });

      fireEvent.pointerDown(microphone, { pointerId: 1 });
      await screen.findByText('Release to send');
      fireEvent.pointerDown(microphone, { pointerId: 2 });

      // A second capture would orphan the first, leaving its stream open.
      expect(voiceCapture.start).toHaveBeenCalledOnce();
    });

    it("does not let a finished recording's ten-second cap truncate the next one", async () => {
      // The cap is scheduled per recording. Left uncleared on release, it fires
      // part-way through whatever recording is live when its ten seconds are up.
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        renderInput({ conversationType: 'contact', voice: true });
        openActions();
        const microphone = screen.getByRole('button', { name: /hold to record voice/i });

        fireEvent.pointerDown(microphone, { pointerId: 1 });
        await screen.findByText('Release to send');
        fireEvent.pointerUp(microphone, { pointerId: 1 });
        await waitFor(() => expect(voiceCapture.stop).toHaveBeenCalledOnce());

        // Start a second take five seconds in, so the first cap would land
        // half-way through it.
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5_000);
        });
        fireEvent.pointerDown(microphone, { pointerId: 2 });
        await screen.findByText('Release to send');
        await act(async () => {
          await vi.advanceTimersByTimeAsync(6_000);
        });

        // Still going: only the first take has been stopped.
        expect(voiceCapture.stop).toHaveBeenCalledOnce();
        expect(screen.getByText('Release to send')).toBeVisible();
      } finally {
        vi.useRealTimers();
      }
    });

    it('suppresses the long-press menu that would fight the gesture', () => {
      renderInput({ conversationType: 'contact', voice: true });
      openActions();
      const microphone = screen.getByRole('button', { name: /hold to record voice/i });

      const menu = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
      microphone.dispatchEvent(menu);

      expect(menu.defaultPrevented).toBe(true);
    });

    it('explains the HTTPS requirement before requesting a microphone', () => {
      Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false });
      renderInput({ conversationType: 'contact', voice: true });
      openActions();
      fireEvent.pointerDown(screen.getByRole('button', { name: /hold to record voice/i }));
      expect(mockToast.error).toHaveBeenCalledWith(
        'Voice recording requires HTTPS to access your microphone.',
        expect.objectContaining({ action: expect.objectContaining({ label: 'Configure HTTPS' }) })
      );
    });
  });

  describe('image attachment', () => {
    it('opens the picker, previews a selected image, and cancels', async () => {
      renderInput({ conversationType: 'contact', voice: true });
      const picker = screen.getByLabelText('Choose image') as HTMLInputElement;
      const click = vi.spyOn(picker, 'click');
      openActions();
      fireEvent.click(screen.getByRole('button', { name: /attach image/i }));
      expect(click).toHaveBeenCalledOnce();

      const file = new File(['source'], 'photo.png', { type: 'image/png' });
      fireEvent.change(picker, { target: { files: [file] } });
      expect(await screen.findByAltText('Image attachment preview')).toBeVisible();
      expect(screen.getByText(/128×96/)).toBeVisible();
      expect(screen.getByText(/1 fragments/)).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(screen.queryByAltText('Image attachment preview')).not.toBeInTheDocument();
    });

    it('sends only after preview confirmation', async () => {
      renderInput({ conversationType: 'contact', voice: true });
      const file = new File(['source'], 'photo.jpg', { type: 'image/jpeg' });
      fireEvent.change(screen.getByLabelText('Choose image'), { target: { files: [file] } });
      expect(await screen.findByAltText('Image attachment preview')).toBeVisible();
      expect(api.sendImage).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole('button', { name: 'Send image' }));
      await waitFor(() =>
        expect(api.sendImage).toHaveBeenCalledWith('PRIV', 'aa'.repeat(32), encodedImage)
      );
    });

    it('routes an attachment through the AI codec when the conversation selects it', async () => {
      renderInput({ conversationType: 'contact', voice: true, imageCodec: 'aeic' });
      const file = new File(['source'], 'photo.jpg', { type: 'image/jpeg' });
      fireEvent.change(screen.getByLabelText('Choose image'), { target: { files: [file] } });

      // The preview shows what will actually be encoded: a 512px square, with
      // the source shape recorded so the receiver can undo the stretch.
      expect(await screen.findByText(/512×512 colour/)).toBeVisible();
      expect(screen.getByText(/from 4032×3024/)).toBeVisible();
      expect(screen.getByText(/1–2 messages/)).toBeVisible();

      expect(prepareAeicImage).toHaveBeenCalledWith(file);
      expect(encodeMeshImage).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Send photo' }));
      await waitFor(() =>
        expect(api.sendAeicImage).toHaveBeenCalledWith('PRIV', 'aa'.repeat(32), preparedAeicImage)
      );
      expect(api.sendImage).not.toHaveBeenCalled();
    });

    it('hides the max-size selector for the AI codec, which is always 512px', async () => {
      renderInput({ conversationType: 'contact', voice: true, imageCodec: 'aeic' });
      const file = new File(['source'], 'photo.jpg', { type: 'image/jpeg' });
      fireEvent.change(screen.getByLabelText('Choose image'), { target: { files: [file] } });
      await screen.findByText(/512×512 colour/);
      expect(screen.queryByLabelText('Maximum image dimension')).not.toBeInTheDocument();
    });

    /**
     * Switching the codec on an already-attached photo has to re-prepare it.
     *
     * When preparation was imperative -- fired from the file input's onChange --
     * the old pixels survived the switch, and the send went out with the codec
     * the user had just switched AWAY from while the panel header claimed the
     * new one. Preparation is now an effect over (file, size, codec).
     */
    it('re-prepares and sends via IE4 when the codec switches away from AI', async () => {
      const { rerender } = renderInput({
        conversationType: 'contact',
        voice: true,
        imageCodec: 'aeic',
      });
      const file = new File(['source'], 'photo.jpg', { type: 'image/jpeg' });
      fireEvent.change(screen.getByLabelText('Choose image'), { target: { files: [file] } });
      await screen.findByText(/512×512 colour/);
      expect(prepareAeicImage).toHaveBeenCalledWith(file);
      expect(encodeMeshImage).not.toHaveBeenCalled();

      rerender(
        <MessageInput
          onSend={onSend}
          disabled={false}
          conversationType="contact"
          imageCodec="ie4"
          placeholder="Type a message..."
          voiceConversation={{ type: 'PRIV', key: 'aa'.repeat(32) }}
        />
      );

      // The IE4 preparation runs, and the AI-specific preview line is gone.
      await waitFor(() => expect(encodeMeshImage).toHaveBeenCalledWith(file, 256));
      await waitFor(() => expect(screen.queryByText(/512×512 colour/)).not.toBeInTheDocument());

      fireEvent.click(screen.getByRole('button', { name: 'Send image' }));
      await waitFor(() =>
        expect(api.sendImage).toHaveBeenCalledWith('PRIV', 'aa'.repeat(32), encodedImage)
      );
      expect(api.sendAeicImage).not.toHaveBeenCalled();
    });

    it('re-prepares and sends via the AI codec when the codec switches to it', async () => {
      const { rerender } = renderInput({
        conversationType: 'contact',
        voice: true,
        imageCodec: 'ie4',
      });
      const file = new File(['source'], 'photo.jpg', { type: 'image/jpeg' });
      fireEvent.change(screen.getByLabelText('Choose image'), { target: { files: [file] } });
      await screen.findByAltText('Image attachment preview');
      expect(encodeMeshImage).toHaveBeenCalledWith(file, 256);

      rerender(
        <MessageInput
          onSend={onSend}
          disabled={false}
          conversationType="contact"
          imageCodec="aeic"
          placeholder="Type a message..."
          voiceConversation={{ type: 'PRIV', key: 'aa'.repeat(32) }}
        />
      );

      await waitFor(() => expect(prepareAeicImage).toHaveBeenCalledWith(file));
      fireEvent.click(screen.getByRole('button', { name: 'Send photo' }));
      await waitFor(() =>
        expect(api.sendAeicImage).toHaveBeenCalledWith('PRIV', 'aa'.repeat(32), preparedAeicImage)
      );
      expect(api.sendImage).not.toHaveBeenCalled();
    });

    it('rejects an invalid image cleanly', async () => {
      vi.mocked(encodeMeshImage).mockRejectedValueOnce(new Error('Invalid image data'));
      renderInput({ conversationType: 'contact', voice: true });
      const file = new File(['bad'], 'broken.png', { type: 'image/png' });
      fireEvent.change(screen.getByLabelText('Choose image'), { target: { files: [file] } });
      await waitFor(() =>
        expect(mockToast.error).toHaveBeenCalledWith('Image unavailable', {
          description: 'Invalid image data',
        })
      );
      expect(screen.queryByRole('button', { name: 'Send image' })).not.toBeInTheDocument();
    });
  });

  describe('GIF sending', () => {
    const fetchMock = vi.fn();

    beforeEach(() => {
      fetchMock.mockResolvedValue({
        ok: true,
        json: async () => ({
          data: [
            {
              id: 'abc123',
              title: 'Waving cat',
              images: { fixed_height_small: { url: 'https://media.giphy.com/small.gif' } },
            },
          ],
        }),
      });
      vi.stubGlobal('fetch', fetchMock);
    });

    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it('hides the GIF button while rendering MeshCore Open GIFs is off', () => {
      renderInput({ conversationType: 'channel', voice: true });
      openActions();

      expect(screen.queryByRole('button', { name: 'Add GIF' })).toBeNull();
    });

    it('offers GIF as a fourth tray action when enabled', () => {
      renderInput({ conversationType: 'channel', voice: true, gifs: true });
      openActions();

      expect(screen.getByRole('button', { name: 'Add GIF' })).toHaveTextContent('GIF');
    });

    it('picks a GIF, previews it, and sends the meshcore-open g:<id> payload', async () => {
      renderInput({ conversationType: 'channel', voice: true, gifs: true });
      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add GIF' }));

      fireEvent.click(await screen.findByRole('button', { name: 'Send GIF: Waving cat' }));

      expect(fetchMock.mock.calls[0][0]).toContain('https://api.giphy.com/v1/gifs/trending?');
      expect(screen.getByAltText('GIF preview')).toHaveAttribute(
        'src',
        'https://media.giphy.com/media/abc123/giphy.gif'
      );
      expect(screen.queryByPlaceholderText('Type a message...')).toBeNull();

      fireEvent.click(getSendButton());
      await waitFor(() => expect(onSend).toHaveBeenCalledWith('g:abc123'));
      expect(getInput()).toHaveValue('');
    });

    it('sends a GIF picked while replying as a meshcore-open reply', async () => {
      renderInput({
        conversationType: 'channel',
        voice: true,
        gifs: true,
        replyContext: { senderName: 'Alice', preview: 'hi' },
      });
      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add GIF' }));
      fireEvent.click(await screen.findByRole('button', { name: 'Send GIF: Waving cat' }));
      fireEvent.click(getSendButton());

      await waitFor(() => expect(onSend).toHaveBeenCalledWith('@[Alice] >hi\ng:abc123'));
    });

    it('searches on Enter without submitting the composer', async () => {
      renderInput({ conversationType: 'channel', voice: true, gifs: true });
      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add GIF' }));
      await screen.findByRole('button', { name: 'Send GIF: Waving cat' });

      const search = screen.getByRole('searchbox', { name: 'Search GIFs' });
      fireEvent.change(search, { target: { value: 'cats & dogs' } });
      fireEvent.keyDown(search, { key: 'Enter' });

      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      const url = new URL(fetchMock.mock.calls[1][0] as string);
      expect(url.pathname).toBe('/v1/gifs/search');
      expect(url.searchParams.get('q')).toBe('cats & dogs');
      expect(url.searchParams.get('rating')).toBe('g');
      expect(onSend).not.toHaveBeenCalled();
    });

    it('removes a picked GIF and restores the text field', async () => {
      renderInput({ conversationType: 'channel', voice: true, gifs: true });
      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add GIF' }));
      fireEvent.click(await screen.findByRole('button', { name: 'Send GIF: Waving cat' }));

      fireEvent.click(screen.getByRole('button', { name: 'Remove GIF' }));

      expect(getInput()).toHaveValue('');
      expect(screen.queryByAltText('GIF preview')).toBeNull();
    });

    it('shows a retry when Giphy cannot be reached', async () => {
      fetchMock.mockRejectedValueOnce(new Error('offline'));
      renderInput({ conversationType: 'channel', voice: true, gifs: true });
      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add GIF' }));

      fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));

      expect(await screen.findByRole('button', { name: 'Send GIF: Waving cat' })).toBeVisible();
    });
  });

  describe('the "+" options tray', () => {
    it('rests as one button and reveals all three actions when opened', () => {
      renderInput({ conversationType: 'contact', voice: true });

      expect(screen.getByRole('button', { name: 'Show message options' })).toBeVisible();
      expect(screen.queryByRole('button', { name: 'Add emoji' })).toBeNull();

      openActions();

      expect(screen.getByRole('button', { name: 'Add emoji' })).toBeVisible();
      expect(screen.getByRole('button', { name: /attach image/i })).toBeVisible();
      expect(screen.getByRole('button', { name: /hold to record voice/i })).toBeVisible();
    });

    it('reports its state to assistive tech and closes again on a second press', () => {
      renderInput({ conversationType: 'contact', voice: true });

      const toggle = screen.getByRole('button', { name: 'Show message options' });
      expect(toggle).toHaveAttribute('aria-expanded', 'false');

      openActions();
      const open = screen.getByRole('button', { name: 'Hide message options' });
      expect(open).toHaveAttribute('aria-expanded', 'true');

      fireEvent.click(open);
      expect(screen.queryByRole('button', { name: 'Add emoji' })).toBeNull();
    });

    it('skips the tray when emoji is the only action available', () => {
      // Hiding a lone button behind a second tap buys nothing.
      renderInput({ conversationType: 'contact' });

      expect(screen.queryByRole('button', { name: 'Show message options' })).toBeNull();
      expect(screen.getByRole('button', { name: 'Add emoji' })).toBeVisible();
    });

    it('collapses once an emoji has been inserted', () => {
      renderInput({ conversationType: 'contact', voice: true });

      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add emoji' }));
      fireEvent.click(screen.getByRole('button', { name: 'Insert 👍' }));

      expect(getInput()).toHaveValue('👍');
      expect(screen.getByRole('button', { name: 'Show message options' })).toBeVisible();
    });

    it('collapses once a photo has been chosen', async () => {
      renderInput({ conversationType: 'contact', voice: true });

      openActions();
      const file = new File(['source'], 'photo.png', { type: 'image/png' });
      fireEvent.change(screen.getByLabelText('Choose image'), { target: { files: [file] } });

      expect(await screen.findByAltText('Image attachment preview')).toBeVisible();
      expect(screen.getByRole('button', { name: 'Show message options' })).toBeVisible();
    });

    it('keeps the file input mounted while collapsed', () => {
      // The tray closes the instant a file is picked, so an input that unmounted
      // with it would drop the change event the OS dialog is about to deliver.
      renderInput({ conversationType: 'contact', voice: true });

      expect(screen.getByLabelText('Choose image')).toBeInTheDocument();
    });

    it('collapses after a message is sent', async () => {
      renderInput({ conversationType: 'contact', voice: true });

      openActions();
      fireEvent.change(getInput(), { target: { value: 'Hello' } });
      fireEvent.click(getSendButton());

      await waitFor(() => expect(onSend).toHaveBeenCalledWith('Hello'));
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Show message options' })).toBeVisible()
      );
    });
  });

  describe('emoji picker', () => {
    it('opens the picker and inserts a quick-row emoji into the message', () => {
      renderInput({ conversationType: 'contact', voice: true });

      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add emoji' }));
      expect(screen.getByRole('dialog', { name: 'Emoji picker' })).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Insert 👍' }));

      expect(getInput()).toHaveValue('👍');
      expect(screen.queryByRole('dialog', { name: 'Emoji picker' })).not.toBeInTheDocument();
      expect(getSendButton()).toBeEnabled();
    });

    it('offers the reaction quick row first, in MCO Advanced order', () => {
      renderInput({ conversationType: 'contact', voice: true });

      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add emoji' }));

      // Same table (and order) as the reaction picker — the composer and the
      // message-actions dialog share one panel.
      const picker = screen.getByRole('dialog', { name: 'Emoji picker' });
      const quick = ['👍', '❤️', '😂', '🎉', '👏', '🔥'];
      const buttons = within(picker).getAllByRole('button');
      expect(buttons.slice(0, quick.length).map((b) => b.textContent)).toEqual(quick);
      // The full grid stays behind the ⋯ toggle until asked for.
      expect(within(picker).queryByRole('button', { name: 'Insert 😀' })).toBeNull();
    });

    it('expands the scrollable grid of every choice and inserts from it', () => {
      renderInput({ conversationType: 'contact', voice: true });

      openActions();
      fireEvent.click(screen.getByRole('button', { name: 'Add emoji' }));
      fireEvent.click(screen.getByRole('button', { name: 'More emojis' }));

      expect(screen.getByText('Smileys')).toBeVisible();
      expect(screen.getByText('Gestures')).toBeVisible();
      expect(screen.getByText('Hearts')).toBeVisible();
      expect(screen.getByText('Objects')).toBeVisible();

      fireEvent.click(screen.getByRole('button', { name: 'Insert 😀' }));

      expect(getInput()).toHaveValue('😀');
      expect(screen.queryByRole('dialog', { name: 'Emoji picker' })).not.toBeInTheDocument();
      // Inserting collapses the tray, same as before.
      expect(screen.getByRole('button', { name: 'Show message options' })).toBeVisible();
    });
  });

  describe('reply', () => {
    it('shows the reply banner with the sender name and quote', () => {
      renderInput({
        conversationType: 'contact',
        replyContext: { senderName: 'Alice', preview: 'are we still on for tomorrow' },
      });

      expect(screen.getByText('Replying to Alice')).toBeInTheDocument();
      expect(screen.getByText('are we still on for tomorrow')).toBeInTheDocument();
    });

    it('cancels the reply from the banner', () => {
      const onCancelReply = vi.fn();
      renderInput({
        conversationType: 'contact',
        replyContext: { senderName: 'Alice', preview: 'hey' },
        onCancelReply,
      });

      fireEvent.click(screen.getByRole('button', { name: 'Cancel reply' }));
      expect(onCancelReply).toHaveBeenCalled();
    });

    it('prefixes the sent text with the wire mention and quote', async () => {
      renderInput({
        conversationType: 'contact',
        replyContext: { senderName: 'Alice', preview: 'are we still on for tomorrow' },
      });

      fireEvent.change(getInput(), { target: { value: 'yes, see you then' } });
      fireEvent.click(getSendButton());

      await waitFor(() => expect(onSend).toHaveBeenCalled());
      expect(onSend).toHaveBeenCalledWith(
        '@[Alice] >are we still on for tomorrow\nyes, see you then'
      );
    });

    it('clears the reply after a successful send', async () => {
      const onCancelReply = vi.fn();
      renderInput({
        conversationType: 'contact',
        replyContext: { senderName: 'Alice', preview: 'hey' },
        onCancelReply,
      });

      fireEvent.change(getInput(), { target: { value: 'yo' } });
      fireEvent.click(getSendButton());

      await waitFor(() => expect(onCancelReply).toHaveBeenCalled());
    });

    it('does not clear the reply when the send fails', async () => {
      onSend.mockRejectedValueOnce(new Error('no radio'));
      const onCancelReply = vi.fn();
      renderInput({
        conversationType: 'contact',
        replyContext: { senderName: 'Alice', preview: 'hey' },
        onCancelReply,
      });

      fireEvent.change(getInput(), { target: { value: 'yo' } });
      fireEvent.click(getSendButton());

      await waitFor(() => expect(onSend).toHaveBeenCalled());
      expect(onCancelReply).not.toHaveBeenCalled();
      expect(screen.getByText('Replying to Alice')).toBeInTheDocument();
    });

    it('counts the mention+quote overhead in the byte counter', () => {
      // "@[Alice] >hey\n" is the wire prefix; "hi" is 2 more bytes.
      renderInput({
        conversationType: 'contact',
        replyContext: { senderName: 'Alice', preview: 'hey' },
      });
      fireEvent.change(getInput(), { target: { value: 'hi' } });

      const prefixBytes = byteLen('@[Alice] >hey\n');
      expect(screen.getByText(new RegExp(`${prefixBytes + 2}/156`))).toBeTruthy();
    });
  });

  describe('mention autocomplete', () => {
    it('shows no popup without candidates', () => {
      renderInput({ conversationType: 'channel' });
      fireEvent.change(getInput(), { target: { value: '@' } });
      expect(screen.queryByRole('listbox', { name: 'Mention suggestions' })).toBeNull();
    });

    it('suggests matching names after "@"', () => {
      renderInput({ conversationType: 'channel', mentionCandidates: ['Alice', 'Alicia', 'Bob'] });
      const input = getInput();
      fireEvent.change(input, { target: { value: '@ali' } });

      const popup = screen.getByRole('listbox', { name: 'Mention suggestions' });
      expect(within(popup).getByText('@Alice')).toBeInTheDocument();
      expect(within(popup).getByText('@Alicia')).toBeInTheDocument();
      expect(within(popup).queryByText('@Bob')).toBeNull();
    });

    it('inserts the bracketed wire mention on click', () => {
      renderInput({ conversationType: 'channel', mentionCandidates: ['Alice', 'Bob'] });
      const input = getInput();
      fireEvent.change(input, { target: { value: 'hey @al' } });

      fireEvent.mouseDown(screen.getByRole('option', { name: '@Alice' }));

      expect(input).toHaveValue('hey @[Alice] ');
    });

    it('inserts the highlighted candidate on Enter and closes the popup', () => {
      renderInput({ conversationType: 'channel', mentionCandidates: ['Alice', 'Bob'] });
      const input = getInput();
      fireEvent.change(input, { target: { value: '@a' } });

      fireEvent.keyDown(input, { key: 'Enter' });

      expect(input).toHaveValue('@[Alice] ');
      expect(onSend).not.toHaveBeenCalled();
      expect(screen.queryByRole('listbox', { name: 'Mention suggestions' })).toBeNull();
    });

    it('cycles the highlighted candidate with the arrow keys', () => {
      renderInput({ conversationType: 'channel', mentionCandidates: ['Alice', 'Bob', 'Charlie'] });
      const input = getInput();
      fireEvent.change(input, { target: { value: '@' } });

      fireEvent.keyDown(input, { key: 'ArrowDown' });
      fireEvent.keyDown(input, { key: 'Enter' });

      expect(input).toHaveValue('@[Bob] ');
    });

    it('dismisses the popup on Escape without changing the text', () => {
      renderInput({ conversationType: 'channel', mentionCandidates: ['Alice'] });
      const input = getInput();
      fireEvent.change(input, { target: { value: '@al' } });

      fireEvent.keyDown(input, { key: 'Escape' });

      expect(screen.queryByRole('listbox', { name: 'Mention suggestions' })).toBeNull();
      expect(input).toHaveValue('@al');
    });

    it('closes when there is no longer a match', () => {
      renderInput({ conversationType: 'channel', mentionCandidates: ['Alice'] });
      const input = getInput();
      fireEvent.change(input, { target: { value: '@zz' } });

      expect(screen.queryByRole('listbox', { name: 'Mention suggestions' })).toBeNull();
    });
  });
});
