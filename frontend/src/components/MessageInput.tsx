import {
  useState,
  useCallback,
  useImperativeHandle,
  forwardRef,
  useRef,
  useEffect,
  useMemo,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  type PointerEvent,
} from 'react';
import { ImagePlus, Loader2, Mic, Plus, Smile, X } from 'lucide-react';
import { EmojiPickerPanel } from './EmojiPickerPanel';
import { api } from '../api';
import {
  encodeMeshImage,
  prepareAeicImage,
  AEIC_SQUARE_SIZE,
  type EncodedMeshImage,
  type PreparedAeicImage,
} from '../services/imageCodec';
import type { ImageCodecId } from '../api';
import { estimateImageTransmitSeconds, IMAGE_FRAGMENT_BYTES } from '../utils/imageEnvelope';
import { VoiceCapture } from '../services/voiceCapture';
import { Shrink } from 'lucide-react';
import { Button } from './ui/button';
import { toast } from './ui/sonner';
import { cn } from '@/lib/utils';
import {
  getTextReplaceEnabled,
  getTextReplaceMapJson,
  applyTextReplacements,
} from '../utils/textReplace';

// MeshCore message size limits (empirically determined from LoRa packet constraints)
// Direct delivery allows ~156 bytes; multi-hop requires buffer for path growth.
// Channels include "sender: " prefix in the encrypted payload.
// All limits are in bytes (UTF-8), not characters, since LoRa packets are byte-constrained.
const DM_HARD_LIMIT = 156; // Max bytes for direct delivery
const DM_WARNING_THRESHOLD = 140; // Conservative for multi-hop
const CHANNEL_HARD_LIMIT = 156; // Base byte limit before sender overhead
const CHANNEL_WARNING_THRESHOLD = 120; // Conservative for multi-hop
const CHANNEL_DANGER_BUFFER = 8; // Red zone starts this many bytes before hard limit

const textEncoder = new TextEncoder();
const RADIO_NO_RESPONSE_SNIPPET = 'no response was heard back';
/** Get UTF-8 byte length of a string (LoRa packets are byte-constrained, not character-constrained). */
function byteLen(s: string): number {
  return textEncoder.encode(s).length;
}

interface MessageInputProps {
  onSend: (text: string) => Promise<void>;
  disabled: boolean;
  placeholder?: string;
  /** Conversation type for character limit calculation */
  conversationType?: 'contact' | 'channel' | 'raw';
  /** Sender name (radio name) for channel message limit calculation */
  senderName?: string;
  voiceConversation?: { type: 'PRIV' | 'CHAN'; key: string };
  /** When the conversation compresses outbound messages (MCMP), the counter
   *  reflects the compressed wire size instead of the raw byte length. */
  mcmpEnabled?: boolean;
  /** MCMP transport version (2 or 3) the estimate should size for. */
  mcmpVersion?: number;
  /** Which codec an attached photo uses. 'aeic' replaces the AVIF/JPEG fragment
   *  transport with the neural codec: ~150 bytes as one or two text messages. */
  imageCodec?: ImageCodecId;
}

type LimitState = 'normal' | 'warning' | 'danger' | 'error';

export interface MessageInputHandle {
  appendText: (text: string) => void;
  focus: () => void;
}

