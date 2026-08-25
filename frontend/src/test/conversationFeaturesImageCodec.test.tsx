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

function status(overrides: Partial<AeicStatus> = {}): AeicStatus {
  return {
    runtime_available: true,
    supports_encode: true,
    supports_decode: true,
    downloading: false,
    download_file: null,
    downloaded_bytes: 0,
    download_total_bytes: 0,
    installed_bytes: BUNDLE_BYTES,
    bundle_total_bytes: BUNDLE_BYTES,
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

  function renderModal(overrides: { imageCodec?: 'ie4' | 'aeic' } = {}) {
    return render(
      <ConversationFeaturesModal
        open
        onClose={vi.fn()}
        conversationType="contact"
        conversationId={'aa'.repeat(32)}
        conversationName="Alice"
        mcmpEnabled={false}
        mcmpVersion={2}
        imageCodec={overrides.imageCodec ?? 'ie4'}
        rawMediaTextFallback
        onSetMcmpEnabled={onSetMcmpEnabled}
        onSetImageCodec={onSetImageCodec}
      />
    );
  }

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
      expect(await screen.findByRole('button', { name: /Download model/ })).toBeVisible();
      // 958 MB, so the size is stated before the user commits to it -- both in
      // the explanation and on the button itself.
      expect(screen.getAllByText(/958 MB/)).toHaveLength(2);

      fireEvent.click(ai);
      expect(onSetImageCodec).not.toHaveBeenCalled();
    });

    it('starts the download and then re-reads the status', async () => {
      vi.mocked(api.getAeicStatus).mockResolvedValue(
        status({ supports_encode: false, supports_decode: false, installed_bytes: 0 })
      );
      vi.mocked(api.startAeicModelDownload).mockResolvedValue(status({ downloading: true }));
      renderModal();
      fireEvent.click(await screen.findByRole('button', { name: /Download model/ }));
      await waitFor(() => expect(api.startAeicModelDownload).toHaveBeenCalledTimes(1));
      // Once on mount, once after the download starts.
      await waitFor(() => expect(api.getAeicStatus).toHaveBeenCalledTimes(2));
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
        })
      );
      renderModal();
      expect(await screen.findByText(/aeic_decoder_qdq_conv_pct.onnx.data — 50%/)).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      await waitFor(() => expect(api.cancelAeicModelDownload).toHaveBeenCalledTimes(1));
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
