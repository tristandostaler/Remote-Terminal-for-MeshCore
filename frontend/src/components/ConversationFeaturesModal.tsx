import { useCallback, useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';
import { Switch } from './ui/switch';
import { cn } from '@/lib/utils';
import { api, type AeicStatus, type ImageCodecId } from '../api';
import { toast } from './ui/sonner';

/**
 * Per-conversation MeshCore Open feature toggles. Opened from the chat header.
 *
 * Three features today: MCMP text compression, which codec photos use, and
 * whether media fragments may fall back to text. Each is one bordered block;
 * changes apply immediately. To add a feature, add a prop pair (state + setter)
 * and render another block.
 */

interface FeatureRowProps {
  title: string;
  description: string;
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  ariaLabel: string;
}

function FeatureRow({ title, description, checked, onCheckedChange, ariaLabel }: FeatureRowProps) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm font-medium text-foreground">{title}</div>
        <div className="mt-0.5 text-xs leading-snug text-muted-foreground">{description}</div>
      </div>
      <Switch
        checked={checked}
        onCheckedChange={onCheckedChange}
        aria-label={ariaLabel}
        className="mt-0.5 flex-shrink-0"
      />
    </div>
  );
}

const MCMP_VERSIONS: { value: number; label: string; description: string }[] = [
  { value: 2, label: 'v2', description: 'Smaller; widely compatible' },
  { value: 3, label: 'v3', description: 'Container (timestamp); matches the advanced fork' },
];

const IMAGE_CODECS: {
  value: ImageCodecId;
  label: string;
  headline: string;
  description: string;
}[] = [
  {
    value: 'ie4',
    label: 'Standard',
    headline: '15-40 packets',
    description: '256px greyscale AVIF/JPEG. Readable by MeshCore SAR clients.',
  },
  {
    value: 'aeic',
    label: 'AI reconstruction',
    headline: '1-2 messages',
    description: '512px colour from ~150 bytes. The receiver rebuilds it with a neural decoder.',
  },
];