export const MessageInput = forwardRef<MessageInputHandle, MessageInputProps>(function MessageInput(
  {
    onSend,
    disabled,
    placeholder,
    conversationType,
    senderName,
    voiceConversation,
    mcmpEnabled,
    mcmpVersion,
    imageCodec = 'ie4',
  },
  ref
) {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  // Compressed wire size fetched from the backend (the MCMP codec is
  // server-side), tagged with the exact draft it was computed for so a stale
  // result is never shown for different text. null until the first estimate
  // resolves, or when compression is off for this conversation.
  const [compressed, setCompressed] = useState<{ bytes: number; forText: string } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const captureRef = useRef<VoiceCapture | null>(null);
  const voiceHeldRef = useRef(false);
  const cancelVoiceRef = useRef(false);
  const voiceStartedAtRef = useRef(0);
  // The 10s cap has to be cancellable. Left running, the timer from a released
  // recording fires part-way through the *next* one and cuts it short.
  const voiceTimeoutRef = useRef<number | null>(null);
  const [recording, setRecording] = useState(false);
  // getUserMedia is awaited inside the press, and on a phone that is hundreds of
  // milliseconds (plus a permission prompt the first time). Without a state for
  // "pressed but not yet capturing" the button looks dead for that whole window.
  const [arming, setArming] = useState(false);
  const [voiceSending, setVoiceSending] = useState(false);
  const [cancelVoice, setCancelVoice] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [emojiPickerOpen, setEmojiPickerOpen] = useState(false);
  // Emoji, photo and voice live behind one "+" so the resting composer is a
  // single button and the text field gets the width.
  const [actionsOpen, setActionsOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [imageSize, setImageSize] = useState<64 | 128 | 256>(256);
  const [imagePreview, setImagePreview] = useState<EncodedMeshImage | null>(null);
  const [aeicPreview, setAeicPreview] = useState<PreparedAeicImage | null>(null);
  const [imagePreparing, setImagePreparing] = useState(false);
  const [imageSending, setImageSending] = useState(false);
  const [imagePreviewUrl, setImagePreviewUrl] = useState<string | null>(null);

  // A tray left open in one conversation should not greet you in the next.
  useEffect(() => {
    setActionsOpen(false);
    setEmojiPickerOpen(false);
  }, [voiceConversation?.key]);

  useEffect(() => {
    const blob = imagePreview?.blob ?? aeicPreview?.previewBlob ?? null;
    if (!blob) {
      setImagePreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(blob);
    setImagePreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [imagePreview, aeicPreview]);

  // The two codecs need different pixels, so preparation branches here rather
  // than at send time: the preview has to show what will actually be encoded.
  // IE4 wants a downscaled greyscale AVIF/JPEG; AEIC wants the whole frame
  // stretched into a 512px RGB square.
  //
  // Driven by an effect over (file, size, codec) rather than called from each
  // control's onChange. When it was imperative, changing the codec on an
  // already-attached photo left the old preparation in place and the send went
  // out with the codec the user had just switched AWAY from — while the panel
  // header claimed the new one.
  useEffect(() => {
    if (!imageFile) {
      setImagePreview(null);
      setAeicPreview(null);
      setImagePreparing(false);
      return;
    }
    let cancelled = false;
    setImagePreparing(true);
    setImagePreview(null);
    setAeicPreview(null);
    void (async () => {
      try {
        if (imageCodec === 'aeic') {
          const prepared = await prepareAeicImage(imageFile);
          if (!cancelled) setAeicPreview(prepared);
        } else {
          const encoded = await encodeMeshImage(imageFile, imageSize);
          if (!cancelled) setImagePreview(encoded);
        }
      } catch (error) {
        if (cancelled) return;
        setImageFile(null);
        toast.error('Image unavailable', {
          description: error instanceof Error ? error.message : String(error),
        });
      } finally {
        if (!cancelled) setImagePreparing(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [imageFile, imageSize, imageCodec]);

  const clearImage = useCallback(() => {
    setImageFile(null);
    setImagePreview(null);
    setAeicPreview(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, []);

  const sendImage = useCallback(async () => {
    if (!voiceConversation || imageSending) return;
    setImageSending(true);
    try {
      // Branch on the SELECTED codec, not on whichever preview happens to be
      // populated: the two must agree, and the selection is the authority.
      if (imageCodec === 'aeic') {
        if (!aeicPreview) return;
        // The server does the ~0.3 s encode and then transmits one or two
        // aei1: messages; this POST is just the 768 KB of prepared pixels.
        await api.sendAeicImage(voiceConversation.type, voiceConversation.key, aeicPreview);
      } else {
        if (!imagePreview) return;
        await api.sendImage(voiceConversation.type, voiceConversation.key, imagePreview);
      }
      clearImage();
    } catch (error) {
      toast.error('Image message failed', {
        description: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setImageSending(false);
    }
  }, [aeicPreview, clearImage, imageCodec, imagePreview, imageSending, voiceConversation]);

  useEffect(() => {
    if (!recording) return;
    const timer = window.setInterval(
      () => setElapsed((performance.now() - voiceStartedAtRef.current) / 1000),
      100
    );
    return () => window.clearInterval(timer);
  }, [recording]);

  const finishVoice = useCallback(
    async (cancel = false) => {
      if (voiceTimeoutRef.current !== null) {
        window.clearTimeout(voiceTimeoutRef.current);
        voiceTimeoutRef.current = null;
      }
      const capture = captureRef.current;
      if (!capture) return;
      captureRef.current = null;
      setRecording(false);
      setArming(false);
      try {
        if (cancel || cancelVoiceRef.current) {
          await capture.cancel();
          return;
        }
        const result = await capture.stop();
        if (result.durationMs < 200) {
          // Silence here is indistinguishable from a broken button, and a tap
          // instead of a hold is the easiest mistake to make on a touchscreen.
          toast.info('Hold the microphone to record', {
            description: 'That press was too short to capture anything.',
          });
          return;
        }
        if (!voiceConversation) return;
        setVoiceSending(true);
        await api.sendVoice(voiceConversation.type, voiceConversation.key, result.pcm);
      } catch (error) {
        toast.error('Voice message failed', {
          description: error instanceof Error ? error.message : String(error),
        });
      } finally {
        setVoiceSending(false);
        setCancelVoice(false);
        cancelVoiceRef.current = false;
        setElapsed(0);
      }
    },
    [voiceConversation]
  );

  const startVoice = useCallback(
    async (event: PointerEvent<HTMLButtonElement>) => {
      if (!voiceConversation || disabled || voiceSending) return;
      // A second press while one capture is already live would orphan the first.
      if (recording || arming || captureRef.current) return;
      if (!window.isSecureContext) {
        toast.error('Voice recording requires HTTPS to access your microphone.', {
          action: {
            label: 'Configure HTTPS',
            onClick: () => {
              window.location.hash = '#settings/https';
            },
          },
        });
        return;
      }
      event.currentTarget.setPointerCapture?.(event.pointerId);
      voiceHeldRef.current = true;
      // Acknowledge the press before awaiting the microphone, so the composer
      // shows something is happening during the getUserMedia round trip.
      setArming(true);
      const capture = new VoiceCapture();
      try {
        await capture.start();
        if (!voiceHeldRef.current) {
          await capture.cancel();
          setArming(false);
          return;
        }
        captureRef.current = capture;
        voiceStartedAtRef.current = performance.now();
        setArming(false);
        setRecording(true);
        voiceTimeoutRef.current = window.setTimeout(() => {
          voiceTimeoutRef.current = null;
          void finishVoice(false);
        }, 10_000);
      } catch (error) {
        setArming(false);
        toast.error('Microphone unavailable', {
          description: error instanceof Error ? error.message : String(error),
        });
      }
    },
    [arming, disabled, finishVoice, recording, voiceConversation, voiceSending]
  );

  /** Resize textarea to fit content, clamped between 1 row and ~6 rows. */
  const autoResize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    // Clamp: min 40px (≈1 row), max 160px (≈6 rows)
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, []);

  useImperativeHandle(ref, () => ({
    appendText: (appendedText: string) => {
      setText((prev) => prev + appendedText);
      textareaRef.current?.focus();
    },
    focus: () => {
      textareaRef.current?.focus();
    },
  }));

  // Re-measure height whenever text changes (covers programmatic updates like appendText)
  useEffect(() => {
    autoResize();
  }, [text, autoResize]);

  // Calculate character limits based on conversation type
  const limits = useMemo(() => {
    if (conversationType === 'contact') {
      return {
        warningAt: DM_WARNING_THRESHOLD,
        dangerAt: DM_HARD_LIMIT, // Same as hard limit for DMs (no intermediate red zone)
        hardLimit: DM_HARD_LIMIT,
      };
    } else if (conversationType === 'channel') {
      // Channel hard limit = 156 bytes - senderName bytes - 2 (for ": " separator)
      const nameByteLen = senderName ? byteLen(senderName) : 10;
      const hardLimit = Math.max(1, CHANNEL_HARD_LIMIT - nameByteLen - 2);
      return {
        warningAt: CHANNEL_WARNING_THRESHOLD,
        dangerAt: Math.max(1, hardLimit - CHANNEL_DANGER_BUFFER),
        hardLimit,
      };
    }
    return null; // Raw/other - no limits
  }, [conversationType, senderName]);

  // UTF-8 byte length of the current text (LoRa packets are byte-constrained)
  const textByteLen = useMemo(() => byteLen(text), [text]);

  // When MCMP is on, poll the backend (which owns the codec) for the compressed
  // wire size, debounced. That size is what actually rides the packet, so the
  // effective character capacity grows as compressible text is typed.
  useEffect(() => {
    if (!mcmpEnabled || !limits || text.trim().length === 0) {
      setCompressed(null);
      return;
    }
    let cancelled = false;
    const handle = setTimeout(() => {
      api
        .estimateMcmp(text, mcmpVersion ?? 2)
        .then((res) => {
          if (!cancelled) setCompressed({ bytes: res.wire_bytes, forText: text });
        })
        .catch(() => {
          if (!cancelled) setCompressed(null);
        });
    }, 150);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [text, mcmpEnabled, mcmpVersion, limits]);

  // The byte count the counter shows: the compressed size when we have one (the
  // exact one for this draft, or the last one while a fresh estimate is in
  // flight), otherwise the raw length (and always for uncompressed
  // conversations).
  const showCompressed = mcmpEnabled && compressed !== null;
  const effectiveByteLen = compressed !== null ? compressed.bytes : textByteLen;
  // True while MCMP is on but the compressed size for the *current* text hasn't
  // resolved yet (the estimate is debounced). We must not judge the draft "too
  // long" from the raw length during this window — that caused a red flash on
  // long-but-compressible messages before the estimate came back.
  const estimatePending =
    mcmpEnabled &&
    !!limits &&
    text.trim().length > 0 &&
    !(compressed !== null && compressed.forText === text);

  // Determine current limit state
  const { limitState, warningMessage } = useMemo((): {
    limitState: LimitState;
    warningMessage: string | null;
  } => {
    if (!limits) return { limitState: 'normal', warningMessage: null };
    // Stay neutral until the compressed size is known — don't alarm on the raw
    // length while compression is being computed.
    if (estimatePending) return { limitState: 'normal', warningMessage: null };

    if (effectiveByteLen >= limits.hardLimit) {
      return { limitState: 'error', warningMessage: 'likely truncated by radio' };
    }
    if (effectiveByteLen >= limits.dangerAt) {
      return { limitState: 'danger', warningMessage: 'may impact multi-repeater hop delivery' };
    }
    if (effectiveByteLen >= limits.warningAt) {
      return { limitState: 'warning', warningMessage: 'may impact multi-repeater hop delivery' };
    }
    return { limitState: 'normal', warningMessage: null };
  }, [effectiveByteLen, limits, estimatePending]);

  const remaining = limits ? limits.hardLimit - effectiveByteLen : 0;

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      const trimmed = text.trim();
      if (!trimmed || sending || disabled) return;

      setSending(true);
      try {
        await onSend(trimmed);
        setText('');
        setActionsOpen(false);
      } catch (err) {
        console.error('Failed to send message:', err);
        const description = err instanceof Error ? err.message : 'Check radio connection';
        const isRadioNoResponse =
          err instanceof Error && err.message.toLowerCase().includes(RADIO_NO_RESPONSE_SNIPPET);
        toast.error(isRadioNoResponse ? 'Radio did not confirm send' : 'Failed to send message', {
          description,
        });
        return;
      } finally {
        setSending(false);
      }
      // Refocus after React re-enables the textarea
      setTimeout(() => textareaRef.current?.focus(), 0);
    },
    [text, sending, disabled, onSend]
  );

  const handleChange = useCallback((e: ChangeEvent<HTMLTextAreaElement>) => {
    const input = e.target;
    const raw = input.value;
    // Skip replacement during IME / dead-key composition to avoid garbling interim input
    if (!e.nativeEvent || (e.nativeEvent as InputEvent).isComposing) {
      setText(raw);
      return;
    }
    if (getTextReplaceEnabled()) {
      const result = applyTextReplacements(
        raw,
        input.selectionStart ?? raw.length,
        getTextReplaceMapJson()
      );
      if (result) {
        setText(result.text);
        // Schedule cursor restore after React flushes the new value
        const pos = result.cursor;
        requestAnimationFrame(() => input.setSelectionRange(pos, pos));
        return;
      }
    }
    setText(raw);
  }, []);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSubmit(e as unknown as FormEvent);
      }
      // Shift+Enter falls through naturally and inserts a newline
    },
    [handleSubmit]
  );

  const canSubmit = text.trim().length > 0;

  // "The voice gesture owns the composer": true from the moment the mic is
  // pressed, not from the moment audio starts flowing, so the row does not sit
  // unchanged through the getUserMedia round trip.
  const voiceActive = arming || recording;

  const insertEmoji = useCallback(
    (emoji: string) => {
      const input = textareaRef.current;
      const start = input?.selectionStart ?? text.length;
      const end = input?.selectionEnd ?? start;
      setText((current) => `${current.slice(0, start)}${emoji}${current.slice(end)}`);
      setEmojiPickerOpen(false);
      setActionsOpen(false);
      requestAnimationFrame(() => {
        const cursor = start + emoji.length;
        textareaRef.current?.focus();
        textareaRef.current?.setSelectionRange(cursor, cursor);
      });
    },
    [text.length]
  );

  // Show counter for messages (not raw).
  // Desktop: always visible. Mobile: only show count after 100 characters.
  const showCharCounter = limits !== null;
  const showMobileCounterValue = text.length > 100;

  return (
    <form
      className="message-input-shell px-4 py-2.5 border-t border-border flex flex-col gap-1"
      onSubmit={handleSubmit}
      autoComplete="off"
    >
      {(imageFile || imagePreparing) && (
        <div className="rounded-lg border border-border bg-muted/30 p-3">
          <div className="flex gap-3">
            <div className="flex h-24 w-24 shrink-0 items-center justify-center overflow-hidden rounded-md bg-muted">
              {imagePreparing || !imagePreviewUrl ? (
                <Loader2 className="animate-spin text-muted-foreground" size={22} />
              ) : (
                <img
                  src={imagePreviewUrl}
                  alt="Image attachment preview"
                  className="h-full w-full object-cover"
                />
              )}
            </div>
            <div className="min-w-0 flex-1 text-sm">
              <div className="font-medium">
                {imageCodec === 'aeic' ? 'Photo (AI reconstruction)' : 'Image attachment'}
              </div>
              {aeicPreview && (
                <div className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                  <div>
                    {AEIC_SQUARE_SIZE}×{AEIC_SQUARE_SIZE} colour · from {aeicPreview.sourceWidth}×
                    {aeicPreview.sourceHeight}
                  </div>
                  {/* Deliberately a range, not a number: the bitstream size is
                      only known after the server-side encode. Measured 117-209 B
                      over the reference corpus, which is 1-2 messages. */}
                  <div>~150 bytes · 1–2 messages</div>
                  <div>Estimated transfer: ~4s per direct hop</div>
                  <div>
                    The receiver rebuilds this with a neural decoder, so they see a similar picture
                    rather than the same pixels.
                  </div>
                </div>
              )}
              {imagePreview && (
                <div className="mt-1 space-y-0.5 text-xs text-muted-foreground">
                  <div>
                    {imagePreview.width}×{imagePreview.height} ·{' '}
                    {imagePreview.format === 0 ? 'AVIF' : 'JPEG'}
                  </div>
                  <div>
                    {(imagePreview.blob.size / 1024).toFixed(1)} KB ·{' '}
                    {Math.ceil(imagePreview.blob.size / IMAGE_FRAGMENT_BYTES)} fragments
                  </div>
                  <div>
                    Estimated transfer: ~
                    {Math.max(
                      1,
                      Math.round(
                        estimateImageTransmitSeconds(
                          Math.ceil(imagePreview.blob.size / IMAGE_FRAGMENT_BYTES),
                          imagePreview.blob.size
                        )
                      )
                    )}
                    s per direct hop
                  </div>
                  {imagePreview.blob.size > 3000 && (
                    <div className="text-warning">
                      Large LoRa transfer — consider a smaller size.
                    </div>
                  )}
                </div>
              )}
              {/* AEIC always encodes a 512px square, so there is nothing to pick.
                  Not rendered at all rather than hidden: a display:none control
                  is still a tab stop and still reachable by a screen reader. */}
              {imageCodec !== 'aeic' && (
                <label className="mt-2 flex items-center gap-2 text-xs">
                  Max size
                  <select
                    aria-label="Maximum image dimension"
                    value={imageSize}
                    disabled={imagePreparing || imageSending}
                    className="rounded border border-input bg-background px-1.5 py-1"
                    onChange={(event) => {
                      // Re-preparation is the effect's job; setting the size is
                      // all this has to do.
                      setImageSize(Number(event.target.value) as 64 | 128 | 256);
                    }}
                  >
                    <option value={64}>64 px</option>
                    <option value={128}>128 px</option>
                    <option value={256}>256 px</option>
                  </select>
                </label>
              )}
            </div>
          </div>
          <div className="mt-3 flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={imageSending}
              onClick={clearImage}
            >
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={(!imagePreview && !aeicPreview) || imagePreparing || imageSending}
              onClick={() => void sendImage()}
            >
              {imageSending ? 'Sending...' : imageCodec === 'aeic' ? 'Send photo' : 'Send image'}
            </Button>
          </div>
        </div>
      )}
      <div className="relative flex gap-2 items-end">
        {voiceConversation && (
          <>
            {/* Mounted whether or not the tray is open: the tray closes the
                moment a file is picked, and an unmounted input would drop the
                change event the OS dialog is about to deliver. */}
            <input
              ref={fileInputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/avif"
              className="sr-only"
              aria-label="Choose image"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                setImageFile(file);
                setActionsOpen(false);
              }}
            />
          </>
        )}
        {!voiceActive && voiceConversation && (
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="flex-shrink-0 rounded-full"
            aria-label={actionsOpen ? 'Hide message options' : 'Show message options'}
            aria-expanded={actionsOpen}
            title="Emoji, photo and voice message"
            disabled={disabled || sending}
            onClick={() => {
              setActionsOpen((open) => !open);
              setEmojiPickerOpen(false);
            }}
          >
            {actionsOpen ? <X size={18} /> : <Plus size={18} />}
          </Button>
        )}
        {/* With no attachments available the tray would hide a single emoji
            button behind a second tap, so it is skipped entirely. */}
        {!voiceActive && (actionsOpen || !voiceConversation) && (
          <div className="relative flex-shrink-0">
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="rounded-full"
              aria-label="Add emoji"
              aria-expanded={emojiPickerOpen}
              disabled={disabled || sending}
              onClick={() => setEmojiPickerOpen((open) => !open)}
            >
              <Smile size={18} />
            </Button>
            {emojiPickerOpen && (
              <div
                role="dialog"
                aria-label="Emoji picker"
                // Wide enough for the quick row (6 emojis + the ⋯ toggle) with
                // a viewport clamp for narrow phones.
                className="absolute bottom-12 left-0 z-20 w-[19rem] max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-popover p-2 text-popover-foreground shadow-lg"
              >
                {/* Same picker as message reactions: the MCO Advanced table,
                    quick row first, full categorized grid behind the ⋯. */}
                <EmojiPickerPanel emojiLabel={(emoji) => `Insert ${emoji}`} onPick={insertEmoji} />
              </div>
            )}
          </div>
        )}
        {!voiceActive && actionsOpen && voiceConversation && (
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="flex-shrink-0 rounded-full"
            disabled={disabled || sending || imagePreparing || imageSending}
            aria-label="Attach image"
            onClick={() => fileInputRef.current?.click()}
          >
            <ImagePlus size={18} />
          </Button>
        )}
        {/* ONE mic button, in a slot whose position does not change when
            recording starts. The old code swapped it for a second button
            elsewhere in the row, which disconnected the element the pointer was
            captured to -- on touch that loses the pointerup that stops the
            recording, leaving the 10s timeout as the only terminator. Keeping
            the same DOM node across the state flip is the whole fix. */}
        {voiceConversation && (actionsOpen || voiceActive) && (
          <Button
            type="button"
            variant={voiceActive ? 'destructive' : 'outline'}
            size="icon"
            className={cn(
              'flex-shrink-0 touch-none select-none rounded-full',
              recording && !cancelVoice && 'animate-pulse'
            )}
            // Stops iOS/Android raising the text-selection callout on a long
            // press, which otherwise fights the gesture mid-recording.
            style={{ WebkitTouchCallout: 'none' }}
            onContextMenu={(event) => event.preventDefault()}
            disabled={disabled || voiceSending}
            aria-label={
              voiceActive ? 'Release to send voice message' : 'Hold to record voice message'
            }
            onPointerDown={startVoice}
            onPointerUp={() => {
              voiceHeldRef.current = false;
              void finishVoice();
            }}
            onPointerCancel={() => {
              voiceHeldRef.current = false;
              void finishVoice(true);
            }}
            onPointerMove={(event) => {
              const bounds = event.currentTarget.getBoundingClientRect();
              const movedAway =
                event.clientY < bounds.top - 60 ||
                event.clientY > bounds.bottom + 60 ||
                event.clientX < bounds.left - 60 ||
                event.clientX > bounds.right + 60;
              if (recording && movedAway) {
                cancelVoiceRef.current = true;
                setCancelVoice(true);
              }
            }}
          >
            {cancelVoice ? <X size={18} /> : <Mic size={18} />}
          </Button>
        )}
        {voiceActive ? (
          <div
            role="status"
            aria-live="polite"
            className={cn(
              'flex min-h-10 flex-1 min-w-0 items-center gap-2 rounded-md border px-3 text-sm',
              cancelVoice
                ? 'border-destructive/50 bg-destructive/10 text-destructive'
                : 'border-border bg-muted/50 text-foreground'
            )}
          >
            {/* A red dot and a running clock before any audio is flowing would
                claim a recording that has not started. While arming, the bar
                says only that the press was received. */}
            {arming ? (
              <>
                <Loader2 className="h-3 w-3 shrink-0 animate-spin" aria-hidden="true" />
                <span className="shrink-0 text-xs text-muted-foreground">
                  Starting microphone...
                </span>
              </>
            ) : (
              <>
                <span
                  className={cn(
                    'h-2 w-2 shrink-0 rounded-full bg-destructive',
                    !cancelVoice && 'animate-pulse'
                  )}
                />
                <span className="w-10 shrink-0 tabular-nums font-medium">
                  {Math.floor(elapsed / 60)}:
                  {Math.floor(elapsed % 60)
                    .toString()
                    .padStart(2, '0')}
                </span>
                <span
                  className="flex h-5 flex-1 items-center justify-center gap-0.5"
                  aria-hidden="true"
                >
                  {[2, 4, 3, 5, 3, 4, 2].map((height, index) => (
                    <span
                      key={index}
                      className={cn(
                        'w-0.5 rounded-full bg-current opacity-60',
                        !cancelVoice && 'animate-pulse'
                      )}
                      style={{ height: `${height * 2}px`, animationDelay: `${index * 90}ms` }}
                    />
                  ))}
                </span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {cancelVoice ? 'Release to cancel' : 'Release to send'}
                </span>
              </>
            )}
          </div>
        ) : (
          <>
            <textarea
              ref={textareaRef}
              name="chat-message-input"
              aria-label={placeholder || 'Type a message'}
              data-lpignore="true"
              data-1p-ignore="true"
              data-bwignore="true"
              rows={1}
              value={text}
              onChange={handleChange}
              onKeyDown={handleKeyDown}
              placeholder={placeholder || 'Type a message...'}
              disabled={disabled || sending}
              className={cn(
                'flex-1 min-w-0 resize-none overflow-y-auto',
                'rounded-md border border-input bg-background px-3 py-2 text-base ring-offset-background',
                'placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2',
                'disabled:cursor-not-allowed disabled:opacity-50 md:text-sm'
              )}
              style={{ minHeight: '40px', maxHeight: '160px' }}
            />
          </>
        )}
        <Button
          type="submit"
          disabled={disabled || sending || voiceActive || !canSubmit}
          className="flex-shrink-0"
        >
          {sending ? 'Sending...' : 'Send'}
        </Button>
      </div>
      {recording && !cancelVoice && (
        <div className="text-center text-xs text-muted-foreground">Slide up to cancel</div>
      )}
      {showCharCounter && (
        <>
          <div className="hidden sm:flex items-center justify-end gap-2 text-xs">
            <span
              className={cn(
                'tabular-nums',
                limitState === 'error' || limitState === 'danger'
                  ? 'text-destructive font-medium'
                  : limitState === 'warning'
                    ? 'text-warning'
                    : 'text-muted-foreground'
              )}
            >
              {showCompressed && (
                <Shrink
                  className="inline h-3 w-3 -mt-0.5 mr-0.5 text-primary"
                  aria-label="compressed size"
                />
              )}
              {effectiveByteLen}/{limits!.hardLimit}
              {remaining < 0 && ` (${remaining})`}
            </span>
            {warningMessage && (
              <span className={cn(limitState === 'error' ? 'text-destructive' : 'text-warning')}>
                — {warningMessage}
              </span>
            )}
          </div>

          {(showMobileCounterValue || warningMessage) && (
            <div className="flex sm:hidden items-center justify-end gap-2 text-xs">
              {showMobileCounterValue && (
                <span
                  className={cn(
                    'tabular-nums',
                    limitState === 'error' || limitState === 'danger'
                      ? 'text-destructive font-medium'
                      : limitState === 'warning'
                        ? 'text-warning'
                        : 'text-muted-foreground'
                  )}
                >
                  {showCompressed && (
                    <Shrink
                      className="inline h-3 w-3 -mt-0.5 mr-0.5 text-primary"
                      aria-label="compressed size"
                    />
                  )}
                  {effectiveByteLen}/{limits!.hardLimit}
                  {remaining < 0 && ` (${remaining})`}
                </span>
              )}
              {warningMessage && (
                <span className={cn(limitState === 'error' ? 'text-destructive' : 'text-warning')}>
                  — {warningMessage}
                </span>
              )}
            </div>
          )}
        </>
      )}
    </form>
  );
});
