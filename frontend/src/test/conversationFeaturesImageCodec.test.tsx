/**
 * The photo-codec selector in the conversation features panel.
 *
 * This is the control the whole AEIC feature hangs off, so the tests cover the
 * three states that matter: the AI option offered, the AI option refused because
 * the server cannot run it, and the model download it offers in between.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ConversationFeaturesModal } from '../components/ConversationFeaturesModal';
import type { AeicStatus } from '../api';
import { api } from '../api';

vi.mock('../components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('../api', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api')>();
  return {
    ...original,
    api: {
      ...original.api,
      getAeicStatus: vi.fn(),
      startAeicModelDownload: vi.fn(),
      cancelAeicModelDownload: vi.fn(),
    },
  };
});

const BUNDLE_BYTES = 1_004_548_432;
// The send half: the send-side entropy graph plus the CDF tables.
const SEND_HALF_BYTES = 68_075_815;

function status(overrides: Partial<AeicStatus> = {}): AeicStatus {
  return {
    runtime_available: true,
    reconstruction_enabled: true,
    supports_encode: true,
    supports_decode: true,
    downloading: false,
    download_file: null,
    downloaded_bytes: 0,
    download_total_bytes: 0,
    installed_bytes: BUNDLE_BYTES,
    bundle_total_bytes: BUNDLE_BYTES,
    send_half_total_bytes: SEND_HALF_BYTES,
    download_scope: null,
    download_target_bytes: 0,
    download_done_bytes: 0,
    model_dir: 'data/models/aeic',
    rate_point: 'ft32',
    last_error: null,
    assets: [
      { file_name: 'a.onnx', role: 'decoder_graph', size_bytes: 1, installed: true },
      { file_name: 'b.bin', role: 'cdf_tables', size_bytes: 1, installed: true },
    ],
    ...overrides,
  };
}

describe('photo codec selector', () => {
  const onSetImageCodec = vi.fn();
  const onSetMcmpEnabled = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAeicStatus).mockResolvedValue(status());
  });

  function renderModal(
    overrides: { imageCodec?: 'ie4' | 'aeic'; conversationType?: 'contact' | 'channel' } = {}
  ) {
    return render(
      <ConversationFeaturesModal
        open
        onClose={vi.fn()}
        conversationType={overrides.conversationType ?? 'contact'}
        conversationId={'aa'.repeat(32)}
        conversationName="Alice"
        mcmpEnabled={false}
        mcmpVersion={2}
        imageCodec={overrides.imageCodec ?? 'ie4'}
        rawMediaTextTransport
        onSetMcmpEnabled={onSetMcmpEnabled}
        onSetImageCodec={onSetImageCodec}
      />
    );
  }

  /*
   * Which codec interoperates is not guessable, and the default is the one that
   * does not: a photo sent to a channel on Standard is invisible to MCO Advanced,
   * which never fetches image fragments. Channels only -- on a DM neither codec
   * reaches that app, so the note would be advice that leads nowhere.
   */
  it('says which codec MCO Advanced can read, on a channel', async () => {
    renderModal({ conversationType: 'channel', imageCodec: 'ie4' });
    expect(await screen.findByText(/cannot read Standard photos/)).toBeVisible();
    expect(screen.getByText(/Switch this channel to AI reconstruction/)).toBeVisible();
  });

  it('confirms the interoperable choice once it is made', async () => {
    renderModal({ conversationType: 'channel', imageCodec: 'aeic' });
    expect(await screen.findByText(/one photo codec MCO Advanced also reads/)).toBeVisible();
  });

  it('omits the interop note on a direct message', async () => {
    renderModal({ conversationType: 'contact', imageCodec: 'ie4' });
    await screen.findByRole('radio', { name: 'Standard' });
    expect(screen.queryByText(/MCO Advanced/)).not.toBeInTheDocument();
  });

  it('offers both codecs and marks the current one', async () => {
    renderModal({ imageCodec: 'ie4' });
    const standard = await screen.findByRole('radio', { name: 'Standard' });
    const ai = screen.getByRole('radio', { name: 'AI reconstruction' });
    expect(standard).toHaveAttribute('aria-checked', 'true');
    expect(ai).toHaveAttribute('aria-checked', 'false');
  });

  it('reflects a conversation already on the AI codec', async () => {
    renderModal({ imageCodec: 'aeic' });
    const ai = await screen.findByRole('radio', { name: 'AI reconstruction' });
    expect(ai).toHaveAttribute('aria-checked', 'true');
    // The lossiness is unusual enough that the panel says so explicitly.
    expect(screen.getByText(/recognisably\s+similar picture/i)).toBeVisible();
  });

  it('selects the AI codec for this conversation', async () => {
    renderModal({ imageCodec: 'ie4' });
    fireEvent.click(await screen.findByRole('radio', { name: 'AI reconstruction' }));
    expect(onSetImageCodec).toHaveBeenCalledWith('contact', 'aa'.repeat(32), 'aeic');
  });

  it('switches back to the standard codec', async () => {
    renderModal({ imageCodec: 'aeic' });
    fireEvent.click(await screen.findByRole('radio', { name: 'Standard' }));
    expect(onSetImageCodec).toHaveBeenCalledWith('contact', 'aa'.repeat(32), 'ie4');
  });

  it('shows the airtime difference that justifies the codec', async () => {
    renderModal();
    expect(await screen.findByText('15-40 packets')).toBeVisible();
    expect(screen.getByText('1-2 messages')).toBeVisible();
  });

  describe('when the server cannot run the codec', () => {
    it('disables the AI option and offers the model download', async () => {
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({
          supports_encode: false,
          supports_decode: false,
          installed_bytes: 0,
          assets: [
            { file_name: 'a.onnx', role: 'decoder_graph', size_bytes: 1, installed: false },
            { file_name: 'b.bin', role: 'cdf_tables', size_bytes: 1, installed: false },
          ],
        })
      );
      renderModal();
      const ai = await screen.findByRole('radio', { name: 'AI reconstruction' });
      expect(ai).toBeDisabled();
      // Both sizes are stated before the reader commits to either: 65 MB to
      // send, 958 MB to also open what others send.
      expect(await screen.findByRole('button', { name: /Get sending working/ })).toBeVisible();
      expect(screen.getByRole('button', { name: /Whole model/ })).toBeVisible();
      expect(screen.getAllByText(/958 MB/)).toHaveLength(2);

      fireEvent.click(ai);
      expect(onSetImageCodec).not.toHaveBeenCalled();
    });

    it('starts the whole download and then re-reads the status', async () => {
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({ supports_encode: false, supports_decode: false, installed_bytes: 0 })
      );
      vi.mocked(api.startAeicModelDownload).mockResolvedValue(status({ downloading: true }));
      renderModal();
      fireEvent.click(await screen.findByRole('button', { name: /Whole model/ }));
      await waitFor(() => expect(api.startAeicModelDownload).toHaveBeenCalledWith('full'));
      // Once on mount, once after the download starts.
      await waitFor(() => expect(api.getAeicStatus).toHaveBeenCalledTimes(2));
    });

    it('can fetch the send half alone', async () => {
      // The server fetches this by itself, so the button is for someone who
      // would rather not wait -- and it must not pull the other 893 MB.
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({ supports_encode: false, supports_decode: false, installed_bytes: 0 })
      );
      vi.mocked(api.startAeicModelDownload).mockResolvedValue(status({ downloading: true }));
      renderModal();
      fireEvent.click(await screen.findByRole('button', { name: /Get sending working/ }));
      await waitFor(() => expect(api.startAeicModelDownload).toHaveBeenCalledWith('send'));
    });

    it('names the env var to set when the codec is switched off', async () => {
      // Not "reinstall with the aeic extra": the runtime is enabled with an
      // environment variable and a restart, and run.sh installs the
      // dependencies on that start. Telling the operator to rebuild sends them
      // down a path they do not need.
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({ runtime_available: false, supports_encode: false, supports_decode: false })
      );
      renderModal();
      expect(await screen.findByText('MESHCORE_ENABLE_AEIC=true')).toBeVisible();
      expect(screen.getByText(/switched off on this server/)).toBeVisible();
      expect(screen.getByText(/no rebuild needed/)).toBeVisible();
      expect(screen.queryByRole('button', { name: /Download model/ })).not.toBeInTheDocument();
    });

    it('shows progress and a cancel action while downloading', async () => {
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({
          supports_encode: false,
          supports_decode: false,
          downloading: true,
          download_file: 'aeic_decoder_qdq_conv_pct.onnx.data',
          downloaded_bytes: BUNDLE_BYTES / 2,
          installed_bytes: 0,
          download_scope: 'full',
          download_target_bytes: BUNDLE_BYTES,
          download_done_bytes: BUNDLE_BYTES / 2,
        })
      );
      renderModal();
      expect(await screen.findByText(/aeic_decoder_qdq_conv_pct.onnx.data — 50%/)).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      await waitFor(() => expect(api.cancelAeicModelDownload).toHaveBeenCalledTimes(1));
    });

    it('lets the AI codec be chosen once sending works, and asks for the rest', async () => {
      // The whole point of the split: 65 MB on disk and the AI codec is
      // selectable, with no 1.4 GB decode ever happening on this host.
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({
          supports_encode: true,
          supports_decode: false,
          installed_bytes: SEND_HALF_BYTES,
        })
      );
      renderModal();

      expect(await screen.findByRole('radio', { name: 'AI reconstruction' })).toBeEnabled();
      expect(screen.getByText(/Sending works/)).toBeVisible();
      expect(screen.getByRole('button', { name: /Download the rest/ })).toBeVisible();
      // The rest, not the whole thing again: the send half is already on disk.
      expect(screen.getByRole('button', { name: /893 MB/ })).toBeVisible();
      expect(screen.queryByRole('button', { name: /Get sending working/ })).not.toBeInTheDocument();
    });

    it('measures a send-half download against the send half', async () => {
      // Against the 958 MB bundle this would read 3% and then stop, which looks
      // like a download that died.
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({
          supports_encode: false,
          supports_decode: false,
          downloading: true,
          download_file: 'aeic_entropy_side_fp32_op17.onnx',
          installed_bytes: 0,
          download_scope: 'send',
          download_target_bytes: SEND_HALF_BYTES,
          download_done_bytes: SEND_HALF_BYTES / 2,
        })
      );
      renderModal();

      expect(await screen.findByText(/Getting ready to send \(65 MB\) — 50%/)).toBeVisible();
    });

    it('keeps sending selectable when rebuilding is switched off', async () => {
      // MESHCORE_ENABLE_AEIC=false is about the ~1.4 GB rebuild, not the codec.
      // The old panel showed "switched off, set MESHCORE_ENABLE_AEIC=true" here,
      // which both hid a working sender and told the reader to install a
      // dependency that was already installed.
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({
          runtime_available: true,
          reconstruction_enabled: false,
          supports_encode: true,
          supports_decode: false,
          installed_bytes: SEND_HALF_BYTES,
        })
      );
      renderModal();

      expect(await screen.findByRole('radio', { name: 'AI reconstruction' })).toBeEnabled();
      expect(screen.getByText(/Rebuilding photos other people send is switched off/)).toBeVisible();
      expect(screen.getByText('MESHCORE_ENABLE_AEIC=false')).toBeVisible();
      // No offer to fetch 893 MB of weights that nothing is allowed to load.
      expect(screen.queryByRole('button', { name: /Download the rest/ })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /Whole model/ })).not.toBeInTheDocument();
    });

    it('still offers the send half when rebuilding is off and nothing is installed', async () => {
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({
          runtime_available: true,
          reconstruction_enabled: false,
          supports_encode: false,
          supports_decode: false,
          installed_bytes: 0,
        })
      );
      vi.mocked(api.startAeicModelDownload).mockResolvedValue(status({ downloading: true }));
      renderModal();

      fireEvent.click(await screen.findByRole('button', { name: /Get sending working/ }));

      await waitFor(() => expect(api.startAeicModelDownload).toHaveBeenCalledWith('send'));
    });

    it('surfaces a previous failure so it is not silently retried forever', async () => {
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({
          supports_encode: false,
          supports_decode: false,
          installed_bytes: 0,
          last_error: 'ConnectError: connection reset',
        })
      );
      renderModal();
      expect(await screen.findByText('ConnectError: connection reset')).toBeVisible();
    });
  });

  it('leaves the AI option disabled when the status endpoint is unavailable', async () => {
    // An older server without the endpoint must not break the panel.
    vi.mocked(api.getAeicStatus).mockRejectedValue(new Error('404'));
    renderModal();
    const ai = await screen.findByRole('radio', { name: 'AI reconstruction' });
    await waitFor(() => expect(ai).toBeDisabled());
    expect(screen.getByRole('radio', { name: 'Standard' })).toBeEnabled();
  });
});
