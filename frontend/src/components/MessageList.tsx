import {
  useEffect,
  useLayoutEffect,
  useRef,
  useCallback,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import type { Channel, Contact, Message, MessagePath, RadioConfig, RawPacket } from '../types';
import { CONTACT_TYPE_ROOM } from '../types';
import { api, type UnsupportedMediaStatus } from '../api';
import type { AeicSessionStatus } from '../api';
import {
  findLinkedChannelReferences,
  formatTime,
  parseSenderFromText,
} from '../utils/messageParser';
import {
  giphyUrlForId,
  parseGif,
  parseReaction,
  QUICK_REACTION_EMOJIS,
  REACTION_EMOJI_CATEGORIES,
  splitReplyMention,
} from '../utils/meshcoreOpenPayloads';
import { useRichPayloads } from '../contexts/RichPayloadContext';
import { usePathHopWidth } from '../contexts/PathHopWidthContext';
import { formatHopCounts, formatPathHopWidths, type SenderInfo } from '../utils/pathUtils';
import { getDirectContactRoute } from '../utils/pathUtils';
import { ContactAvatar } from './ContactAvatar';
import { PathModal } from './PathModal';
import { RawPacketInspectorDialog } from './RawPacketDetailModal';
import { toast } from './ui/sonner';
import { Button } from './ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';
import { handleKeyboardActivate } from '../utils/a11y';
import { useVirtualizer } from '@tanstack/react-virtual';
import { cn } from '@/lib/utils';
import { Download, ImageOff, Loader2, Play, RefreshCw, Sparkles, X } from 'lucide-react';
import { parseVoiceEnvelope } from '../utils/voiceEnvelope';
import { awaitMediaTransfer, imageSnapshot, voiceSnapshot } from '../utils/mediaTransfer';
import { estimateImageTransmitSeconds, parseImageEnvelope } from '../utils/imageEnvelope';
import {
  aeicApproxBitstreamBytes,
  aeicAspectRatio,
  parseAeicBinaryRef,
  parseAeicChunk,
  parseUnsupportedMediaRef,
  type AeicChunk,
} from '../utils/aeicEnvelope';

function ImageMessage({ message, content }: { message: Message; content: string }) {
  const envelope = parseImageEnvelope(content);
  // 'partial' is its own state, and not a flavour of 'unavailable': fragments that
  // did arrive are kept, and asking again re-requests only the gaps. Reporting that
  // as unavailable threw away real progress and told the person nothing was there.
  const [state, setState] = useState<'idle' | 'loading' | 'complete' | 'partial' | 'unavailable'>(
    message.outgoing ? 'complete' : 'idle'
  );
  const [received, setReceived] = useState(message.outgoing ? (envelope?.fragmentCount ?? 0) : 0);
  const [fullscreen, setFullscreen] = useState(false);
  if (!envelope) return null;

  const load = async () => {
    if (state === 'loading') return;
    const before = received;
    // Tracked alongside the state, because the `received` in this closure is the
    // value at render time. A poll that rejects halfway through leaves that at 0
    // while fragments are already stored, which would land in the catch below as
    // 'unavailable' and hide the retry that only asks for the gaps.
    let latest = received;
    setState('loading');
    try {
      const session = await awaitMediaTransfer({
        start: async () => imageSnapshot(await api.fetchImage(message.id)),
        poll: async (sessionId) => imageSnapshot(await api.getImageSession(sessionId)),
        onProgress: (snapshot) => {
          latest = snapshot.received;
          setReceived(snapshot.received);
        },
      });
      if (session.complete) {
        setState('complete');
        return;
      }
      setState(session.received > 0 ? 'partial' : 'unavailable');
      const missing = session.total - session.received;
      if (session.received > before) {
        toast.warning(`Image incomplete — ${missing} of ${session.total} parts missing`, {
          description: 'Tap the image to ask the sender for the missing parts.',
        });
      } else {
        toast.error(
          before > 0 ? 'No further image parts arrived' : 'Image parts are not arriving',
          {
            description:
              'The sender may be out of range or offline. The parts already here are kept, ' +
              'so retrying only asks for what is missing.',
          }
        );
      }
    } catch (error) {
      setState(latest > 0 ? 'partial' : 'unavailable');
      toast.error('Image message unavailable', {
        description: error instanceof Error ? error.message : String(error),
      });
    }
  };
  const contentUrl = api.imageContentUrl(envelope.sessionId);
  const label = `${envelope.width}×${envelope.height} · ${envelope.format === 0 ? 'AVIF' : 'JPEG'}`;
  return (
    <div className="w-48 max-w-full">
      {state === 'complete' ? (
        <button
          type="button"
          className="block overflow-hidden rounded-md"
          onClick={() => setFullscreen(true)}
          aria-label="Open image"
        >
          <img src={contentUrl} alt="Image message" className="aspect-square w-48 object-cover" />
        </button>
      ) : (
        <button
          type="button"
          onClick={() => void load()}
          disabled={state === 'loading'}
          aria-label={state === 'partial' ? 'Request the missing image parts' : 'Load image'}
          className="flex aspect-square w-48 flex-col items-center justify-center gap-2 rounded-md bg-muted/60 text-muted-foreground"
        >
          {state === 'loading' ? (
            <Loader2 className="animate-spin" size={30} />
          ) : state === 'unavailable' ? (
            <ImageOff size={30} />
          ) : state === 'partial' ? (
            <RefreshCw size={30} />
          ) : (
            <Download size={30} />
          )}
          <span
            className="px-2 text-center text-xs"
            title={
              state === 'loading' || state === 'partial'
                ? `${received} of ${envelope.fragmentCount} fragments received so far` +
                  (state === 'partial'
                    ? '. Retrying asks the sender only for the ones still missing.'
                    : '')
                : undefined
            }
          >
            {state === 'loading'
              ? `${received}/${envelope.fragmentCount}`
              : state === 'partial'
                ? `${envelope.fragmentCount - received} of ${envelope.fragmentCount} parts missing — tap to retry`
                : state === 'unavailable'
                  ? 'Unavailable — tap to retry'
                  : 'Tap to load'}
          </span>
        </button>
      )}
      <div
        className="mt-1 text-[0.6875rem] text-muted-foreground"
        title={
          `${label}, split across ${envelope.fragmentCount} mesh messages. ` +
          'The time is the estimated airtime for one hop to carry all of them — ' +
          'each further hop repeats that cost.'
        }
      >
        {label} · {envelope.fragmentCount} fragments · ~
        {Math.max(
          1,
          Math.round(estimateImageTransmitSeconds(envelope.fragmentCount, envelope.sizeBytes))
        )}
        s tx/hop
      </div>
      {fullscreen && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/90 p-4"
          role="dialog"
          aria-label="Image viewer"
          onClick={() => setFullscreen(false)}
        >
          <button
            type="button"
            className="absolute right-4 top-4 rounded-full bg-background/80 p-2 text-foreground"
            aria-label="Close image"
            onClick={() => setFullscreen(false)}
          >
            <X size={22} />
          </button>
          <img
            src={contentUrl}
            alt="Image message full size"
            className="max-h-full max-w-full object-contain"
            onClick={(event) => event.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
}

/**
 * A photo sent with the AEIC neural codec.
 *
 * Unlike the IE4 bubble there is nothing to fetch: the ~150-byte bitstream
 * already arrived inside these one-or-two text messages. What this waits on is
 * the *decode* — a ~5 s ONNX synthesis pass on the server — so it polls the
 * session rather than requesting fragments.
 *
 * Chunk 0 carries the metadata byte and owns the bubble; a later chunk renders
 * as a small chip so the conversation does not show a wall of basE91.
 */
function AeicImageMessage({
  message,
  chunk,
  sessionKey,
}: {
  message: Message;
  chunk?: AeicChunk;
  /**
   * The session named by an `aeib:` marker row, when this is one.
   *
   * Preferred over looking the session up by message id, because a
   * content-addressed session belongs to as many rows as there were sends of
   * that picture and can only be bound to the first of them. Resolving by
   * message id left every later bubble reporting no session at all.
   */
  sessionKey?: string | null;
}) {
  const [session, setSession] = useState<AeicSessionStatus | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'decoded' | 'unavailable'>('idle');
  const [detail, setDetail] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);

  // Every poll carries a cancellation token owned by the effect that started
  // it. The list is virtualized, so these bubbles unmount whenever they scroll
  // out of view — without a token the 60-iteration loop below kept running,
  // firing a request a second per off-screen image and setting state on a dead
  // component, and scrolling back in started a second loop on top.
  const tokenRef = useRef<{ cancelled: boolean }>({ cancelled: false });

  const poll = useCallback(
    async (token: { cancelled: boolean }) => {
      setState('loading');
      setDetail(null);
      try {
        let current = sessionKey
          ? await api.getAeicSession(sessionKey)
          : await api.getAeicSessionForMessage(message.id);
        if (token.cancelled) return;
        setSession(current);
        // The synthesis pass is seconds, not milliseconds, and it is queued behind
        // any other decode on the server.
        for (let attempt = 0; !current.decoded && attempt < 60; attempt += 1) {
          if (current.decode_error) break;
          await new Promise((resolve) => window.setTimeout(resolve, 1000));
          if (token.cancelled) return;
          current = await api.getAeicSession(current.session_key);
          if (token.cancelled) return;
          setSession(current);
        }
        if (current.decoded) {
          setState('decoded');
          return;
        }
        setState('unavailable');
        setDetail(
          current.decode_error ??
            (current.received_chunks < current.total_chunks
              ? `Waiting for part ${current.received_chunks + 1} of ${current.total_chunks}`
              : 'Not decoded yet')
        );
      } catch (error) {
        if (token.cancelled) return;
        setState('unavailable');
        setDetail(error instanceof Error ? error.message : String(error));
      }
    },
    [message.id, sessionKey]
  );

  // An inbound image decodes on its own as soon as it is reassembled, so look
  // once on mount rather than making the reader tap to find out.
  useEffect(() => {
    const token = { cancelled: false };
    tokenRef.current = token;
    void poll(token);
    return () => {
      token.cancelled = true;
    };
  }, [poll]);

  const retryDecode = useCallback(async () => {
    if (!session) return;
    setState('loading');
    try {
      const refreshed = await api.retryAeicDecode(session.session_key);
      setSession(refreshed);
      setState(refreshed.decoded ? 'decoded' : 'unavailable');
      if (!refreshed.decoded) setDetail(refreshed.decode_error ?? 'Not decoded yet');
    } catch (error) {
      setState('unavailable');
      setDetail(error instanceof Error ? error.message : String(error));
      toast.error('Could not decode image', {
        description: error instanceof Error ? error.message : String(error),
      });
    }
  }, [session]);

  const contentUrl = session ? api.aeicContentUrl(session.session_key) : null;
  const ratio = aeicAspectRatio(session?.aspect_code ?? chunk?.aspectCode ?? null);
  const approxBytes =
    session?.bitstream_bytes || (chunk ? aeicApproxBitstreamBytes(chunk.payloadChars) : 0);
  const square = session?.square_size ?? chunk?.squareSize ?? 512;
  // A binary-transport image has no chunk to count; its packet count is a
  // property of the GRP_DATA stream the server already reassembled.
  const parts = chunk?.total ?? session?.total_chunks ?? 1;

  return (
    <div className="w-48 max-w-full">
      {state === 'decoded' && contentUrl ? (
        <button
          type="button"
          className="block overflow-hidden rounded-md"
          onClick={() => setFullscreen(true)}
          aria-label="Open AI-reconstructed image"
        >
          <img
            src={contentUrl}
            alt="AI-reconstructed image message"
            className="w-48 object-cover"
            style={{ aspectRatio: String(ratio) }}
          />
        </button>
      ) : (
        <button
          type="button"
          onClick={() =>
            void (state === 'unavailable' && session ? retryDecode() : poll(tokenRef.current))
          }
          disabled={state === 'loading'}
          aria-label="Reconstruct image"
          className="flex aspect-square w-48 flex-col items-center justify-center gap-2 rounded-md bg-muted/60 px-2 text-center text-muted-foreground"
        >
          {state === 'loading' ? (
            <Loader2 className="animate-spin" size={30} />
          ) : state === 'unavailable' ? (
            <ImageOff size={30} />
          ) : (
            <Sparkles size={30} />
          )}
          <span className="text-xs leading-snug">
            {state === 'loading'
              ? session && session.received_chunks < session.total_chunks
                ? `${session.received_chunks}/${session.total_chunks} parts`
                : 'Reconstructing…'
              : state === 'unavailable'
                ? (detail ?? 'Unavailable')
                : 'Tap to reconstruct'}
          </span>
        </button>
      )}
      <div
        className="mt-1 text-[0.6875rem] text-muted-foreground"
        title={
          `${square}×${square} pixels, encoded by the AI image codec into ` +
          `${approxBytes} bytes and carried as ${parts} mesh message${parts === 1 ? '' : 's'}`
        }
      >
        {square}px AI · {approxBytes} B · {parts} msg{parts === 1 ? '' : 's'}
      </div>
      {fullscreen && contentUrl && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/90 p-4"
          role="dialog"
          aria-label="Image viewer"
          onClick={() => setFullscreen(false)}
        >
          <button
            type="button"
            className="absolute right-4 top-4 rounded-full bg-background/80 p-2 text-foreground"
            aria-label="Close image"
            onClick={() => setFullscreen(false)}
          >
            <X size={22} />
          </button>
          <img
            src={contentUrl}
            alt="AI-reconstructed image, full size"
            className="max-h-full max-w-full object-contain"
            onClick={(event) => event.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
}

/**
 * Media that arrived in a codec this server cannot decode, and kept.
 *
 * The box exists because the alternative was silence: an image someone sent was
 * identified, refused and dropped, with nothing in the conversation to say a
 * picture had been sent at all. The bytes are stored, so this is not a tombstone
 * -- retry is wired to a real endpoint, and the same button turns an image
 * received today into a picture on the day a decoder for its codec ships.
 */
function UnsupportedMediaMessage({ mediaId }: { mediaId: number }) {
  const [status, setStatus] = useState<UnsupportedMediaStatus | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const next = await api.getUnsupportedMedia(mediaId);
        if (!cancelled) setStatus(next);
      } catch {
        // The box still says something useful without it; see the fallback below.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mediaId]);

  const retry = async () => {
    setBusy(true);
    try {
      const next = await api.retryUnsupportedMediaDecode(mediaId);
      setStatus(next);
      if (!next.decoded) {
        toast.info('Still no decoder for this format', { description: next.reason });
      }
    } catch (error) {
      toast.error('Could not decode the picture', {
        description: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setBusy(false);
    }
  };

  const label = status?.codec_label ?? 'an unsupported format';
  return (
    <div className="w-56 max-w-full rounded-md border border-border bg-muted/40 p-2.5">
      <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
        <ImageOff size={14} className="flex-shrink-0" />
        <span>Picture not shown</span>
      </div>
      <p className="mt-1 text-xs leading-snug text-muted-foreground">
        {status?.reason ?? `A picture arrived in ${label}, which this server cannot decode.`}
      </p>
      {status && status.blob_count > 0 && (
        <p
          className="mt-1 text-[0.6875rem] text-muted-foreground"
          title="Kept exactly as it arrived, in packet order, so a decoder added later can read it."
        >
          {status.total_bytes} bytes kept in {status.blob_count} packet
          {status.blob_count === 1 ? '' : 's'}
        </p>
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="mt-2 h-7 w-full text-xs"
        disabled={busy}
        onClick={() => void retry()}
      >
        {busy ? <Loader2 className="animate-spin" size={13} /> : <RefreshCw size={13} />}
        <span className="ml-1.5">Retry decoding</span>
      </Button>
    </div>
  );
}

/** A continuation chunk of an AEIC image; the picture itself hangs off chunk 0. */
function AeicImagePart({ chunk }: { chunk: AeicChunk }) {
  return (
    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <Sparkles size={13} className="flex-shrink-0" />
      <span>
        Image part {chunk.index + 1} of {chunk.total}
      </span>
    </div>
  );
}

function VoiceMessage({ message, content }: { message: Message; content: string }) {
  const envelope = parseVoiceEnvelope(content);
  const duration = envelope?.durationSeconds ?? 0;
  const [state, setState] = useState<'idle' | 'loading' | 'partial' | 'unavailable'>('idle');
  const [progress, setProgress] = useState(0);
  const [missing, setMissing] = useState(0);

  if (!envelope) return null;
  const play = async () => {
    if (state === 'loading') return;
    // See ImageMessage: the rendered progress is not readable from the catch below.
    let latest = { received: 0, total: 0 };
    setState('loading');
    try {
      const session = await awaitMediaTransfer({
        start: async () => voiceSnapshot(await api.fetchVoice(message.id)),
        poll: async (sessionId) => voiceSnapshot(await api.getVoiceSession(sessionId)),
        onProgress: (snapshot) => {
          latest = { received: snapshot.received, total: snapshot.total };
          setProgress(snapshot.total ? snapshot.received / snapshot.total : 0);
        },
      });
      if (!session.complete) {
        // A recording cannot be played with holes in it, so unlike a picture there
        // is nothing to show for a partial transfer -- but the count still belongs
        // on screen, because it is the difference between "nearly here, ask again"
        // and "nothing is coming".
        setMissing(session.total - session.received);
        setState(session.received > 0 ? 'partial' : 'unavailable');
        toast.error(
          session.received > 0
            ? `Voice note incomplete — ${session.total - session.received} of ${session.total} parts missing`
            : 'Voice note parts are not arriving',
          {
            description:
              'Tap again to ask the sender for the missing parts; the ones already here are kept.',
          }
        );
        return;
      }
      const audio = new Audio(api.voiceAudioUrl(session.sessionId));
      await audio.play();
      setState('idle');
    } catch (error) {
      setMissing(Math.max(0, latest.total - latest.received));
      setState(latest.received > 0 ? 'partial' : 'unavailable');
      toast.error('Voice message unavailable', {
        description: error instanceof Error ? error.message : String(error),
      });
    }
  };
  return (
    <button
      type="button"
      onClick={() => void play()}
      disabled={state === 'loading'}
      className="flex min-w-40 items-center gap-2"
      aria-label={`Play ${duration} second voice message`}
    >
      {state === 'loading' ? (
        <Loader2 className="animate-spin" size={18} />
      ) : state === 'partial' ? (
        <RefreshCw size={18} />
      ) : (
        <Play size={18} />
      )}
      <span>
        {state === 'unavailable'
          ? 'Unavailable'
          : state === 'partial'
            ? `${missing} part${missing === 1 ? '' : 's'} missing — tap to retry`
            : state === 'loading'
              ? `Fetching ${Math.round(progress * 100)}%`
              : `${duration}s voice message`}
      </span>
    </button>
  );
}

interface MessageListProps {
  messages: Message[];
  contacts: Contact[];
  channels?: Channel[];
  loading: boolean;
  loadingOlder?: boolean;
  hasOlderMessages?: boolean;
  /** Id of the oldest unread message, from the server. Null when nothing is unread. */
  unreadMarkerMessageId?: number | null;
  onDismissUnreadMarker?: () => void;
  /** Called when the unread boundary is not in loaded history and must be jumped to. */
  onNavigateToUnread?: (messageId: number) => void;
  onSenderClick?: (sender: string) => void;
  onLoadOlder?: () => void;
  onResendChannelMessage?: (messageId: number, newTimestamp?: boolean) => void;
  /** Retransmit an outgoing message. `newTimestamp` applies to channel messages only. */
  onRetryMessage?: (message: Message, newTimestamp?: boolean) => void;
  /** Stop the transmissions not yet made for an outgoing message. */
  onCancelMessage?: (message: Message) => void;
  /** Drop the message from our history. Local only -- the mesh has no unsend. */
  onDeleteMessage?: (message: Message) => void;
  /** Send a MeshCore Open Advanced compatible emoji reaction to a message. */
  onReactToMessage?: (message: Message, emoji: string) => void | Promise<void>;
  onChannelReferenceClick?: (channelName: string) => void;
  radioName?: string;
  config?: RadioConfig | null;
  onOpenContactInfo?: (publicKey: string, fromChannel?: boolean) => void;
  targetMessageId?: number | null;
  onTargetReached?: () => void;
  hasNewerMessages?: boolean;
  loadingNewer?: boolean;
  onLoadNewer?: () => void;
  onJumpToBottom?: () => void;
  preSorted?: boolean;
}

// Renders a MeshCore Open GIF payload, falling back to the raw text on load error.
function GifPayload({ gifId, rawText }: { gifId: string; rawText: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return <>{rawText}</>;
  }
  const url = giphyUrlForId(gifId);
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-block"
      title="Open GIF on Giphy"
    >
      <img
        src={url}
        alt="GIF"
        loading="lazy"
        onError={() => setFailed(true)}
        className="max-w-[240px] max-h-[240px] rounded-md"
      />
    </a>
  );
}

// Renders a MeshCore Open reaction generically (emoji + "reacted"); the target
// message is not resolved (see issue #291).
function ReactionPayload({ emoji }: { emoji: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-xl leading-none">{emoji}</span>
      <span className="text-xs text-muted-foreground italic">reacted</span>
    </span>
  );
}

// Render a bare payload body (no reply prefix) into its rich node, or null.
function renderPayloadBody(body: string): ReactNode | null {
  const gifId = parseGif(body);
  if (gifId) {
    return <GifPayload gifId={gifId} rawText={body} />;
  }
  const reaction = parseReaction(body);
  if (reaction) {
    return <ReactionPayload emoji={reaction.emoji} />;
  }
  return null;
}

// Recognize a MeshCore Open payload and render it. Handles both a whole-message
// payload ("g:<id>") and a reply-prefixed one ("@[Name] g:<id>") — the form
// meshcore-open sends when a GIF/reaction is a reply, which otherwise renders as
// raw text (issue #291). Returns null when the content is not a recognized
// payload, so the caller renders normally.
function renderMeshcoreOpenPayload(
  content: string,
  radioName?: string,
  onChannelReferenceClick?: (channelName: string) => void
): ReactNode | null {
  const whole = renderPayloadBody(content);
  if (whole) return whole;

  const split = splitReplyMention(content);
  if (split) {
    const body = renderPayloadBody(split.body);
    if (body) {
      // Preserve the reply mention (rendered as a normal @[Name] mention) so the
      // GIF/reaction still reads as a reply to that person.
      return (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          {renderTextWithMentions(split.mention, radioName, onChannelReferenceClick)}
          {body}
        </span>
      );
    }
  }
  return null;
}

/**
 * Starting guess for an unmeasured row: a single-line message with its header.
 * Rows are measured for real once they scroll into view.
 */
const ESTIMATED_MESSAGE_HEIGHT = 64;

/** Stand-in viewport height for when the scroll container cannot be measured. */
const FALLBACK_VIEWPORT_HEIGHT = 800;
/**
 * Frames a pending bottom-pin may re-assert itself for. Row heights start as
 * estimates and converge over the first few measurement passes, so the pin has to
 * outlive them; the budget stops a list that can never reach the bottom (a
 * container stuck at zero height) from re-scrolling forever.
 */
const BOTTOM_SCROLL_FRAME_BUDGET = 20;

// URL regex for linkifying plain text
const URL_PATTERN =
  /https?:\/\/(www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_+.~#?&//=]*)/g;

function renderChannelReferences(
  text: string,
  keyPrefix: string,
  onChannelReferenceClick?: (channelName: string) => void
): ReactNode[] {
  const references = findLinkedChannelReferences(text);
  if (references.length === 0) {
    return [text];
  }

  const parts: ReactNode[] = [];
  let lastIndex = 0;

  references.forEach((reference, index) => {
    if (reference.start > lastIndex) {
      parts.push(text.slice(lastIndex, reference.start));
    }

    const className =
      'rounded px-0.5 font-medium text-primary underline underline-offset-2 transition-colors';
    if (onChannelReferenceClick) {
      parts.push(
        <button
          key={`${keyPrefix}-channel-${index}`}
          type="button"
          className={cn(
            className,
            'inline border-0 bg-transparent p-0 align-baseline hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring'
          )}
          onClick={() => onChannelReferenceClick(reference.label)}
        >
          {reference.label}
        </button>
      );
    } else {
      parts.push(
        <span key={`${keyPrefix}-channel-${index}`} className={className}>
          {reference.label}
        </span>
      );
    }

    lastIndex = reference.end;
  });

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts;
}

// Helper to convert URLs and channel references in a plain text string into rich content
function linkifyText(
  text: string,
  keyPrefix: string,
  onChannelReferenceClick?: (channelName: string) => void
): ReactNode[] {
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let keyIndex = 0;

  URL_PATTERN.lastIndex = 0;
  while ((match = URL_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(
        ...renderChannelReferences(
          text.slice(lastIndex, match.index),
          `${keyPrefix}-text-${keyIndex}`,
          onChannelReferenceClick
        )
      );
    }
    parts.push(
      <a
        key={`${keyPrefix}-link-${keyIndex++}`}
        href={match[0]}
        target="_blank"
        rel="noopener noreferrer"
        className="text-primary underline hover:text-primary/80"
      >
        {match[0]}
      </a>
    );
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex === 0) {
    return renderChannelReferences(text, keyPrefix, onChannelReferenceClick);
  }
  if (lastIndex < text.length) {
    parts.push(
      ...renderChannelReferences(
        text.slice(lastIndex),
        `${keyPrefix}-tail`,
        onChannelReferenceClick
      )
    );
  }
  return parts;
}

// Helper to render text with highlighted @[Name] mentions and clickable URLs
function renderTextWithMentions(
  text: string,
  radioName?: string,
  onChannelReferenceClick?: (channelName: string) => void
): ReactNode {
  const mentionPattern = /@\[([^\]]+)\]/g;
  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let keyIndex = 0;

  while ((match = mentionPattern.exec(text)) !== null) {
    // Add text before the match (with linkification)
    if (match.index > lastIndex) {
      parts.push(
        ...linkifyText(
          text.slice(lastIndex, match.index),
          `pre-${keyIndex}`,
          onChannelReferenceClick
        )
      );
    }

    const mentionedName = match[1];
    const isOwnMention = radioName ? mentionedName === radioName : false;

    parts.push(
      <span
        key={`mention-${keyIndex++}`}
        className={cn(
          'rounded px-0.5',
          isOwnMention ? 'bg-primary/30 text-primary font-medium' : 'bg-muted-foreground/20'
        )}
      >
        @[{mentionedName}]
      </span>
    );

    lastIndex = match.index + match[0].length;
  }

  // Add remaining text after last match (with linkification)
  if (lastIndex < text.length) {
    parts.push(...linkifyText(text.slice(lastIndex), `post-${keyIndex}`, onChannelReferenceClick));
  }

  return parts.length > 0 ? parts : text;
}

// Clickable hop count badge that opens the path modal
interface HopCountBadgeProps {
  paths: MessagePath[];
  onClick: () => void;
  /** Our own message, so the paths are echoes coming back rather than an arrival. */
  outgoing?: boolean;
}

/** One entry of the hop badge in words: "direct", "1 hop", "3 hops". */
function describeHopCount(part: string): string {
  if (part === 'd') return 'direct';
  return part === '1' ? '1 hop' : `${part} hops`;
}

/**
 * Spell out what the hop badge's digits mean.
 *
 * "(d/2/3)" is compact enough to be unreadable if you do not already know the
 * convention, so the tooltip names every number instead of only advertising the
 * click. One entry per receive path, ascending.
 */
function hopCountTitle(display: string, widthLabel: string | null, outgoing: boolean): string {
  const parts = display.split('/');
  const readable = parts.map(describeHopCount);
  const width = widthLabel ? `, ${widthLabel} per hop` : '';

  let summary: string;
  if (parts.length > 1) {
    const verb = outgoing ? 'Echoed back' : 'Heard';
    summary = `${verb} ${parts.length} times: ${readable.join(', ')}`;
  } else {
    const verb = outgoing ? 'Echoed back' : 'Arrived';
    summary =
      readable[0] === 'direct'
        ? `${verb} direct, with no repeaters in between`
        : `${verb} over ${readable[0]}`;
  }
  return `${summary}${width} — click to see the route`;
}

function HopCountBadge({ paths, onClick, outgoing = false }: HopCountBadgeProps) {
  const { showPathHopWidth } = usePathHopWidth();
  const hopInfo = formatHopCounts(paths);
  const widthLabel = showPathHopWidth ? formatPathHopWidths(paths) : null;
  const label = widthLabel ? `(${hopInfo.display} · ${widthLabel})` : `(${hopInfo.display})`;
  const title = hopCountTitle(hopInfo.display, widthLabel, outgoing);

  return (
    <span
      className="text-[0.625rem] text-muted-foreground cursor-pointer hover:text-primary hover:underline"
      role="button"
      tabIndex={0}
      onKeyDown={handleKeyboardActivate}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      title={title}
      aria-label={title}
    >
      {label}
    </span>
  );
}

// Region scope badge for messages that arrived via a transport-routed (region-scoped) packet.
function RegionBadge({ region }: { region: string }) {
  return (
    <span
      className="align-middle text-[0.625rem] uppercase tracking-wider px-1.5 py-0.5 rounded bg-muted text-muted-foreground"
      title={`Region-scoped flood: only repeaters configured for "${region}" relay this`}
    >
      {region}
    </span>
  );
}

/**
 * Where an outgoing message's send got to, for display. `delivered` wins over
 * everything: an ACK that lands after the attempts ran out still means it
 * arrived. `unknown` covers rows stored before send tracking existed.
 */
type DisplaySendStatus = 'delivered' | 'sending' | 'sent' | 'failed' | 'canceled' | 'unknown';

function displaySendStatus(msg: Message): DisplaySendStatus {
  if (msg.acked > 0) return 'delivered';
  if (!msg.send_state) return 'unknown';
  return msg.send_state;
}

const SEND_STATUS_GLYPHS: Record<DisplaySendStatus, string> = {
  delivered: '✓✓',
  sent: '✓',
  sending: '···',
  failed: '✕',
  canceled: '⊘',
  unknown: '?',
};

/**
 * The compression badge, e.g. "53% mcmp3". Null when the body rode as plain text
 * (MCMP off for the conversation, or v2's "only if smaller" gate declined).
 *
 * The percentage is measured the way MCO Advanced measures it -- over the
 * compressed-text segment, which for v3 excludes the container header -- so the
 * two clients quote the same number for the same message. The true on-air size
 * lives in the tooltip, where it cannot be mistaken for the ratio.
 */
function compressionLabel(msg: Message): string | null {
  if (!msg.compression) return null;
  const plainBytes = msg.plain_bytes ?? 0;
  const ratioBytes = msg.payload_bytes ?? msg.wire_bytes ?? 0;
  if (plainBytes <= 0) return msg.compression;
  const saved = Math.max(
    0,
    Math.min(100, Math.round(((plainBytes - ratioBytes) * 100) / plainBytes))
  );
  return `${saved}% ${msg.compression}`;
}

function compressionTitle(msg: Message): string | undefined {
  if (!msg.compression) return undefined;
  const version = msg.compression === 'mcmp3' ? 'v3' : 'v2';
  const parts = [`Compressed with MCMP ${version}`];
  if (msg.plain_bytes != null && msg.wire_bytes != null) {
    parts.push(`${msg.plain_bytes} B of text went out as ${msg.wire_bytes} B on air`);
  }
  if (
    msg.compression === 'mcmp3' &&
    msg.payload_bytes != null &&
    msg.wire_bytes != null &&
    msg.payload_bytes !== msg.wire_bytes
  ) {
    parts.push(
      `the percentage covers the ${msg.payload_bytes} B of compressed text, excluding the v3 container`
    );
  }
  return parts.join(' — ');
}

const SEND_STATUS_TITLES: Record<DisplaySendStatus, string> = {
  // One tick versus two is the WhatsApp convention, and it is *relative* -- it
  // only means anything if you know the other mark exists. So each tooltip names
  // its own tick count instead of leaving the reader to infer the scale from a
  // mark they may never have seen. Meaning first, glyph second, so the sentence
  // still reads sensibly to a screen reader that cannot see either.
  delivered: 'Delivered (two ticks)',
  sent: 'Sent (one tick) — the radio accepted it, but nothing has confirmed it yet',
  sending: 'Sending — still retrying',
  failed: 'Out of attempts without an acknowledgement',
  canceled: 'Sending cancelled',
  // Refined by conversation type in sendStatusTitle; this is the neutral fallback.
  unknown: 'No confirmation heard yet',
};

/**
 * What the status glyph means, including the number stuck to it.
 *
 * A bare "✓✓3" says nothing about what was counted, and the answer differs by
 * conversation: on a channel each count is a repeater that echoed us back, on a
 * direct message it is an acknowledgement from the recipient.
 */
function sendStatusTitle(message: Message, status: DisplaySendStatus): string {
  const base = SEND_STATUS_TITLES[status];
  if (status === 'unknown') {
    // A channel message is confirmed by repeater echoes, a direct one by an ACK
    // from the recipient -- saying "repeats" on a DM would name the wrong thing.
    return message.type === 'CHAN'
      ? 'No repeater echoes heard yet'
      : 'No acknowledgement heard yet';
  }
  if (status !== 'delivered') return base;
  if (message.type === 'CHAN') {
    const count = Math.max(1, message.acked);
    return `${base} — ${count} repeater echo${count === 1 ? '' : 'es'} heard`;
  }
  return `${base} — acknowledged by the recipient`;
}

/** The time, said in full and labelled by direction. */
function timestampTitle(message: Message): string {
  const stamp = new Date(message.received_at * 1000).toLocaleString();
  return `${message.outgoing ? 'Sent' : 'Received'} ${stamp}`;
}

/** Both halves of the "2/3" counter, and where the limit comes from. */
function attemptsTitle(attempts: number, maxAttempts: number): string {
  return (
    `Transmitted ${attempts} time${attempts === 1 ? '' : 's'}, ` +
    `up to ${maxAttempts} allowed for this message ` +
    '(Settings › Radio › Direct Message Send Attempts)'
  );
}

/**
 * The small line under every message: when it happened, how it travelled, and --
 * for our own messages -- how the send went. MCO Advanced puts the same facts in
 * one row rather than scattering them around the bubble, which keeps the bubble
 * itself readable as the message grows a codec badge and an attempt counter.
 */
function MessageMetaLine({
  message,
  paths,
  onShowPaths,
  onOpenActions,
}: {
  message: Message;
  paths: MessagePath[] | null;
  onShowPaths?: () => void;
  onOpenActions?: () => void;
}) {
  const status = message.outgoing ? displaySendStatus(message) : null;
  const compression = compressionLabel(message);
  const attempts = message.send_attempts ?? 0;
  const maxAttempts = message.send_max_attempts ?? 0;
  // Only worth saying once a retry has actually happened -- "1 of 3" on every
  // message would be noise.
  const showAttempts = attempts > 1 && maxAttempts > 0;
  // Multiple echoes on a channel message are worth showing as a count: each one
  // is a separate repeater that heard us.
  const glyph = status
    ? SEND_STATUS_GLYPHS[status] +
      (status === 'delivered' && message.acked > 1 ? message.acked : '')
    : null;
  const statusTitle = status ? sendStatusTitle(message, status) : null;

  return (
    <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-[0.625rem] leading-tight text-muted-foreground">
      <span title={timestampTitle(message)}>{formatTime(message.received_at)}</span>
      {paths && paths.length > 0 && onShowPaths && (
        <HopCountBadge paths={paths} onClick={onShowPaths} outgoing={message.outgoing} />
      )}
      {message.region && <RegionBadge region={message.region} />}
      {status &&
        glyph &&
        statusTitle &&
        (onShowPaths ? (
          <span
            className="cursor-pointer hover:text-primary"
            role="button"
            tabIndex={0}
            onKeyDown={handleKeyboardActivate}
            onClick={(e) => {
              e.stopPropagation();
              onShowPaths();
            }}
            title={`${statusTitle} — click to see the delivery detail`}
            aria-label={`${statusTitle}, click to see the delivery detail`}
          >
            {glyph}
          </span>
        ) : (
          <span title={statusTitle} aria-label={statusTitle}>
            {glyph}
          </span>
        ))}
      {showAttempts && (
        <span title={attemptsTitle(attempts, maxAttempts)}>
          {attempts}/{maxAttempts}
        </span>
      )}
      {compression && (
        <span className="font-mono" title={compressionTitle(message)}>
          {compression}
        </span>
      )}
      {onOpenActions && (
        <button
          type="button"
          className="ml-auto px-1 leading-none hover:text-primary"
          onClick={(e) => {
            e.stopPropagation();
            onOpenActions();
          }}
          title="Message actions — copy, retry, cancel or delete"
          aria-label="Message actions"
        >
          ⋯
        </button>
      )}
    </div>
  );
}

/**
 * Per-message actions. A dialog rather than an inline popover: the list is
 * virtualized, so a popover anchored inside a row gets clipped by the scroll
 * container -- and a centred sheet is the same interaction on phone and desktop.
 *
 * Cancel is offered only while transmissions are still scheduled; delete is
 * always offered and cancels as a side effect, which is what "delete" has to mean
 * for a message we might otherwise keep re-transmitting.
 */
function MessageActionsDialog({
  message,
  onClose,
  onCopy,
  onRetry,
  onCancel,
  onDelete,
  onReact,
}: {
  message: Message;
  onClose: () => void;
  onCopy: (message: Message) => void | Promise<void>;
  onRetry?: (message: Message, newTimestamp?: boolean) => void;
  onCancel?: (message: Message) => void;
  onDelete?: (message: Message) => void;
  /** Present only when this message can carry a reaction. */
  onReact?: (message: Message, emoji: string) => void | Promise<void>;
}) {
  const status = message.outgoing ? displaySendStatus(message) : null;
  const canCancel = !!onCancel && status === 'sending';
  const canRetry = !!onRetry && message.outgoing;
  const [showAllEmojis, setShowAllEmojis] = useState(false);

  const run = (action: () => void | Promise<void>) => () => {
    onClose();
    void action();
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Message actions</DialogTitle>
          <DialogDescription className="line-clamp-2 break-words">{message.text}</DialogDescription>
        </DialogHeader>
        {onReact && (
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-1">
              {QUICK_REACTION_EMOJIS.map((emoji) => (
                <button
                  key={emoji}
                  type="button"
                  className="flex h-9 w-9 items-center justify-center rounded-full text-xl transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={`React with ${emoji}`}
                  onClick={run(() => onReact(message, emoji))}
                >
                  {emoji}
                </button>
              ))}
              <button
                type="button"
                className="flex h-9 w-9 items-center justify-center rounded-full text-sm text-muted-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={showAllEmojis ? 'Hide emoji list' : 'More emojis'}
                aria-expanded={showAllEmojis}
                onClick={() => setShowAllEmojis((prev) => !prev)}
              >
                {showAllEmojis ? <X className="h-4 w-4" /> : '⋯'}
              </button>
            </div>
            {showAllEmojis && (
              <div className="max-h-48 overflow-y-auto rounded-md border border-border p-2">
                {REACTION_EMOJI_CATEGORIES.map((category) => (
                  <div key={category.label}>
                    <div className="px-1 pb-1 pt-2 text-xs font-medium text-muted-foreground first:pt-0">
                      {category.label}
                    </div>
                    <div className="flex flex-wrap">
                      {category.emojis.map((emoji, i) => (
                        <button
                          key={`${category.label}-${i}`}
                          type="button"
                          className="flex h-8 w-8 items-center justify-center rounded text-lg transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          aria-label={`React with ${emoji}`}
                          onClick={run(() => onReact(message, emoji))}
                        >
                          {emoji}
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="flex flex-col gap-2">
          <Button variant="outline" onClick={run(() => onCopy(message))}>
            Copy text
          </Button>
          {canRetry && (
            <Button
              variant="outline"
              onClick={run(() =>
                // A channel message cannot be retried byte-perfect outside its
                // 30s dedup window, so a manual channel retry always goes out
                // under a fresh timestamp as a new packet.
                onRetry!(message, message.type === 'CHAN')
              )}
            >
              Retry sending
            </Button>
          )}
          {canCancel && (
            <Button variant="outline" onClick={run(() => onCancel!(message))}>
              Cancel sending
            </Button>
          )}
          {onDelete && (
            <Button variant="destructive" onClick={run(() => onDelete(message))}>
              Delete from history
            </Button>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          Retry and cancel only affect what we transmit next — anything already on air cannot be
          recalled, and deleting removes our copy only.
        </p>
      </DialogContent>
    </Dialog>
  );
}

const RESEND_WINDOW_SECONDS = 30;
const CORRUPT_SENDER_LABEL = '<No name -- corrupt packet?>';
const ANALYZE_PACKET_NOTICE =
  'This analyzer shows one stored full packet copy only. When multiple receives have identical payloads, the backend deduplicates them to a single stored packet and appends any additional receive paths onto the message path history instead of storing multiple full packet copies.';

function hasUnexpectedControlChars(text: string): boolean {
  for (const char of text) {
    const code = char.charCodeAt(0);
    if (
      (code >= 0 && code <= 8) ||
      code === 11 ||
      code === 12 ||
      (code >= 14 && code <= 31) ||
      code === 127
    ) {
      return true;
    }
  }
  return false;
}

export function MessageList({
  messages,
  contacts,
  channels = [],
  loading,
  loadingOlder = false,
  hasOlderMessages = false,
  unreadMarkerMessageId,
  onDismissUnreadMarker,
  onNavigateToUnread,
  onSenderClick,
  onLoadOlder,
  onResendChannelMessage,
  onRetryMessage,
  onCancelMessage,
  onDeleteMessage,
  onReactToMessage,
  onChannelReferenceClick,
  radioName,
  config,
  onOpenContactInfo,
  targetMessageId,
  onTargetReached,
  hasNewerMessages = false,
  loadingNewer = false,
  onLoadNewer,
  onJumpToBottom,
  preSorted = false,
}: MessageListProps) {
  const { renderRichPayloads } = useRichPayloads();
  const listRef = useRef<HTMLDivElement>(null);
  const prevMessagesLengthRef = useRef<number>(0);
  const isInitialLoadRef = useRef<boolean>(true);
  // A pending request to pin the list to the newest message.
  //
  // Rows are measured lazily, so the total size at mount is a guess built from
  // estimates. A single scrollToIndex against that guess gets undone by the
  // measurement passes that follow — and under StrictMode's double-invoked
  // effects it is undone completely, leaving the view stranded at the top. So
  // the request is held open and re-asserted as measurements land, rather than
  // fired once and marked done.
  const pendingBottomScrollRef = useRef(false);
  const [bottomScrollNonce, setBottomScrollNonce] = useState(0);
  const virtualSpacerRef = useRef<HTMLDivElement>(null);
  // Distance from the scroll container's content origin down to the first row.
  // Non-zero because the container carries p-4 and can show a loading/older
  // banner above the rows; see the scrollMargin note on the virtualizer.
  const [scrollMargin, setScrollMargin] = useState(0);
  const [showScrollToBottom, setShowScrollToBottom] = useState(false);
  const [selectedPath, setSelectedPath] = useState<{
    paths: MessagePath[];
    senderInfo: SenderInfo;
    messageId?: number;
    packetId?: number | null;
    isOutgoingChan?: boolean;
  } | null>(null);
  const [resendableIds, setResendableIds] = useState<Set<number>>(new Set());
  const [actionsTarget, setActionsTarget] = useState<Message | null>(null);
  const resendTimersRef = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());
  const packetCacheRef = useRef<Map<number, RawPacket>>(new Map());
  const packetSignalOverrideRef = useRef<{ rssi: number | null; snr: number | null } | undefined>(
    undefined
  );
  const [packetInspectorSource, setPacketInspectorSource] = useState<
    | { kind: 'packet'; packet: RawPacket }
    | { kind: 'loading'; message: string }
    | { kind: 'unavailable'; message: string }
    | null
  >(null);
  const [highlightedMessageId, setHighlightedMessageId] = useState<number | null>(null);
  const [showJumpToUnread, setShowJumpToUnread] = useState(false);
  const [jumpToUnreadDismissed, setJumpToUnreadDismissed] = useState(false);
  const targetScrolledRef = useRef(false);
  const unreadMarkerRef = useRef<HTMLButtonElement | HTMLDivElement | null>(null);

  // Capture scroll state in the scroll handler BEFORE any state updates
  const scrollStateRef = useRef({
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    wasNearTop: false,
    wasNearBottom: true, // Default to true so initial messages scroll to bottom
  });

  // Track conversation key to detect when entire message set changes
  const prevConvKeyRef = useRef<string | null>(null);

  const handleAnalyzePacket = useCallback(async (message: Message) => {
    // Extract signal from the first path if available
    const firstPath = message.paths?.[0];
    packetSignalOverrideRef.current =
      firstPath && (firstPath.rssi != null || firstPath.snr != null)
        ? { rssi: firstPath.rssi ?? null, snr: firstPath.snr ?? null }
        : undefined;

    if (message.packet_id == null) {
      setPacketInspectorSource({
        kind: 'unavailable',
        message:
          'No archival raw packet is available for this message, so packet analysis cannot be shown.',
      });
      return;
    }

    const cached = packetCacheRef.current.get(message.packet_id);
    if (cached) {
      setPacketInspectorSource({ kind: 'packet', packet: cached });
      return;
    }

    setPacketInspectorSource({ kind: 'loading', message: 'Loading packet analysis...' });

    try {
      const packet = await api.getPacket(message.packet_id);
      packetCacheRef.current.set(message.packet_id, packet);
      setPacketInspectorSource({ kind: 'packet', packet });
    } catch (error) {
      const description = error instanceof Error ? error.message : 'Unknown error';
      const isMissing = error instanceof Error && /not found/i.test(error.message);
      if (!isMissing) {
        toast.error('Failed to load raw packet', { description });
      }
      setPacketInspectorSource({
        kind: 'unavailable',
        message: isMissing
          ? 'The archival raw packet for this message is no longer available. It may have been purged from Settings > Database, so only the stored message and merged route history remain.'
          : `Could not load the archival raw packet for this message: ${description}`,
      });
    }
  }, []);

  // Sort messages by received_at ascending (oldest first)
  // Note: Deduplication is handled by useConversationMessages.observeMessage()
  // and the database UNIQUE constraint on (type, conversation_key, text, sender_timestamp)
  const sortedMessages = useMemo(
    () =>
      preSorted
        ? messages
        : [...messages].sort((a, b) => a.received_at - b.received_at || a.id - b.id),
    [messages, preSorted]
  );
  /**
   * Only the visible window of messages is mounted. A long channel history otherwise
   * costs a full render of every message on any update — hundreds of milliseconds once
   * a conversation has a few thousand messages, which stalls everything else on the
   * main thread, typing included.
   *
   * Heights are measured, not assumed: messages vary wildly (one line, a wrapped
   * paragraph, path badges, the unread divider), so `estimateSize` is only the starting
   * guess for rows that have not been on screen yet.
   */
  const virtualizer = useVirtualizer({
    count: sortedMessages.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => ESTIMATED_MESSAGE_HEIGHT,
    // Rows do not start at the scroll container's origin: the container has p-4
    // padding and may render an "older messages" banner above them. Without this
    // the virtualizer's offsets are short by that distance, so every
    // scrollToIndex with 'start'/'center' lands high by 16-48px — and the error
    // moves as the banner appears and disappears during pagination.
    scrollMargin,
    // String sentinel for the transient window past the end of a shrunken list:
    // a bare index would share the keyspace with message ids and poison the
    // measurement cache for whichever message happens to have that id.
    getItemKey: (index) => sortedMessages[index]?.id ?? `__idx:${index}`,
    overscan: 8,
    // A row that measures zero has not really been laid out yet (hidden pane, images
    // still loading). Keep the estimate instead, or the window balloons to compensate.
    measureElement: (element) => element.getBoundingClientRect().height || ESTIMATED_MESSAGE_HEIGHT,
    // A viewport that measures zero (before first layout, a hidden tab, jsdom) would
    // otherwise collapse the window to nothing and render an empty list. Fall back to a
    // nominal height so we always mount a plausible screenful.
    observeElementRect: (instance, cb) => {
      const element = instance.scrollElement;
      if (!element) return;
      const report = () => {
        const rect = element.getBoundingClientRect();
        cb({ width: rect.width, height: rect.height || FALLBACK_VIEWPORT_HEIGHT });
      };
      report();
      const observer = new ResizeObserver(report);
      observer.observe(element);
      return () => observer.disconnect();
    },
  });
  const virtualRows = virtualizer.getVirtualItems();

  // Re-measured whenever something above the rows can change height.
  useLayoutEffect(() => {
    const spacer = virtualSpacerRef.current;
    const list = listRef.current;
    if (!spacer || !list) return;
    // Relative to the scroll container's *content* origin, so it is independent
    // of the current scroll position. offsetTop is not usable here: the two
    // elements can resolve to different offsetParents.
    const next = Math.round(
      spacer.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop
    );
    setScrollMargin((prev) => (prev === next ? prev : next));
  }, [loadingOlder, hasOlderMessages, messages.length]);

  const scrollToIndex = useCallback(
    (index: number, align: 'start' | 'center' | 'end') => {
      if (index < 0) return;
      virtualizer.scrollToIndex(index, { align });
    },
    [virtualizer]
  );

  const requestBottomScroll = useCallback(() => {
    pendingBottomScrollRef.current = true;
    setBottomScrollNonce((n) => n + 1);
  }, []);

  // Drives a pending bottom-pin across a bounded run of frames. Deliberately not
  // keyed on the virtualizer's total size: that churns on every measurement pass,
  // which would re-enter this effect continuously. A fixed frame budget converges
  // as rows are measured and then stops on its own.
  useEffect(() => {
    if (!pendingBottomScrollRef.current) return;
    if (sortedMessages.length === 0) return;

    let frames = 0;
    let raf = 0;
    let lastAppliedTop: number | null = null;
    const step = () => {
      const list = listRef.current;
      if (!pendingBottomScrollRef.current || !list) return;

      // Something other than us moved the scroll (a programmatic scrollTop, a
      // restored position, assistive tech). Gesture handlers only catch a human
      // at the wheel, so also bail when the position we last set has been moved
      // upward — the pin must never fight another writer.
      if (lastAppliedTop !== null && list.scrollTop < lastAppliedTop - 1) {
        pendingBottomScrollRef.current = false;
        return;
      }

      scrollToIndex(sortedMessages.length - 1, 'end');
      lastAppliedTop = list.scrollTop;

      const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight <= 1;
      if (atBottom || (frames += 1) >= BOTTOM_SCROLL_FRAME_BUDGET) {
        pendingBottomScrollRef.current = false;
        return;
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [bottomScrollNonce, sortedMessages.length, scrollToIndex]);

  // Any deliberate scroll gesture cancels the pending pin, so the retry loop can
  // never fight a user who has started reading back through history.
  const cancelBottomScroll = useCallback(() => {
    pendingBottomScrollRef.current = false;
  }, []);

  // Handle scroll position AFTER render
  useLayoutEffect(() => {
    if (!listRef.current) return;

    const messagesAdded = messages.length - prevMessagesLengthRef.current;

    // Detect if messages are from a different conversation (handles the case where
    // the key prop remount consumes isInitialLoadRef on stale data from the previous
    // conversation before the cache restore effect sets the correct messages)
    const convKey = messages.length > 0 ? messages[0].conversation_key : null;
    const conversationChanged = convKey !== null && convKey !== prevConvKeyRef.current;
    if (convKey !== null) prevConvKeyRef.current = convKey;

    if ((isInitialLoadRef.current || conversationChanged) && messages.length > 0) {
      // Initial load or conversation switch - pin to the newest message. Requested
      // rather than performed here; see pendingBottomScrollRef.
      //
      // Unless we are loading *at* a specific message: jump-to-message and
      // jump-to-unread clear the list before fetching a window around their
      // target, which trips both the initial-load and conversation-changed
      // branches. Pinning to the bottom here would then discard the target scroll
      // a frame later, stranding the user at the newest message instead.
      if (!targetMessageId) {
        requestBottomScroll();
      }
      isInitialLoadRef.current = false;
    } else if (messagesAdded > 0 && prevMessagesLengthRef.current > 0) {
      if (scrollStateRef.current.wasNearTop) {
        // User was near top (loading older) - keep the message that was on top in place.
        // Prepended rows are unmeasured, so anchoring by index beats height arithmetic.
        scrollToIndex(messagesAdded, 'start');
      } else if (scrollStateRef.current.wasNearBottom && !hasNewerMessagesRef.current) {
        // User was near bottom - follow new messages (including sent).
        // Skip when browsing mid-history (hasNewerMessages) so that forward-pagination
        // appends in place instead of chasing the bottom in an infinite load loop.
        requestBottomScroll();
      }
    }

    prevMessagesLengthRef.current = messages.length;
  }, [messages, sortedMessages.length, scrollToIndex, requestBottomScroll, targetMessageId]);

  // Scroll to target message and highlight it
  useLayoutEffect(() => {
    if (!targetMessageId || targetScrolledRef.current || messages.length === 0) return;
    const targetIndex = sortedMessages.findIndex((msg) => msg.id === targetMessageId);
    if (targetIndex === -1) return;

    // Prevent the initial-load layout effect from overriding our scroll, and drop
    // any bottom pin already queued by an earlier pass over the same commit.
    isInitialLoadRef.current = false;
    pendingBottomScrollRef.current = false;
    scrollToIndex(targetIndex, 'center');
    setHighlightedMessageId(targetMessageId);
    targetScrolledRef.current = true;
    onTargetReached?.();
  }, [messages, sortedMessages, targetMessageId, onTargetReached, scrollToIndex]);

  // Reset target scroll tracking when targetMessageId changes
  useEffect(() => {
    targetScrolledRef.current = false;
  }, [targetMessageId]);

  // Reset initial load flag when conversation changes (messages becomes empty then filled)
  useEffect(() => {
    if (messages.length === 0) {
      isInitialLoadRef.current = true;
      prevMessagesLengthRef.current = 0;
      prevConvKeyRef.current = null;
      scrollStateRef.current = {
        scrollTop: 0,
        scrollHeight: 0,
        clientHeight: 0,
        wasNearTop: false,
        wasNearBottom: true,
      };
    }
  }, [messages.length]);

  // Track resendable outgoing CHAN messages (within 30s window)
  useEffect(() => {
    if (!onResendChannelMessage) return;

    const now = Math.floor(Date.now() / 1000);
    const newResendable = new Set<number>();
    const timers = resendTimersRef.current;

    for (const msg of messages) {
      if (!msg.outgoing || msg.type !== 'CHAN' || msg.sender_timestamp === null) continue;
      const remaining = RESEND_WINDOW_SECONDS - (now - msg.sender_timestamp);
      if (remaining <= 0) continue;

      newResendable.add(msg.id);

      // Schedule removal if not already tracked
      if (!timers.has(msg.id)) {
        const timer = setTimeout(() => {
          setResendableIds((prev) => {
            const next = new Set(prev);
            next.delete(msg.id);
            return next;
          });
          timers.delete(msg.id);
        }, remaining * 1000);
        timers.set(msg.id, timer);
      }
    }

    setResendableIds((prev) => {
      if (prev.size === newResendable.size) {
        let changed = false;
        for (const id of newResendable) {
          if (!prev.has(id)) {
            changed = true;
            break;
          }
        }
        if (!changed) {
          return prev;
        }
      }

      return newResendable;
    });

    return () => {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, [messages, onResendChannelMessage]);

  /**
   * Located by message id, not by timestamp. The previous `received_at > boundary`
   * scan returned 0 — the top of the loaded window — whenever the real boundary was
   * further back than anything loaded, so the divider silently pointed at the wrong
   * message. Matching on identity returns -1 in that case, which is the truth: the
   * boundary is elsewhere, and `boundaryOutsideWindow` below offers to go to it.
   */
  const unreadMarkerIndex = useMemo(() => {
    if (unreadMarkerMessageId == null) return -1;
    return sortedMessages.findIndex((msg) => msg.id === unreadMarkerMessageId);
  }, [sortedMessages, unreadMarkerMessageId]);

  // Unread exists, but the message it starts at has not been loaded.
  const boundaryOutsideWindow = unreadMarkerMessageId != null && unreadMarkerIndex === -1;

  const syncJumpToUnreadVisibility = useCallback(() => {
    if (jumpToUnreadDismissed) {
      setShowJumpToUnread(false);
      return;
    }
    // Boundary is real but out of the loaded window: always offer the jump.
    if (boundaryOutsideWindow) {
      setShowJumpToUnread(true);
      return;
    }
    if (unreadMarkerIndex === -1) {
      setShowJumpToUnread(false);
      return;
    }

    const marker = unreadMarkerRef.current;
    const list = listRef.current;
    if (!marker || !list) {
      setShowJumpToUnread(true);
      return;
    }

    const markerRect = marker.getBoundingClientRect();
    const listRect = list.getBoundingClientRect();

    if (
      markerRect.width === 0 ||
      markerRect.height === 0 ||
      listRect.width === 0 ||
      listRect.height === 0
    ) {
      setShowJumpToUnread(true);
      return;
    }

    const markerVisible =
      markerRect.top >= listRect.top &&
      markerRect.bottom <= listRect.bottom &&
      markerRect.left >= listRect.left &&
      markerRect.right <= listRect.right;

    setShowJumpToUnread(!markerVisible);
  }, [jumpToUnreadDismissed, unreadMarkerIndex, boundaryOutsideWindow]);

  // Refs for scroll handler to read without causing callback recreation
  const onLoadOlderRef = useRef(onLoadOlder);
  const loadingOlderRef = useRef(loadingOlder);
  const hasOlderMessagesRef = useRef(hasOlderMessages);
  const onLoadNewerRef = useRef(onLoadNewer);
  const loadingNewerRef = useRef(loadingNewer);
  const hasNewerMessagesRef = useRef(hasNewerMessages);
  onLoadOlderRef.current = onLoadOlder;
  loadingOlderRef.current = loadingOlder;
  hasOlderMessagesRef.current = hasOlderMessages;
  onLoadNewerRef.current = onLoadNewer;
  loadingNewerRef.current = loadingNewer;
  hasNewerMessagesRef.current = hasNewerMessages;

  const setUnreadMarkerElement = useCallback(
    (node: HTMLButtonElement | HTMLDivElement | null) => {
      unreadMarkerRef.current = node;
      syncJumpToUnreadVisibility();
    },
    [syncJumpToUnreadVisibility]
  );

  useEffect(() => {
    setJumpToUnreadDismissed(false);
  }, [unreadMarkerIndex]);

  useLayoutEffect(() => {
    syncJumpToUnreadVisibility();
  }, [messages, syncJumpToUnreadVisibility]);

  // Handle scroll - capture state and detect when user is near top/bottom
  // Stable callback: reads changing values from refs, never recreated.
  const handleScroll = useCallback(() => {
    if (!listRef.current) return;

    const { scrollTop, scrollHeight, clientHeight } = listRef.current;
    const distanceFromBottom = scrollHeight - scrollTop - clientHeight;

    scrollStateRef.current = {
      scrollTop,
      scrollHeight,
      clientHeight,
      wasNearTop: scrollTop < 150,
      wasNearBottom: distanceFromBottom < 100,
    };

    setShowScrollToBottom(distanceFromBottom > 100);

    if (!onLoadOlderRef.current || loadingOlderRef.current || !hasOlderMessagesRef.current) {
      // skip older load
    } else if (scrollTop < 100) {
      onLoadOlderRef.current();
    }

    if (
      onLoadNewerRef.current &&
      !loadingNewerRef.current &&
      hasNewerMessagesRef.current &&
      distanceFromBottom < 100
    ) {
      onLoadNewerRef.current();
    }
    syncJumpToUnreadVisibility();
  }, [syncJumpToUnreadVisibility]);

  // Scroll to bottom handler (or jump to bottom if viewing historical messages)
  const scrollToBottom = useCallback(() => {
    if (hasNewerMessages && onJumpToBottom) {
      onJumpToBottom();
      return;
    }
    scrollToIndex(sortedMessages.length - 1, 'end');
  }, [hasNewerMessages, onJumpToBottom, scrollToIndex, sortedMessages.length]);

  // Sender info for outgoing messages (used by path modal on own messages)
  const selfSenderInfo = useMemo<SenderInfo>(
    () => ({
      name: config?.name || 'Unknown',
      publicKeyOrPrefix: config?.public_key || '',
      lat: config?.lat ?? null,
      lon: config?.lon ?? null,
      pathHashMode: config?.path_hash_mode ?? null,
    }),
    [config?.name, config?.public_key, config?.lat, config?.lon, config?.path_hash_mode]
  );

  // Copy / retry / cancel / delete / react: the menu only appears when the host
  // wired at least one of them, so a read-only embedding of the list stays
  // read-only.
  const hasMessageActions = !!(
    onRetryMessage ||
    onCancelMessage ||
    onDeleteMessage ||
    onReactToMessage
  );

  // Whether a MeshCore Open Advanced reaction can address this message. The
  // wire hash needs a sender timestamp, and in 1:1 chats the peer's client only
  // matches incoming reactions against its own outgoing messages -- so reacting
  // to our own bubble could never land anywhere and is not offered.
  const canReactToMessage = useCallback(
    (msg: Message): boolean => {
      if (!onReactToMessage || msg.sender_timestamp == null) return false;
      if (msg.type === 'CHAN') return true;
      const conversationContact = contacts.find((c) => c.public_key === msg.conversation_key);
      if (conversationContact?.type === CONTACT_TYPE_ROOM) return true;
      return !msg.outgoing;
    },
    [contacts, onReactToMessage]
  );

  const copyMessageText = useCallback(async (message: Message) => {
    try {
      await navigator.clipboard.writeText(message.text);
      toast.success('Message copied');
    } catch {
      toast.error('Could not copy the message');
    }
  }, []);

  // Derive live so the byte-perfect button disables if the 30s window expires while modal is open
  const isSelectedMessageResendable =
    selectedPath?.messageId !== undefined && resendableIds.has(selectedPath.messageId);

  // Look up contact by public key
  const getContact = (conversationKey: string | null): Contact | null => {
    if (!conversationKey) return null;
    return contacts.find((c) => c.public_key === conversationKey) || null;
  };

  // Look up contact by name (for channel messages where we parse sender from text)
  const getContactByName = (name: string): Contact | null => {
    return contacts.find((c) => c.name === name) || null;
  };

  const isCorruptUnnamedChannelMessage = (msg: Message, parsedSender: string | null): boolean => {
    return (
      msg.type === 'CHAN' &&
      !msg.outgoing &&
      !msg.sender_name &&
      !msg.sender_key &&
      !parsedSender &&
      hasUnexpectedControlChars(msg.text)
    );
  };

  // Build sender info for path modal
  const getSenderInfo = (
    msg: Message,
    contact: Contact | null,
    parsedSender: string | null
  ): SenderInfo => {
    if (
      msg.type === 'PRIV' &&
      contact?.type === CONTACT_TYPE_ROOM &&
      (msg.sender_key || msg.sender_name)
    ) {
      const authorContact =
        (msg.sender_key
          ? contacts.find((candidate) => candidate.public_key === msg.sender_key)
          : null) || (msg.sender_name ? getContactByName(msg.sender_name) : null);
      if (authorContact) {
        const directRoute = getDirectContactRoute(authorContact);
        return {
          name: authorContact.name || msg.sender_name || authorContact.public_key.slice(0, 12),
          publicKeyOrPrefix: authorContact.public_key,
          lat: authorContact.lat,
          lon: authorContact.lon,
          pathHashMode: directRoute?.path_hash_mode ?? null,
        };
      }
      return {
        name: msg.sender_name || msg.sender_key || 'Unknown',
        publicKeyOrPrefix: msg.sender_key || '',
        lat: null,
        lon: null,
        pathHashMode: null,
      };
    }
    if (msg.type === 'PRIV' && contact) {
      const directRoute = getDirectContactRoute(contact);
      return {
        name: contact.name || contact.public_key.slice(0, 12),
        publicKeyOrPrefix: contact.public_key,
        lat: contact.lat,
        lon: contact.lon,
        pathHashMode: directRoute?.path_hash_mode ?? null,
      };
    }
    if (msg.type === 'CHAN') {
      const senderName = msg.sender_name || parsedSender;
      const senderContact =
        (msg.sender_key
          ? contacts.find((candidate) => candidate.public_key === msg.sender_key)
          : null) || (senderName ? getContactByName(senderName) : null);
      if (senderContact) {
        const directRoute = getDirectContactRoute(senderContact);
        return {
          name: senderContact.name || senderName || senderContact.public_key.slice(0, 12),
          publicKeyOrPrefix: senderContact.public_key,
          lat: senderContact.lat,
          lon: senderContact.lon,
          pathHashMode: directRoute?.path_hash_mode ?? null,
        };
      }
      if (senderName || msg.sender_key) {
        return {
          name: senderName || msg.sender_key || 'Unknown',
          publicKeyOrPrefix: msg.sender_key || msg.conversation_key || '',
          lat: null,
          lon: null,
          pathHashMode: null,
        };
      }
    }

    // For channel messages, try to find contact by parsed sender name
    if (parsedSender) {
      const senderContact = getContactByName(parsedSender);
      if (senderContact) {
        const directRoute = getDirectContactRoute(senderContact);
        return {
          name: parsedSender,
          publicKeyOrPrefix: senderContact.public_key,
          lat: senderContact.lat,
          lon: senderContact.lon,
          pathHashMode: directRoute?.path_hash_mode ?? null,
        };
      }
    }
    // Fallback: unknown sender
    return {
      name: parsedSender || 'Unknown',
      publicKeyOrPrefix: msg.conversation_key || '',
      lat: null,
      lon: null,
      pathHashMode: null,
    };
  };

  if (loading) {
    return (
      <div className="flex-1 overflow-y-auto p-5 text-center text-muted-foreground" role="status">
        Loading messages...
      </div>
    );
  }

  if (messages.length === 0) {
    return (
      <div className="flex-1 overflow-y-auto p-5 text-center text-muted-foreground">
        No messages yet
      </div>
    );
  }

  // Helper to get a unique sender key for grouping messages
  const getSenderKey = (
    msg: Message,
    senderName: string | null,
    isCorruptChannelMessage: boolean
  ): string => {
    if (msg.outgoing) return '__outgoing__';
    if (msg.type === 'PRIV' && msg.sender_key) return `key:${msg.sender_key}`;
    if (msg.type === 'PRIV' && senderName) return `name:${senderName}`;
    if (msg.type === 'PRIV' && msg.conversation_key) return msg.conversation_key;
    if (msg.sender_key) return `key:${msg.sender_key}`;
    if (senderName) return `name:${senderName}`;
    if (isCorruptChannelMessage) return `corrupt:${msg.id}`;
    return '__unknown__';
  };

  return (
    <div className="flex-1 overflow-hidden relative">
      <div
        className="h-full overflow-y-auto p-4 flex flex-col"
        ref={listRef}
        onScroll={handleScroll}
        onWheel={cancelBottomScroll}
        onTouchStart={cancelBottomScroll}
        onKeyDown={cancelBottomScroll}
      >
        {loadingOlder && (
          <div className="text-center py-2 text-muted-foreground text-sm" role="status">
            Loading older messages...
          </div>
        )}
        {!loadingOlder && hasOlderMessages && (
          <div className="text-center py-2 text-muted-foreground text-xs">
            Scroll up for older messages
          </div>
        )}
        <div
          ref={virtualSpacerRef}
          className="relative w-full flex-shrink-0"
          style={{ height: virtualizer.getTotalSize() }}
        >
          {virtualRows.map((virtualRow) => {
            const index = virtualRow.index;
            const msg = sortedMessages[index];
            // The virtualizer can briefly hold indices from a longer previous list
            // (conversation switch, blocked-sender refilter). Rendering ahead of
            // that would dereference undefined and blank the whole chat pane.
            if (!msg) return null;
            // For DMs, look up contact; for channel messages, use parsed sender
            const contact = msg.type === 'PRIV' ? getContact(msg.conversation_key) : null;
            const isRoomServer = contact?.type === CONTACT_TYPE_ROOM;

            // Only parse "sender: text" prefix for channel messages — DMs never carry
            // an in-text sender prefix, so parsing them would incorrectly strip
            // user text that happens to contain a colon (e.g. "TEST1: TEST2").
            const { sender, content } =
              msg.type === 'PRIV'
                ? { sender: null, content: msg.text }
                : parseSenderFromText(msg.text);
            // AEIC images ride as `aei1:` text; parsed once here so the bubble
            // dispatch below does not re-parse it per branch.
            const aeicChunk = parseAeicChunk(content);
            // An image received as binary GRP_DATA: no text crossed the air,
            // so the server left a marker for this bubble to hang off.
            const aeicBinaryRef = parseAeicBinaryRef(content);
            // Media the server kept but cannot decode: a box rather than silence.
            const unsupportedMediaRef = parseUnsupportedMediaRef(content);
            const directSenderName =
              msg.type === 'PRIV' && isRoomServer ? msg.sender_name || null : null;
            const channelSenderName = msg.type === 'CHAN' ? msg.sender_name || sender : null;
            const channelSenderContact =
              msg.type === 'CHAN' && channelSenderName ? getContactByName(channelSenderName) : null;
            const isCorruptChannelMessage = isCorruptUnnamedChannelMessage(msg, sender);
            const displaySender = msg.outgoing
              ? 'You'
              : directSenderName ||
                (isRoomServer && msg.sender_key ? msg.sender_key.slice(0, 8) : null) ||
                contact?.name ||
                channelSenderName ||
                (isCorruptChannelMessage
                  ? CORRUPT_SENDER_LABEL
                  : msg.conversation_key?.slice(0, 8) || 'Unknown');

            const canClickSender =
              !msg.outgoing &&
              onSenderClick &&
              displaySender !== 'Unknown' &&
              displaySender !== CORRUPT_SENDER_LABEL;

            // Determine if we should show avatar (first message in a chunk from same sender)
            const currentSenderKey = getSenderKey(
              msg,
              directSenderName || channelSenderName,
              isCorruptChannelMessage
            );
            const prevMsg = sortedMessages[index - 1];
            const prevParsedSender =
              prevMsg && prevMsg.type === 'CHAN' ? parseSenderFromText(prevMsg.text).sender : null;
            const prevSenderKey = prevMsg
              ? getSenderKey(
                  prevMsg,
                  prevMsg.type === 'PRIV' &&
                    getContact(prevMsg.conversation_key)?.type === CONTACT_TYPE_ROOM
                    ? prevMsg.sender_name
                    : prevMsg.type === 'CHAN'
                      ? prevMsg.sender_name || prevParsedSender
                      : prevParsedSender,
                  isCorruptUnnamedChannelMessage(prevMsg, prevParsedSender)
                )
              : null;
            const isFirstInGroup = currentSenderKey !== prevSenderKey;
            const showAvatar = !msg.outgoing && isFirstInGroup;
            const isFirstMessage = index === 0;

            // Get avatar info for incoming messages
            let avatarName: string | null = null;
            let avatarKey: string = '';
            let avatarVariant: 'default' | 'corrupt' = 'default';
            if (!msg.outgoing) {
              if (msg.type === 'PRIV' && msg.conversation_key) {
                if (isRoomServer) {
                  avatarName = directSenderName;
                  avatarKey =
                    msg.sender_key || (avatarName ? `name:${avatarName}` : msg.conversation_key);
                } else {
                  avatarName = contact?.name || null;
                  avatarKey = msg.conversation_key;
                }
              } else if (isCorruptChannelMessage) {
                avatarName = CORRUPT_SENDER_LABEL;
                avatarKey = `corrupt:${msg.id}`;
                avatarVariant = 'corrupt';
              } else {
                // Channel message: use stored sender identity first, then parsed/fallback display name
                avatarName =
                  channelSenderName || (displaySender !== 'Unknown' ? displaySender : null);
                avatarKey =
                  msg.sender_key ||
                  channelSenderContact?.public_key ||
                  (avatarName ? `name:${avatarName}` : `message:${msg.id}`);
              }
            }
            const avatarActionLabel =
              avatarName && avatarName !== 'Unknown'
                ? `View info for ${avatarName}`
                : `View info for ${avatarKey.slice(0, 12)}`;

            return (
              // Absolutely positioned so the scroll container keeps a stable total height
              // while only the visible window is mounted. `flex flex-col` matters: it makes
              // child margins (group spacing, the unread divider) part of the measured height.
              <div
                key={msg.id}
                data-index={index}
                ref={virtualizer.measureElement}
                className="absolute left-0 top-0 flex w-full flex-col pb-0.5"
                // start is measured from the scroll container's origin, which
                // scrollMargin accounts for; the spacer already sits that far
                // down, so subtract it back out when positioning within it.
                style={{ transform: `translateY(${virtualRow.start - scrollMargin}px)` }}
              >
                {unreadMarkerIndex === index &&
                  (onDismissUnreadMarker ? (
                    <button
                      ref={setUnreadMarkerElement}
                      type="button"
                      className="my-2 flex w-full items-center gap-3 text-left text-xs font-medium text-primary transition-colors hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      onClick={onDismissUnreadMarker}
                    >
                      <span className="h-px flex-1 bg-border" />
                      <span className="rounded-full border border-primary/30 bg-primary/10 px-3 py-1">
                        Unread messages
                      </span>
                      <span className="h-px flex-1 bg-border" />
                    </button>
                  ) : (
                    <div
                      ref={setUnreadMarkerElement}
                      className="my-2 flex w-full items-center gap-3 text-xs font-medium text-primary"
                    >
                      <span className="h-px flex-1 bg-border" />
                      <span className="rounded-full border border-primary/30 bg-primary/10 px-3 py-1">
                        Unread messages
                      </span>
                      <span className="h-px flex-1 bg-border" />
                    </div>
                  ))}
                <div
                  data-message-id={msg.id}
                  className={cn(
                    'flex items-start max-w-[85%]',
                    msg.outgoing && 'flex-row-reverse self-end',
                    isFirstInGroup && !isFirstMessage && 'mt-3'
                  )}
                >
                  {!msg.outgoing && (
                    <div className="w-10 flex-shrink-0 flex items-start pt-0.5">
                      {showAvatar &&
                        avatarKey &&
                        (onOpenContactInfo ? (
                          <button
                            type="button"
                            className="avatar-action-button rounded-full border-none bg-transparent p-0 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            aria-label={avatarActionLabel}
                            onClick={() =>
                              onOpenContactInfo(
                                avatarKey,
                                msg.type === 'CHAN' || (msg.type === 'PRIV' && isRoomServer)
                              )
                            }
                          >
                            <ContactAvatar
                              name={avatarName}
                              publicKey={avatarKey}
                              size={32}
                              clickable
                              variant={avatarVariant}
                            />
                          </button>
                        ) : (
                          <span>
                            <ContactAvatar
                              name={avatarName}
                              publicKey={avatarKey}
                              size={32}
                              variant={avatarVariant}
                            />
                          </span>
                        ))}
                    </div>
                  )}
                  <div
                    className={cn(
                      'py-1.5 px-3 rounded-lg min-w-0',
                      msg.outgoing ? 'bg-msg-outgoing' : 'bg-msg-incoming',
                      highlightedMessageId === msg.id && 'message-highlight'
                    )}
                    // Right-click anywhere on the bubble is the desktop habit;
                    // the meta line's ⋯ button covers touch and keyboard.
                    onContextMenu={
                      hasMessageActions
                        ? (e) => {
                            e.preventDefault();
                            setActionsTarget(msg);
                          }
                        : undefined
                    }
                  >
                    {showAvatar && (
                      <div className="text-[0.8125rem] font-semibold text-foreground mb-0.5">
                        {canClickSender ? (
                          <span
                            className="cursor-pointer hover:text-primary transition-colors"
                            role="button"
                            tabIndex={0}
                            onKeyDown={handleKeyboardActivate}
                            onClick={() => onSenderClick(displaySender)}
                            title={`Mention ${displaySender}`}
                          >
                            {displaySender}
                          </span>
                        ) : (
                          displaySender
                        )}
                      </div>
                    )}
                    <div className="break-words whitespace-pre-wrap">
                      {parseImageEnvelope(content) ? (
                        <ImageMessage message={msg} content={content} />
                      ) : unsupportedMediaRef ? (
                        <UnsupportedMediaMessage mediaId={unsupportedMediaRef} />
                      ) : aeicBinaryRef ? (
                        <AeicImageMessage message={msg} sessionKey={aeicBinaryRef} />
                      ) : aeicChunk ? (
                        aeicChunk.index === 0 ? (
                          <AeicImageMessage message={msg} chunk={aeicChunk} />
                        ) : (
                          <AeicImagePart chunk={aeicChunk} />
                        )
                      ) : parseVoiceEnvelope(content) ? (
                        <VoiceMessage message={msg} content={content} />
                      ) : (
                        (renderRichPayloads &&
                          renderMeshcoreOpenPayload(content, radioName, onChannelReferenceClick)) ||
                        content.split('\n').map((line, i, arr) => (
                          <span key={i}>
                            {renderTextWithMentions(line, radioName, onChannelReferenceClick)}
                            {i < arr.length - 1 && <br />}
                          </span>
                        ))
                      )}
                    </div>
                    {msg.reactions && Object.keys(msg.reactions).length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {Object.entries(msg.reactions).map(([emoji, count]) =>
                          canReactToMessage(msg) ? (
                            <button
                              key={emoji}
                              type="button"
                              className="inline-flex items-center gap-1 rounded-full border border-border bg-background/60 px-2 py-0.5 text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                              title={`React with ${emoji}`}
                              onClick={() => void onReactToMessage?.(msg, emoji)}
                            >
                              <span>{emoji}</span>
                              {count > 1 && (
                                <span className="text-xs text-muted-foreground">{count}</span>
                              )}
                            </button>
                          ) : (
                            <span
                              key={emoji}
                              className="inline-flex items-center gap-1 rounded-full border border-border bg-background/60 px-2 py-0.5 text-sm"
                            >
                              <span>{emoji}</span>
                              {count > 1 && (
                                <span className="text-xs text-muted-foreground">{count}</span>
                              )}
                            </span>
                          )
                        )}
                      </div>
                    )}
                    <MessageMetaLine
                      message={msg}
                      paths={msg.paths ?? null}
                      onShowPaths={
                        // The paths dialog doubles as the outgoing-channel status
                        // panel, so an unechoed channel message still opens it.
                        (msg.paths && msg.paths.length > 0) ||
                        (msg.outgoing && msg.type === 'CHAN' && !!onResendChannelMessage)
                          ? () =>
                              setSelectedPath({
                                paths: msg.paths ?? [],
                                senderInfo: msg.outgoing
                                  ? selfSenderInfo
                                  : getSenderInfo(msg, contact, directSenderName || sender),
                                messageId: msg.id,
                                packetId: msg.packet_id,
                                isOutgoingChan:
                                  msg.outgoing && msg.type === 'CHAN' && !!onResendChannelMessage,
                              })
                          : undefined
                      }
                      onOpenActions={hasMessageActions ? () => setActionsTarget(msg) : undefined}
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        {loadingNewer && (
          <div className="text-center py-2 text-muted-foreground text-sm" role="status">
            Loading newer messages...
          </div>
        )}
        {!loadingNewer && hasNewerMessages && (
          <div className="text-center py-2 text-muted-foreground text-xs">
            Scroll down for newer messages
          </div>
        )}
      </div>

      {/* Scroll to bottom button */}
      {showJumpToUnread && (
        <div className="pointer-events-none absolute bottom-4 left-1/2 -translate-x-1/2">
          <div className="pointer-events-auto flex h-9 items-center overflow-hidden rounded-full border border-border bg-card shadow-lg transition-all hover:scale-105">
            <button
              type="button"
              onClick={() => {
                if (boundaryOutsideWindow && unreadMarkerMessageId != null) {
                  // Not in loaded history: hand off to the jump-to-message path,
                  // which loads a window around the boundary instead of paging
                  // everything between here and there.
                  onNavigateToUnread?.(unreadMarkerMessageId);
                } else if (unreadMarkerRef.current?.scrollIntoView) {
                  unreadMarkerRef.current.scrollIntoView({ block: 'center' });
                } else {
                  // The marker row is outside the rendered window — scroll by index.
                  scrollToIndex(unreadMarkerIndex, 'center');
                }
                setJumpToUnreadDismissed(true);
                setShowJumpToUnread(false);
              }}
              className="h-full px-3 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Jump to unread
            </button>
            <button
              type="button"
              onClick={() => {
                setJumpToUnreadDismissed(true);
                setShowJumpToUnread(false);
              }}
              className="flex h-full w-9 items-center justify-center border-l border-border text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label="Dismiss jump to unread"
              title="Dismiss jump to unread"
            >
              ×
            </button>
          </div>
        </div>
      )}
      {showScrollToBottom && (
        <button
          onClick={scrollToBottom}
          className="absolute bottom-4 right-4 w-9 h-9 rounded-full bg-card hover:bg-accent border border-border flex items-center justify-center shadow-lg transition-all hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          title="Scroll to bottom"
          aria-label="Scroll to bottom"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="text-muted-foreground"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
      )}

      {/* Path modal */}
      {selectedPath && (
        <PathModal
          open={true}
          onClose={() => setSelectedPath(null)}
          paths={selectedPath.paths}
          senderInfo={selectedPath.senderInfo}
          contacts={contacts}
          config={config ?? null}
          messageId={selectedPath.messageId}
          packetId={selectedPath.packetId}
          isOutgoingChan={selectedPath.isOutgoingChan}
          isResendable={isSelectedMessageResendable}
          onResend={onResendChannelMessage}
          onAnalyzePacket={
            selectedPath.packetId != null
              ? () => {
                  const message = messages.find((entry) => entry.id === selectedPath.messageId);
                  if (message) {
                    void handleAnalyzePacket(message);
                  }
                }
              : undefined
          }
        />
      )}
      {packetInspectorSource && (
        <RawPacketInspectorDialog
          open={packetInspectorSource !== null}
          onOpenChange={(isOpen) => {
            if (!isOpen) {
              setPacketInspectorSource(null);
              packetSignalOverrideRef.current = undefined;
            }
          }}
          channels={channels}
          source={packetInspectorSource}
          title="Analyze Packet"
          description="On-demand raw packet analysis for a message-backed archival packet."
          notice={ANALYZE_PACKET_NOTICE}
          signalOverride={packetSignalOverrideRef.current}
        />
      )}
      {actionsTarget && (
        <MessageActionsDialog
          message={actionsTarget}
          onClose={() => setActionsTarget(null)}
          onCopy={copyMessageText}
          onRetry={onRetryMessage}
          onCancel={onCancelMessage}
          onDelete={onDeleteMessage}
          onReact={canReactToMessage(actionsTarget) ? onReactToMessage : undefined}
        />
      )}
    </div>
  );
}
