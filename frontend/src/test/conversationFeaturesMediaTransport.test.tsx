/**
 * The "send media as text" switch in the conversation features panel.
 *
 * It exists because a node whose firmware has no CMD_SEND_RAW_DATA cannot open a
 * received photo or voice note at all. On by default, so the tests care most
 * about the two ways it can be wrong: absent for a channel (where the setting
 * has nothing to act on), and silently defaulting to off.
 */

import { fireEvent, render, screen } from '@testing-library/react';
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

function status(): AeicStatus {
  return {
    runtime_available: true,
    reconstruction_enabled: true,
    supports_encode: true,
    supports_decode: true,
    downloading: false,
    download_file: null,
    downloaded_bytes: 0,
    download_total_bytes: 0,
    installed_bytes: 1,
    bundle_total_bytes: 1,
    send_half_total_bytes: 1,
    download_scope: null,
    download_target_bytes: 0,
    download_done_bytes: 0,
    model_dir: 'data/models/aeic',
    rate_point: 'ft32',
    last_error: null,
    assets: [],
  };
}

const CONTACT_ID = 'aa'.repeat(32);

describe('media text fallback switch', () => {
  const onSetRawMediaTextTransport = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAeicStatus).mockResolvedValue(status());
  });

  function renderModal(
    overrides: {
      rawMediaTextTransport?: boolean;
      conversationType?: 'contact' | 'channel';
      onSetRawMediaTextTransport?: typeof onSetRawMediaTextTransport;
    } = {}
  ) {
    return render(
      <ConversationFeaturesModal
        open
        onClose={vi.fn()}
        conversationType={overrides.conversationType ?? 'contact'}
        conversationId={CONTACT_ID}
        conversationName="Alice"
        mcmpEnabled={false}
        mcmpVersion={2}
        imageCodec="ie4"
        rawMediaTextTransport={overrides.rawMediaTextTransport ?? true}
        onSetMcmpEnabled={vi.fn()}
        onSetImageCodec={vi.fn()}
        onSetRawMediaTextTransport={
          'onSetRawMediaTextTransport' in overrides
            ? overrides.onSetRawMediaTextTransport
            : onSetRawMediaTextTransport
        }
      />
    );
  }

  it('shows the switch on for a contact by default', () => {
    renderModal();

    // The label names the action the switch would perform, which is how it reports
    // being on: from here, the only thing left to do is go back to raw packets.
    expect(screen.getByLabelText('Fetch media as raw packets instead of text')).toBeInTheDocument();
  });

  it('turns the text transport off through the callback', () => {
    renderModal({ rawMediaTextTransport: true });

    fireEvent.click(screen.getByLabelText('Fetch media as raw packets instead of text'));

    expect(onSetRawMediaTextTransport).toHaveBeenCalledWith(CONTACT_ID, false);
  });

  it('turns the text transport back on through the callback', () => {
    renderModal({ rawMediaTextTransport: false });

    fireEvent.click(screen.getByLabelText('Fetch media as text messages'));

    expect(onSetRawMediaTextTransport).toHaveBeenCalledWith(CONTACT_ID, true);
  });

  it('explains what each position means', () => {
    renderModal({ rawMediaTextTransport: false });
    expect(screen.getByText(/will fail with an error/)).toBeInTheDocument();

    /*
     * On is explained too, and not left blank. It is the default, so it is the
     * state most people are in, and the surprising part of it -- that a SAR client
     * asking in raw packets is still answered in raw packets -- is exactly what an
     * always-on switch would otherwise hide.
     */
    renderModal({ rawMediaTextTransport: true });
    expect(screen.getByText(/answered in raw packets/)).toBeInTheDocument();
  });

  it('is absent for a channel', () => {
    /*
     * The raw media transport is contact-directed even for a picture announced on
     * a channel: the fetch request goes to the sender's contact, so that contact's
     * setting governs. A channel switch would be dead UI.
     */
    renderModal({ conversationType: 'channel' });

    expect(
      screen.queryByLabelText('Fetch media as raw packets instead of text')
    ).not.toBeInTheDocument();
  });

  it('is absent when no handler is wired', () => {
    renderModal({ onSetRawMediaTextTransport: undefined });

    expect(
      screen.queryByLabelText('Fetch media as raw packets instead of text')
    ).not.toBeInTheDocument();
  });
});