function formatMib(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(0)} MB`;
}

/**
 * The AEIC option's availability, plus the model download it may need.
 *
 * Local to the modal rather than global state: it is only looked at here, and
 * the answer can change while the dialog is open (a download finishing), so it
 * re-polls while one is in flight.
 */
function useAeicStatus(open: boolean) {
  const [status, setStatus] = useState<AeicStatus | null>(null);
  const refresh = useCallback(async () => {
    try {
      setStatus(await api.getAeicStatus());
    } catch {
      // An older server without the endpoint just leaves the AI option disabled
      // with its default explanation; nothing worth interrupting the user for.
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void refresh();
  }, [open, refresh]);

  useEffect(() => {
    if (!open || !status?.downloading) return;
    const timer = window.setInterval(() => void refresh(), 1000);
    return () => window.clearInterval(timer);
  }, [open, status?.downloading, refresh]);

  return { status, refresh };
}

/**
 * Download / progress / diagnosis for the model the AI codec needs.
 *
 * The bundle has two halves and only one of them is anybody's decision. Sending
 * needs 65 MB, costs no memory worth worrying about, and the server fetches it
 * on its own -- so this panel never asks about it, it only reports it. Receiving
 * needs the other 893 MB on disk and ~1.4 GB of memory per picture, which is a
 * real question on a Pi, so that half stays an explicit button.
 */
function AeicModelPanel({
  status,
  onRefresh,
}: {
  status: AeicStatus;
  onRefresh: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);

  const start = useCallback(
    async (scope: 'send' | 'full') => {
      setBusy(true);
      try {
        await api.startAeicModelDownload(scope);
        await onRefresh();
      } catch (error) {
        toast.error('Could not start the download', {
          description: error instanceof Error ? error.message : String(error),
        });
      } finally {
        setBusy(false);
      }
    },
    [onRefresh]
  );

  const cancel = useCallback(async () => {
    setBusy(true);
    try {
      await api.cancelAeicModelDownload();
      await onRefresh();
    } finally {
      setBusy(false);
    }
  }, [onRefresh]);

  if (!status.runtime_available) {
    return (
      <p className="mt-2 text-xs leading-snug text-muted-foreground">
        The AI codec is switched off on this server. Set <code>MESHCORE_ENABLE_AEIC=true</code> and
        restart — it installs ~120 MB of dependencies on that first start, no rebuild needed.
        Requires a 64-bit host (amd64 or aarch64).
      </p>
    );
  }

  if (status.downloading) {
    // Against the half in flight: 65 MB measured against the whole 958 would
    // crawl to 7% and then call itself done.
    const target = status.download_target_bytes || status.bundle_total_bytes;
    const percent = Math.min(100, Math.round((status.download_done_bytes / target) * 100));
    return (
      <div className="mt-2 space-y-1.5">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="animate-spin" size={13} />
          <span className="truncate">
            {status.download_scope === 'send'
              ? `Getting ready to send (${formatMib(target)})`
              : (status.download_file ?? 'Downloading')}{' '}
            — {percent}%
          </span>
        </div>
        <div className="h-1 overflow-hidden rounded-full bg-muted">
          <div className="h-full bg-primary transition-all" style={{ width: `${percent}%` }} />
        </div>
        <button
          type="button"
          onClick={() => void cancel()}
          disabled={busy}
          className="text-xs text-muted-foreground underline hover:text-foreground"
        >
          Cancel
        </button>
      </div>
    );
  }

  if (status.supports_decode) return null;

  if (!status.reconstruction_enabled) {
    // MESHCORE_ENABLE_AEIC=false governs rebuilding only, so this is not the
    // "switched off" dead end above: sending works, and saying so is the whole
    // difference between a setting and a broken feature.
    return (
      <div className="mt-2 space-y-1.5">
        <p className="text-xs leading-snug text-muted-foreground">
          Sending works. Rebuilding photos other people send is switched off on this server (
          <code>MESHCORE_ENABLE_AEIC=false</code>) — it needs about 1.4 GB of memory per photo. One
          that arrives is kept and shown as a box, so switching this back on still decodes it.
        </p>
        {!status.supports_encode && (
          <button
            type="button"
            onClick={() => void start('send')}
            disabled={busy}
            className="rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-accent disabled:opacity-60"
          >
            Get sending working ({formatMib(status.send_half_total_bytes)})
          </button>
        )}
      </div>
    );
  }

  const error = status.last_error && (
    <p className="text-xs leading-snug text-destructive">{status.last_error}</p>
  );
  const rest = status.bundle_total_bytes - status.send_half_total_bytes;

  if (status.supports_encode) {
    return (
      <div className="mt-2 space-y-1.5">
        <p className="text-xs leading-snug text-muted-foreground">
          Sending works. Opening an AI photo someone sends you needs the rest of the model —{' '}
          {formatMib(rest)} on the server, and about 1.4 GB of memory each time it rebuilds one,
          which is more than a small Pi has. Until then a received photo is kept and shown as a box
          you can open later or from a roomier machine.
        </p>
        {error}
        <button
          type="button"
          onClick={() => void start('full')}
          disabled={busy}
          className="rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-accent disabled:opacity-60"
        >
          Download the rest ({formatMib(rest)})
        </button>
      </div>
    );
  }

  return (
    <div className="mt-2 space-y-1.5">
      <p className="text-xs leading-snug text-muted-foreground">
        Sending needs {formatMib(status.send_half_total_bytes)} of model on the server, which it
        fetches by itself — so this normally clears on its own. Start it now if you would rather not
        wait, or take the whole {formatMib(status.bundle_total_bytes)} to open received photos too.
      </p>
      {error}
      <div className="flex flex-wrap gap-1.5">
        <button
          type="button"
          onClick={() => void start('send')}
          disabled={busy}
          className="rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-accent disabled:opacity-60"
        >
          Get sending working ({formatMib(status.send_half_total_bytes)})
        </button>
        <button
          type="button"
          onClick={() => void start('full')}
          disabled={busy}
          className="rounded-md border border-border px-2 py-1 text-xs font-medium hover:bg-accent disabled:opacity-60"
        >
          Whole model ({formatMib(status.bundle_total_bytes)})
        </button>
      </div>
    </div>
  );
}

interface ConversationFeaturesModalProps {
  open: boolean;
  onClose: () => void;
  conversationType: 'contact' | 'channel';
  conversationId: string;
  conversationName: string;
  mcmpEnabled: boolean;
  mcmpVersion: number;
  imageCodec: ImageCodecId;
  rawMediaTextTransport: boolean;
  onSetMcmpEnabled: (
    type: 'channel' | 'contact',
    id: string,
    enabled: boolean,
    version: number
  ) => void;
  onSetImageCodec: (type: 'channel' | 'contact', id: string, codec: ImageCodecId) => void;
  onSetRawMediaTextTransport?: (id: string, enabled: boolean) => void;
}

export function ConversationFeaturesModal({
  open,
  onClose,
  conversationType,
  conversationId,
  conversationName,
  mcmpEnabled,
  mcmpVersion,
  imageCodec,
  rawMediaTextTransport,
  onSetMcmpEnabled,
  onSetImageCodec,
  onSetRawMediaTextTransport,
}: ConversationFeaturesModalProps) {
  const { status: aeicStatus, refresh: refreshAeic } = useAeicStatus(open);
  // Only gate on the server's ability to ENCODE: that is what picking the codec
  // for this conversation actually commits us to. Whether the peer can decode is
  // their business and something we cannot know.
  const aeicSelectable = aeicStatus?.supports_encode ?? false;

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col overflow-hidden sm:max-w-[520px]">
        <DialogHeader className="flex-shrink-0">
          <DialogTitle>Conversation features</DialogTitle>
          <DialogDescription>
            Optional features for{' '}
            <span className="font-medium text-foreground">{conversationName}</span>. Both sides must
            support a feature for it to work, so turn one on only for a contact or channel you know
            can handle it.
          </DialogDescription>
        </DialogHeader>

        {/* Only the feature list scrolls, so the title and its close button stay
            reachable however many features are on. */}
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto overscroll-contain">
          <div className="rounded-md border border-border p-3">
            <FeatureRow
              title="Compress messages (MCMP)"
              description="Pack more text into a single packet with MCMP compression. The recipient must also support MCMP (meshcore-open / RemoteTerm) to read it; the compose counter then shows the compressed size."
              checked={mcmpEnabled}
              onCheckedChange={(next) =>
                onSetMcmpEnabled(conversationType, conversationId, next, mcmpVersion)
              }
              ariaLabel={mcmpEnabled ? 'Disable MCMP compression' : 'Enable MCMP compression'}
            />

            {mcmpEnabled && (
              <div className="mt-3 border-t border-border pt-3">
                <div className="mb-1.5 text-xs font-medium text-foreground">Version</div>
                <div
                  className="grid grid-cols-2 gap-1.5"
                  role="radiogroup"
                  aria-label="MCMP version"
                >
                  {MCMP_VERSIONS.map((opt) => {
                    const selected = mcmpVersion === opt.value;
                    return (
                      <button
                        key={opt.value}
                        type="button"
                        role="radio"
                        aria-checked={selected}
                        aria-label={`MCMP ${opt.label}`}
                        onClick={() =>
                          onSetMcmpEnabled(conversationType, conversationId, true, opt.value)
                        }
                        className={cn(
                          'rounded-md border px-2.5 py-1.5 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                          selected
                            ? 'border-primary bg-primary/10 text-foreground'
                            : 'border-border hover:bg-accent'
                        )}
                      >
                        <div className="font-medium">MCMP {opt.label}</div>
                        <div className="text-xs leading-snug text-muted-foreground">
                          {opt.description}
                        </div>
                      </button>
                    );
                  })}
                </div>
                <p className="mt-2 text-xs leading-snug text-muted-foreground">
                  v2 is smallest and universally readable. v3 adds a metadata container (a timestamp
                  now; signing/replies later) and is slightly larger. Both are decoded automatically
                  on the way in.
                </p>
              </div>
            )}
          </div>

          <div className="rounded-md border border-border p-3">
            <div className="text-sm font-medium text-foreground">Photo codec</div>
            <div className="mt-0.5 text-xs leading-snug text-muted-foreground">
              How an attached photo is packed for the mesh. Both ends must use the same one.
            </div>

            <div
              className="mt-2.5 grid grid-cols-2 gap-1.5"
              role="radiogroup"
              aria-label="Photo codec"
            >
              {IMAGE_CODECS.map((option) => {
                const selected = imageCodec === option.value;
                const disabled = option.value === 'aeic' && !aeicSelectable;
                return (
                  <button
                    key={option.value}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    aria-label={option.label}
                    disabled={disabled}
                    onClick={() => onSetImageCodec(conversationType, conversationId, option.value)}
                    className={cn(
                      'rounded-md border px-2.5 py-1.5 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                      selected
                        ? 'border-primary bg-primary/10 text-foreground'
                        : 'border-border hover:bg-accent',
                      disabled && 'cursor-not-allowed opacity-50 hover:bg-transparent'
                    )}
                  >
                    <div className="font-medium">{option.label}</div>
                    <div className="text-xs font-medium text-foreground/70">{option.headline}</div>
                    <div className="text-xs leading-snug text-muted-foreground">
                      {option.description}
                    </div>
                  </button>
                );
              })}
            </div>

            {aeicStatus && <AeicModelPanel status={aeicStatus} onRefresh={refreshAeic} />}

            {/* Channels only, and stated whichever codec is picked: on a channel AI
                reconstruction is the one format MCO Advanced also speaks, and the
                default is Standard -- so the interoperable choice is the one nobody
                would guess they had to make. */}
            {conversationType === 'channel' && (
              <p className="mt-2 text-xs leading-snug text-muted-foreground">
                {imageCodec === 'aeic' ? (
                  <>
                    On a channel this is the one photo codec MCO Advanced also reads — it travels as
                    the same binary format that app sends. Its own photos only appear here if it
                    sends them this way too; anything else arrives as a codec this server has no
                    decoder for, and says so in the log.
                  </>
                ) : (
                  <>
                    MCO Advanced cannot read Standard photos — it never fetches image fragments.
                    Switch this channel to AI reconstruction to exchange photos with it.
                  </>
                )}
              </p>
            )}

            {imageCodec === 'aeic' && (
              <p className="mt-2 text-xs leading-snug text-muted-foreground">
                AI reconstruction is lossy in an unusual way: the receiver gets a recognisably
                similar picture rather than the same pixels. It is the only way a 512px colour photo
                fits in one or two messages.
              </p>
            )}
          </div>

          {/* Contacts only. The raw transport is contact-directed even for a
              picture announced on a channel, so a channel has nothing to set. */}
          {conversationType === 'contact' && onSetRawMediaTextTransport && (
            <div className="rounded-md border border-border p-3">
              <FeatureRow
                title="Fetch media as text messages"
                description="Standard photos and voice notes move their fragments as raw packets, which some node firmware cannot send at all. With this on, asking for them uses ordinary messages instead — about 2.5x the airtime, but it works on every node. Turn it off to use raw packets and get a clear error where they are unsupported."
                checked={rawMediaTextTransport}
                onCheckedChange={(next) => onSetRawMediaTextTransport(conversationId, next)}
                ariaLabel={
                  rawMediaTextTransport
                    ? 'Fetch media as raw packets instead of text'
                    : 'Fetch media as text messages'
                }
              />

              <p className="mt-2 text-xs leading-snug text-muted-foreground">
                {rawMediaTextTransport ? (
                  <>
                    On: a photo you open from this contact arrives as a run of ordinary messages.
                    Replies still mirror however a request reached us, so a MeshCore SAR client that
                    asks in raw packets is answered in raw packets.
                  </>
                ) : (
                  <>
                    Off: if this node&apos;s firmware has no <code>CMD_SEND_RAW_DATA</code>, opening
                    a standard photo or voice note from this contact will fail with an error rather
                    than spend the extra airtime.
                  </>
                )}
              </p>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
