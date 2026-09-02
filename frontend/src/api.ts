import type {
  AppSettings,
  AppSettingsUpdate,
  BulkCreateHashtagChannelsResult,
  Channel,
  ChannelDetail,
  CommandResponse,
  Contact,
  ContactAnalytics,
  ContactAdvertPathSummary,
  ContactTelemetryResponse,
  FanoutConfig,
  HealthStatus,
  MaintenanceResult,
  Message,
  MessagesAroundResponse,
  RawPacket,
  RadioAdvertMode,
  RadioConfig,
  RadioConfigUpdate,
  RadioDiscoveryResponse,
  RadioRegionDiscoveryResponse,
  RadioTraceHopRequest,
  RadioTraceResponse,
  RadioDiscoveryTarget,
  PathDiscoveryResponse,
  PushSubscriptionInfo,
  MessageActionResponse,
  ResendChannelMessageResponse,
  RepeaterAclResponse,
  RepeaterAdvertIntervalsResponse,
  RepeaterLoginResponse,
  RoomPollConfigRequest,
  RoomPollStatus,
  RepeaterLppTelemetryResponse,
  RepeaterNeighborsResponse,
  RepeaterNodeInfoResponse,
  RepeaterOwnerInfoResponse,
  RepeaterRadioSettingsResponse,
  NodeStatsResponse,
  RepeaterRegionsResponse,
  RepeaterStatusResponse,
  TelemetryHistoryEntry,
  TelemetrySchedule,
  ClockSyncRepeaterResponse,
  TrackedTelemetryContactsResponse,
  TrackedTelemetryResponse,
  StatisticsResponse,
  StatsWindow,
  TraceResponse,
  UnreadCounts,
  Bot,
  BotEngineSettings,
  BotEngineStatus,
  BotFeed,
  BotLibraryEntry,
  BotLogEntry,
  BotRun,
  BotSchedule,
  BotStats,
  BotTestResponse,
  BotUpdatePayload,
  VirtualNodeOverview,
} from './types';

const API_BASE = './api';

/** Which transport a fetch for this session travels on. Text is ~10x slower. */
export type MediaTransport = 'raw' | 'text';

export interface VoiceSessionStatus {
  session_id: string;
  state: string;
  duration_ms: number;
  packet_count: number;
  received_count: number;
  missing_indices: number[];
  transport: MediaTransport;
}

/** Which codec a conversation uses for outbound photos. */
export type ImageCodecId = 'ie4' | 'aeic';

export interface AeicAssetStatus {
  file_name: string;
  role: string;
  size_bytes: number;
  installed: boolean;
}

/** Whether this server can run the AEIC codec, and how the model download is going. */
export interface AeicStatus {
  runtime_available: boolean;
  /** False with MESHCORE_ENABLE_AEIC=false: rebuilding is off, sending is not. */
  reconstruction_enabled: boolean;
  supports_encode: boolean;
  supports_decode: boolean;
  downloading: boolean;
  download_file: string | null;
  downloaded_bytes: number;
  download_total_bytes: number;
  installed_bytes: number;
  bundle_total_bytes: number;
  /** Bytes of the bundle that sending needs; the rest only reconstructs. */
  send_half_total_bytes: number;
  download_scope: 'send' | 'full' | null;
  download_target_bytes: number;
  download_done_bytes: number;
  model_dir: string;
  rate_point: string;
  last_error: string | null;
  assets: AeicAssetStatus[];
}

export interface AeicSessionStatus {
  session_key: string;
  message_id: number | null;
  state: string;
  square_size: number;
  aspect_code: number;
  rate_code: number;
  total_chunks: number;
  received_chunks: number;
  missing_indices: number[];
  bitstream_bytes: number;
  decoded: boolean;
  decode_error: string | null;
}

export interface AeicSendResult {
  session_key: string;
  bitstream_bytes: number;
  chunk_count: number;
  messages: unknown[];
}

/** Media that arrived in a codec this server has no decoder for, and kept. */
export interface UnsupportedMediaStatus {
  id: number;
  conversation_key: string;
  data_type: number;
  codec_label: string;
  received_at: number;
  blob_count: number;
  total_bytes: number;
  decoded: boolean;
  reason: string;
}

export interface ImageSessionStatus {
  session_id: string;
  state: string;
  format: 0 | 1;
  width: number;
  height: number;
  size_bytes: number;
  fragment_count: number;
  received_count: number;
  missing_indices: number[];
  transport: MediaTransport;
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const hasBody = options?.body !== undefined;
  const res = await fetch(`${API_BASE}${url}`, {
    ...options,
    headers: {
      ...(hasBody && { 'Content-Type': 'application/json' }),
      ...options?.headers,
    },
  });
  if (!res.ok) {
    const errorText = await res.text();
    // FastAPI returns errors as {"detail": "message"}, extract the message
    let errorMessage = errorText || res.statusText;
    try {
      const errorJson = JSON.parse(errorText);
      if (errorJson.detail) {
        errorMessage = errorJson.detail;
      }
    } catch {
      // Not JSON, use raw text
    }
    throw new Error(errorMessage);
  }
  return res.json();
}

/** Check if an error is an AbortError (request was cancelled) */
export function isAbortError(err: unknown): boolean {
  // DOMException is thrown by fetch when aborted, and it's not an Error subclass
  if (err instanceof DOMException && err.name === 'AbortError') {
    return true;
  }
  // Also check for Error with AbortError name (for compatibility)
  return err instanceof Error && err.name === 'AbortError';
}

interface DecryptResult {
  started: boolean;
  total_packets: number;
  message: string;
}

export const api = {
  sendImage: async (
    conversationType: 'PRIV' | 'CHAN',
    conversationKey: string,
    image: { blob: Blob; format: 0 | 1; width: number; height: number }
  ) => {
    const query = new URLSearchParams({
      conversation_type: conversationType,
      conversation_key: conversationKey,
      format_id: String(image.format),
      width: String(image.width),
      height: String(image.height),
    });
    const response = await fetch(`${API_BASE}/images/send?${query}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: image.blob,
    });
    if (!response.ok)
      throw new Error((await response.json().catch(() => null))?.detail || response.statusText);
    return response.json();
  },
  fetchImage: (messageId: number) =>
    fetchJson<ImageSessionStatus>(`/images/messages/${messageId}/fetch`, { method: 'POST' }),
  getImageSession: (sessionId: string) =>
    fetchJson<ImageSessionStatus>(`/images/sessions/${sessionId}`),
  imageContentUrl: (sessionId: string) => `${API_BASE}/images/sessions/${sessionId}/content`,
  /**
   * Send a photo with the AEIC neural codec. The body is raw 512x512 packed RGB
   * (786,432 bytes) prepared by `prepareAeicImage`; the server encodes it to
   * ~150 bytes and transmits one or two `aei1:` text messages.
   */
  sendAeicImage: async (
    conversationType: 'PRIV' | 'CHAN',
    conversationKey: string,
    image: { rgb: Uint8Array; sourceWidth: number; sourceHeight: number }
  ) => {
    const query = new URLSearchParams({
      conversation_type: conversationType,
      conversation_key: conversationKey,
      source_width: String(image.sourceWidth),
      source_height: String(image.sourceHeight),
    });
    const response = await fetch(`${API_BASE}/aeic/send?${query}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: image.rgb as unknown as BodyInit,
    });
    if (!response.ok)
      throw new Error((await response.json().catch(() => null))?.detail || response.statusText);
    return response.json() as Promise<AeicSendResult>;
  },
  getAeicStatus: () => fetchJson<AeicStatus>('/aeic/status'),
  startAeicModelDownload: (scope?: 'send' | 'full') =>
    fetchJson<AeicStatus>(`/aeic/model/download${scope ? `?scope=${scope}` : ''}`, {
      method: 'POST',
    }),
  cancelAeicModelDownload: () =>
    fetchJson<AeicStatus>('/aeic/model/download/cancel', { method: 'POST' }),
  getAeicSessionForMessage: (messageId: number) =>
    fetchJson<AeicSessionStatus>(`/aeic/messages/${messageId}`),
  getAeicSession: (sessionKey: string) =>
    fetchJson<AeicSessionStatus>(`/aeic/sessions/${encodeURIComponent(sessionKey)}`),
  getUnsupportedMedia: (mediaId: number) =>
    fetchJson<UnsupportedMediaStatus>(`/unsupported-media/${mediaId}`),
  retryUnsupportedMediaDecode: (mediaId: number) =>
    fetchJson<UnsupportedMediaStatus>(`/unsupported-media/${mediaId}/decode`, { method: 'POST' }),
  retryAeicDecode: (sessionKey: string) =>
    fetchJson<AeicSessionStatus>(`/aeic/sessions/${encodeURIComponent(sessionKey)}/decode`, {
      method: 'POST',
    }),
  aeicContentUrl: (sessionKey: string) =>
    `${API_BASE}/aeic/sessions/${encodeURIComponent(sessionKey)}/content`,
  setImageCodec: (type: 'contact' | 'channel', id: string, codec: ImageCodecId) =>
    fetchJson<{ type: string; id: string; codec: ImageCodecId }>('/settings/image-codec/set', {
      method: 'POST',
      body: JSON.stringify({ type, id, codec }),
    }),
  /** Contacts only: the raw media transport is contact-directed even on a channel. */
  setRawMediaTextTransport: (id: string, enabled: boolean) =>
    fetchJson<{ id: string; enabled: boolean }>('/settings/raw-media-text-transport/set', {
      method: 'POST',
      body: JSON.stringify({ id, enabled }),
    }),
  sendVoice: async (conversationType: 'PRIV' | 'CHAN', conversationKey: string, pcm: Blob) => {
    const response = await fetch(
      `${API_BASE}/voice/send?conversation_type=${conversationType}&conversation_key=${encodeURIComponent(conversationKey)}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: pcm,
      }
    );
    if (!response.ok)
      throw new Error((await response.json().catch(() => null))?.detail || response.statusText);
    return response.json();
  },
  fetchVoice: (messageId: number) =>
    fetchJson<VoiceSessionStatus>(`/voice/messages/${messageId}/fetch`, { method: 'POST' }),
  getVoiceSession: (sessionId: string) =>
    fetchJson<VoiceSessionStatus>(`/voice/sessions/${sessionId}`),
  voiceAudioUrl: (sessionId: string) => `${API_BASE}/voice/sessions/${sessionId}/audio`,
  // Health
  getHealth: () => fetchJson<HealthStatus>('/health'),

  // Radio config
  getRadioConfig: () => fetchJson<RadioConfig>('/radio/config'),
  updateRadioConfig: (config: RadioConfigUpdate) =>
    fetchJson<RadioConfig>('/radio/config', {
      method: 'PATCH',
      body: JSON.stringify(config),
    }),
  getPrivateKey: () => fetchJson<{ private_key: string }>('/radio/private-key'),
  setPrivateKey: (privateKey: string) =>
    fetchJson<{ status: string }>('/radio/private-key', {
      method: 'PUT',
      body: JSON.stringify({ private_key: privateKey }),
    }),
  sendAdvertisement: (mode: RadioAdvertMode = 'flood') =>
    fetchJson<{ status: string }>('/radio/advertise', {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),
  discoverMesh: (target: RadioDiscoveryTarget) =>
    fetchJson<RadioDiscoveryResponse>('/radio/discover', {
      method: 'POST',
      body: JSON.stringify({ target }),
    }),
  discoverRegions: (publicKeys?: string[]) =>
    fetchJson<RadioRegionDiscoveryResponse>('/radio/discover-regions', {
      method: 'POST',
      body: JSON.stringify(publicKeys && publicKeys.length > 0 ? { public_keys: publicKeys } : {}),
    }),
  requestRadioTrace: (hopHashBytes: 1 | 2 | 4, hops: RadioTraceHopRequest[]) =>
    fetchJson<RadioTraceResponse>('/radio/trace', {
      method: 'POST',
      body: JSON.stringify({ hop_hash_bytes: hopHashBytes, hops }),
    }),
  rebootRadio: () =>
    fetchJson<{ status: string; message: string }>('/radio/reboot', {
      method: 'POST',
    }),
  disconnectRadio: () =>
    fetchJson<{ status: string; message: string; connected: boolean; paused: boolean }>(
      '/radio/disconnect',
      {
        method: 'POST',
      }
    ),
  reconnectRadio: () =>
    fetchJson<{ status: string; message: string; connected: boolean }>('/radio/reconnect', {
      method: 'POST',
    }),

  // Contacts
  getContacts: (limit = 100, offset = 0) =>
    fetchJson<Contact[]>(`/contacts?limit=${limit}&offset=${offset}`),
  getRepeaterAdvertPaths: (limitPerRepeater = 10) =>
    fetchJson<ContactAdvertPathSummary[]>(
      `/contacts/repeaters/advert-paths?limit_per_repeater=${limitPerRepeater}`
    ),
  getContactAnalytics: (params: { publicKey?: string; name?: string }, signal?: AbortSignal) => {
    const searchParams = new URLSearchParams();
    if (params.publicKey) searchParams.set('public_key', params.publicKey);
    if (params.name) searchParams.set('name', params.name);
    return fetchJson<ContactAnalytics>(`/contacts/analytics?${searchParams.toString()}`, {
      signal,
    });
  },
  deleteContact: (publicKey: string) =>
    fetchJson<{ status: string }>(`/contacts/${publicKey}`, {
      method: 'DELETE',
    }),
  bulkDeleteContacts: (publicKeys: string[]) =>
    fetchJson<{ deleted: number }>('/contacts/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ public_keys: publicKeys }),
    }),
  createContact: (publicKey: string, name?: string, tryHistorical?: boolean, type?: number) =>
    fetchJson<Contact>('/contacts', {
      method: 'POST',
      body: JSON.stringify({ public_key: publicKey, name, type, try_historical: tryHistorical }),
    }),
  markContactRead: (publicKey: string) =>
    fetchJson<{ status: string; public_key: string }>(`/contacts/${publicKey}/mark-read`, {
      method: 'POST',
    }),
  sendRepeaterCommand: (publicKey: string, command: string) =>
    fetchJson<CommandResponse>(`/contacts/${publicKey}/command`, {
      method: 'POST',
      body: JSON.stringify({ command }),
    }),
  requestTrace: (publicKey: string) =>
    fetchJson<TraceResponse>(`/contacts/${publicKey}/trace`, {
      method: 'POST',
    }),
  requestPathDiscovery: (publicKey: string) =>
    fetchJson<PathDiscoveryResponse>(`/contacts/${publicKey}/path-discovery`, {
      method: 'POST',
    }),
  setContactRoutingOverride: (publicKey: string, route: string) =>
    fetchJson<{ status: string; public_key: string }>(`/contacts/${publicKey}/routing-override`, {
      method: 'POST',
      body: JSON.stringify({ route }),
    }),

  // Channels
  getChannels: () => fetchJson<Channel[]>('/channels'),
  createChannel: (name: string, key?: string) =>
    fetchJson<Channel>('/channels', {
      method: 'POST',
      body: JSON.stringify({ name, key }),
    }),
  bulkCreateHashtagChannels: (channelNames: string[], tryHistorical?: boolean) =>
    fetchJson<BulkCreateHashtagChannelsResult>('/channels/bulk-hashtag', {
      method: 'POST',
      body: JSON.stringify({ channel_names: channelNames, try_historical: tryHistorical }),
    }),
  deleteChannel: (key: string) =>
    fetchJson<{ status: string }>(`/channels/${key}`, { method: 'DELETE' }),
  getChannelDetail: (key: string) => fetchJson<ChannelDetail>(`/channels/${key}/detail`),
  markChannelRead: (key: string) =>
    fetchJson<{ status: string; key: string }>(`/channels/${key}/mark-read`, {
      method: 'POST',
    }),
  setChannelFloodScopeOverride: (key: string, floodScopeOverride: string) =>
    fetchJson<Channel>(`/channels/${key}/flood-scope-override`, {
      method: 'POST',
      body: JSON.stringify({ flood_scope_override: floodScopeOverride }),
    }),

  setChannelPathHashModeOverride: (key: string, pathHashModeOverride: number | null) =>
    fetchJson<Channel>(`/channels/${key}/path-hash-mode-override`, {
      method: 'POST',
      body: JSON.stringify({ path_hash_mode_override: pathHashModeOverride }),
    }),

  // Messages
  getMessages: (
    params?: {
      limit?: number;
      offset?: number;
      type?: 'PRIV' | 'CHAN';
      conversation_key?: string;
      before?: number;
      before_id?: number;
      after?: number;
      after_id?: number;
      q?: string;
    },
    signal?: AbortSignal
  ) => {
    const searchParams = new URLSearchParams();
    if (params?.limit !== undefined) searchParams.set('limit', params.limit.toString());
    if (params?.offset !== undefined) searchParams.set('offset', params.offset.toString());
    if (params?.type) searchParams.set('type', params.type);
    if (params?.conversation_key) searchParams.set('conversation_key', params.conversation_key);
    if (params?.before !== undefined) searchParams.set('before', params.before.toString());
    if (params?.before_id !== undefined) searchParams.set('before_id', params.before_id.toString());
    if (params?.after !== undefined) searchParams.set('after', params.after.toString());
    if (params?.after_id !== undefined) searchParams.set('after_id', params.after_id.toString());
    if (params?.q) searchParams.set('q', params.q);
    const query = searchParams.toString();
    return fetchJson<Message[]>(`/messages${query ? `?${query}` : ''}`, { signal });
  },
  getMessagesAround: (
    messageId: number,
    type?: 'PRIV' | 'CHAN',
    conversationKey?: string,
    signal?: AbortSignal
  ) => {
    const searchParams = new URLSearchParams();
    if (type) searchParams.set('type', type);
    if (conversationKey) searchParams.set('conversation_key', conversationKey);
    const query = searchParams.toString();
    return fetchJson<MessagesAroundResponse>(
      `/messages/around/${messageId}${query ? `?${query}` : ''}`,
      { signal }
    );
  },
  sendDirectMessage: (destination: string, text: string) =>
    fetchJson<Message>('/messages/direct', {
      method: 'POST',
      body: JSON.stringify({ destination, text }),
    }),
  sendChannelMessage: (channelKey: string, text: string) =>
    fetchJson<Message>('/messages/channel', {
      method: 'POST',
      body: JSON.stringify({ channel_key: channelKey, text }),
    }),
  resendChannelMessage: (messageId: number, newTimestamp?: boolean) =>
    fetchJson<ResendChannelMessageResponse>(
      `/messages/channel/${messageId}/resend${newTimestamp ? '?new_timestamp=true' : ''}`,
      { method: 'POST' }
    ),
  /**
   * Send a MeshCore Open Advanced compatible emoji reaction ("remoji") to a
   * message. Returns the target message with its updated reactions map.
   */
  reactToMessage: (messageId: number, emoji: string) =>
    fetchJson<Message>(`/messages/${messageId}/react`, {
      method: 'POST',
      body: JSON.stringify({ emoji }),
    }),
  /**
   * Retransmit an outgoing message. Direct messages go out byte-identical under
   * their original timestamp (so the recipient dedups it as a retry rather than
   * showing it twice) and restart their retry run; `newTimestamp` applies to
   * channel messages only, where it creates a new message row.
   */
  retryMessage: (messageId: number, newTimestamp?: boolean) =>
    fetchJson<MessageActionResponse>(
      `/messages/${messageId}/retry${newTimestamp ? '?new_timestamp=true' : ''}`,
      { method: 'POST' }
    ),
  /** Stop the attempts not yet made. Whatever is already on air cannot be recalled. */
  cancelMessage: (messageId: number) =>
    fetchJson<MessageActionResponse>(`/messages/${messageId}/cancel`, { method: 'POST' }),
  /** Drop our copy and stop retransmitting. The mesh has no unsend. */
  deleteMessage: (messageId: number) =>
    fetchJson<MessageActionResponse>(`/messages/${messageId}`, { method: 'DELETE' }),

  // Packets
  getPacket: (packetId: number) => fetchJson<RawPacket>(`/packets/${packetId}`),
  getUndecryptedPacketCount: () => fetchJson<{ count: number }>('/packets/undecrypted/count'),
  decryptHistoricalPackets: (params: {
    key_type: 'channel' | 'contact';
    channel_key?: string;
    channel_name?: string;
  }) =>
    fetchJson<DecryptResult>('/packets/decrypt/historical', {
      method: 'POST',
      body: JSON.stringify(params),
    }),
  runMaintenance: (options: { pruneUndecryptedDays?: number; purgeLinkedRawPackets?: boolean }) =>
    fetchJson<MaintenanceResult>('/packets/maintenance', {
      method: 'POST',
      body: JSON.stringify({
        ...(options.pruneUndecryptedDays !== undefined && {
          prune_undecrypted_days: options.pruneUndecryptedDays,
        }),
        ...(options.purgeLinkedRawPackets !== undefined && {
          purge_linked_raw_packets: options.purgeLinkedRawPackets,
        }),
      }),
    }),

  // Read State
  getUnreads: () => fetchJson<UnreadCounts>('/read-state/unreads'),
  markAllRead: () =>
    fetchJson<{ status: string; timestamp: number }>('/read-state/mark-all-read', {
      method: 'POST',
    }),

  // Virtual companion node (other MeshCore apps using this radio through the server)
  getVirtualNode: () => fetchJson<VirtualNodeOverview>('/virtual-node'),
  forgetVirtualNodeClient: (clientId: string) =>
    fetchJson<{ status: string; client_id: string }>(
      `/virtual-node/clients/${encodeURIComponent(clientId)}`,
      { method: 'DELETE' }
    ),
  disconnectVirtualNodeClient: (peer: string) =>
    fetchJson<{ status: string; peer: string }>(
      `/virtual-node/connections/${encodeURIComponent(peer)}/disconnect`,
      { method: 'POST' }
    ),

  // App Settings
  getSettings: () => fetchJson<AppSettings>('/settings'),
  updateSettings: (settings: AppSettingsUpdate) =>
    fetchJson<AppSettings>('/settings', {
      method: 'PATCH',
      body: JSON.stringify(settings),
    }),

  // Block lists
  toggleBlockedKey: (key: string) =>
    fetchJson<AppSettings>('/settings/blocked-keys/toggle', {
      method: 'POST',
      body: JSON.stringify({ key }),
    }),
  toggleBlockedName: (name: string) =>
    fetchJson<AppSettings>('/settings/blocked-names/toggle', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),

  // Tracked telemetry
  toggleTrackedTelemetry: (publicKey: string) =>
    fetchJson<TrackedTelemetryResponse>('/settings/tracked-telemetry/toggle', {
      method: 'POST',
      body: JSON.stringify({ public_key: publicKey }),
    }),

  getTelemetrySchedule: () => fetchJson<TelemetrySchedule>('/settings/tracked-telemetry/schedule'),

  toggleClockSyncRepeater: (publicKey: string) =>
    fetchJson<ClockSyncRepeaterResponse>('/settings/clock-sync-repeaters/toggle', {
      method: 'POST',
      body: JSON.stringify({ public_key: publicKey }),
    }),

  // Tracked contact telemetry
  toggleTrackedTelemetryContact: (publicKey: string) =>
    fetchJson<TrackedTelemetryContactsResponse>('/settings/tracked-telemetry-contacts/toggle', {
      method: 'POST',
      body: JSON.stringify({ public_key: publicKey }),
    }),

  getContactTelemetrySchedule: () =>
    fetchJson<TelemetrySchedule>('/settings/tracked-telemetry-contacts/schedule'),

  // Favorites
  toggleFavorite: (type: 'channel' | 'contact', id: string) =>
    fetchJson<{ type: string; id: string; favorite: boolean }>('/settings/favorites/toggle', {
      method: 'POST',
      body: JSON.stringify({ type, id }),
    }),

  toggleChannelMute: (key: string) =>
    fetchJson<{ key: string; muted: boolean }>('/settings/muted-channels/toggle', {
      method: 'POST',
      body: JSON.stringify({ key }),
    }),

  // MCMP compression (per conversation)
  setMcmpEnabled: (type: 'channel' | 'contact', id: string, enabled: boolean, version?: number) =>
    fetchJson<{ type: 'channel' | 'contact'; id: string; enabled: boolean; version: number }>(
      '/settings/mcmp/set',
      {
        method: 'POST',
        body: JSON.stringify({ type, id, enabled, version }),
      }
    ),

  estimateMcmp: (text: string, version = 2) =>
    fetchJson<{ wire_bytes: number; compressed: boolean }>('/messages/mcmp-estimate', {
      method: 'POST',
      body: JSON.stringify({ text, version }),
    }),

  // Fanout
  getFanoutConfigs: () => fetchJson<FanoutConfig[]>('/fanout'),
  createFanoutConfig: (config: {
    type: string;
    name: string;
    config: Record<string, unknown>;
    scope: Record<string, unknown>;
    enabled?: boolean;
  }) =>
    fetchJson<FanoutConfig>('/fanout', {
      method: 'POST',
      body: JSON.stringify(config),
    }),
  updateFanoutConfig: (
    id: string,
    update: {
      name?: string;
      config?: Record<string, unknown>;
      scope?: Record<string, unknown>;
      enabled?: boolean;
    }
  ) =>
    fetchJson<FanoutConfig>(`/fanout/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(update),
    }),
  deleteFanoutConfig: (id: string) =>
    fetchJson<{ deleted: boolean }>(`/fanout/${id}`, {
      method: 'DELETE',
    }),
  disableBotsUntilRestart: () =>
    fetchJson<{
      status: string;
      bots_disabled: boolean;
      bots_disabled_source: 'env' | 'until_restart';
    }>('/fanout/bots/disable-until-restart', {
      method: 'POST',
    }),

  // Statistics
  getNodeStats: (publicKey: string, window?: StatsWindow, signal?: AbortSignal) =>
    fetchJson<NodeStatsResponse>(
      `/contacts/${publicKey}/stats${window ? `?window=${encodeURIComponent(window)}` : ''}`,
      { signal }
    ),

  getStatistics: (window?: StatsWindow) =>
    fetchJson<StatisticsResponse>(
      window ? `/statistics?window=${encodeURIComponent(window)}` : '/statistics'
    ),

  // Granular repeater endpoints
  repeaterLogin: (publicKey: string, password: string) =>
    fetchJson<RepeaterLoginResponse>(`/contacts/${publicKey}/repeater/login`, {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  repeaterStatus: (publicKey: string) =>
    fetchJson<RepeaterStatusResponse>(`/contacts/${publicKey}/repeater/status`, {
      method: 'POST',
    }),
  repeaterNeighbors: (publicKey: string) =>
    fetchJson<RepeaterNeighborsResponse>(`/contacts/${publicKey}/repeater/neighbors`, {
      method: 'POST',
    }),
  repeaterNodeInfo: (publicKey: string) =>
    fetchJson<RepeaterNodeInfoResponse>(`/contacts/${publicKey}/repeater/node-info`, {
      method: 'POST',
    }),
  repeaterAcl: (publicKey: string) =>
    fetchJson<RepeaterAclResponse>(`/contacts/${publicKey}/repeater/acl`, {
      method: 'POST',
    }),
  repeaterRadioSettings: (publicKey: string) =>
    fetchJson<RepeaterRadioSettingsResponse>(`/contacts/${publicKey}/repeater/radio-settings`, {
      method: 'POST',
    }),
  repeaterAdvertIntervals: (publicKey: string) =>
    fetchJson<RepeaterAdvertIntervalsResponse>(`/contacts/${publicKey}/repeater/advert-intervals`, {
      method: 'POST',
    }),
  repeaterOwnerInfo: (publicKey: string) =>
    fetchJson<RepeaterOwnerInfoResponse>(`/contacts/${publicKey}/repeater/owner-info`, {
      method: 'POST',
    }),
  repeaterRegions: (publicKey: string) =>
    fetchJson<RepeaterRegionsResponse>(`/contacts/${publicKey}/repeater/regions`, {
      method: 'POST',
    }),
  repeaterLppTelemetry: (publicKey: string) =>
    fetchJson<RepeaterLppTelemetryResponse>(`/contacts/${publicKey}/repeater/lpp-telemetry`, {
      method: 'POST',
    }),
  repeaterTelemetryHistory: (publicKey: string) =>
    fetchJson<TelemetryHistoryEntry[]>(`/contacts/${publicKey}/repeater/telemetry-history`),
  // Contact telemetry (universal, any contact type)
  requestContactTelemetry: (publicKey: string) =>
    fetchJson<ContactTelemetryResponse>(`/contacts/${publicKey}/telemetry`, {
      method: 'POST',
    }),
  contactTelemetryHistory: (publicKey: string) =>
    fetchJson<TelemetryHistoryEntry[]>(`/contacts/${publicKey}/telemetry-history`),
  roomLogin: (
    publicKey: string,
    opts: { password?: string; useStoredCredential?: boolean; resyncHistory?: boolean } = {}
  ) =>
    fetchJson<RepeaterLoginResponse>(`/contacts/${publicKey}/room/login`, {
      method: 'POST',
      // password may legitimately be "" (guest); only omit it when unset so the
      // backend falls back to the stored credential.
      body: JSON.stringify({
        ...(opts.password !== undefined ? { password: opts.password } : {}),
        use_stored_credential: opts.useStoredCredential ?? false,
        resync_history: opts.resyncHistory ?? false,
      }),
    }),
  getRoomPoll: (publicKey: string) => fetchJson<RoomPollStatus>(`/contacts/${publicKey}/room/poll`),
  setRoomPoll: (publicKey: string, config: RoomPollConfigRequest) =>
    fetchJson<RoomPollStatus>(`/contacts/${publicKey}/room/poll`, {
      method: 'PUT',
      body: JSON.stringify(config),
    }),
  deleteRoomPoll: (publicKey: string) =>
    fetchJson<RoomPollStatus>(`/contacts/${publicKey}/room/poll`, {
      method: 'DELETE',
    }),
  roomStatus: (publicKey: string) =>
    fetchJson<RepeaterStatusResponse>(`/contacts/${publicKey}/room/status`, {
      method: 'POST',
    }),
  roomAcl: (publicKey: string) =>
    fetchJson<RepeaterAclResponse>(`/contacts/${publicKey}/room/acl`, {
      method: 'POST',
    }),
  roomLppTelemetry: (publicKey: string) =>
    fetchJson<RepeaterLppTelemetryResponse>(`/contacts/${publicKey}/room/lpp-telemetry`, {
      method: 'POST',
    }),

  // Push Notifications
  getVapidPublicKey: () => fetchJson<{ public_key: string }>('/push/vapid-public-key'),
  pushSubscribe: (subscription: {
    endpoint: string;
    p256dh: string;
    auth: string;
    label?: string;
  }) =>
    fetchJson<PushSubscriptionInfo>('/push/subscribe', {
      method: 'POST',
      body: JSON.stringify(subscription),
    }),
  getPushSubscriptions: () => fetchJson<PushSubscriptionInfo[]>('/push/subscriptions'),
  deletePushSubscription: (id: string) =>
    fetchJson<{ deleted: boolean }>(`/push/subscriptions/${id}`, { method: 'DELETE' }),
  testPushSubscription: (id: string) =>
    fetchJson<{ status: string }>(`/push/subscriptions/${id}/test`, { method: 'POST' }),
  getPushConversations: () => fetchJson<string[]>('/push/conversations'),
  togglePushConversation: (key: string) =>
    fetchJson<string[]>('/push/conversations/toggle', {
      method: 'POST',
      body: JSON.stringify({ key }),
    }),

  // Bots workspace
  getBots: () => fetchJson<Bot[]>('/bots'),
  getBot: (id: string) => fetchJson<Bot>(`/bots/${id}`),
  createBot: (body: {
    name: string;
    category?: string;
    description?: string;
    code?: string;
    enabled?: boolean;
    from_builtin_key?: string | null;
  }) => fetchJson<Bot>('/bots', { method: 'POST', body: JSON.stringify(body) }),
  updateBot: (id: string, body: BotUpdatePayload) =>
    fetchJson<Bot>(`/bots/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteBot: (id: string) => fetchJson<{ status: string }>(`/bots/${id}`, { method: 'DELETE' }),
  resetBot: (id: string) => fetchJson<Bot>(`/bots/${id}/reset`, { method: 'POST' }),
  testBot: (
    id: string,
    body: {
      text: string;
      is_dm?: boolean;
      is_room?: boolean;
      sender_name?: string;
      sender_key?: string | null;
      channel_key?: string | null;
      channel_name?: string | null;
      room_key?: string | null;
      room_name?: string | null;
    }
  ) =>
    fetchJson<BotTestResponse>(`/bots/${id}/test`, { method: 'POST', body: JSON.stringify(body) }),
  getBotLibrary: () => fetchJson<BotLibraryEntry[]>('/bots/library'),
  getBotRuns: (botId?: string, limit = 50) =>
    fetchJson<BotRun[]>(
      `/bots/runs?limit=${limit}${botId ? `&bot_id=${encodeURIComponent(botId)}` : ''}`
    ),
  getBotStats: (window: '1h' | '24h' | '7d') => fetchJson<BotStats>(`/bots/stats?window=${window}`),
  getBotLogs: (limit = 200) => fetchJson<BotLogEntry[]>(`/bots/logs?limit=${limit}`),
  getBotEngine: () => fetchJson<BotEngineStatus>('/bots/engine'),
  updateBotEngine: (body: Partial<BotEngineSettings>) =>
    fetchJson<BotEngineStatus>('/bots/engine', { method: 'PATCH', body: JSON.stringify(body) }),
  // Bots kill switch: reuses disableBotsUntilRestart() above — the server now
  // silences BOTH the legacy fanout bot modules and the Bots workspace engine
  // from either endpoint.
  getBotSchedules: () => fetchJson<BotSchedule[]>('/bots/schedules/all'),
  createBotSchedule: (body: {
    label: string;
    cron: string;
    channel_key: string;
    message: string;
    flood_scope?: string | null;
    enabled?: boolean;
  }) => fetchJson<BotSchedule>('/bots/schedules', { method: 'POST', body: JSON.stringify(body) }),
  updateBotSchedule: (
    id: string,
    body: Partial<{
      label: string;
      cron: string;
      channel_key: string;
      message: string;
      flood_scope: string | null;
      enabled: boolean;
    }>
  ) =>
    fetchJson<BotSchedule>(`/bots/schedules/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  deleteBotSchedule: (id: string) =>
    fetchJson<{ status: string }>(`/bots/schedules/${id}`, { method: 'DELETE' }),
  validateCron: (cron: string) =>
    fetchJson<{ valid: boolean; error: string | null; next_runs: number[] }>(
      `/bots/schedules/validate-cron?cron=${encodeURIComponent(cron)}`
    ),
  getBotFeeds: () => fetchJson<BotFeed[]>('/bots/feeds/all'),
  createBotFeed: (body: {
    name: string;
    feed_type: 'rss' | 'api';
    url: string;
    channel_key: string;
    interval_seconds?: number;
    format?: string;
    items_path?: string | null;
    max_posts_per_check?: number;
    enabled?: boolean;
  }) => fetchJson<BotFeed>('/bots/feeds', { method: 'POST', body: JSON.stringify(body) }),
  updateBotFeed: (
    id: string,
    body: Partial<{
      name: string;
      feed_type: 'rss' | 'api';
      url: string;
      channel_key: string;
      interval_seconds: number;
      format: string;
      items_path: string | null;
      max_posts_per_check: number;
      enabled: boolean;
    }>
  ) => fetchJson<BotFeed>(`/bots/feeds/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteBotFeed: (id: string) =>
    fetchJson<{ status: string }>(`/bots/feeds/${id}`, { method: 'DELETE' }),
  testBotFeed: (body: {
    url: string;
    feed_type: 'rss' | 'api';
    items_path?: string | null;
    format?: string;
  }) =>
    fetchJson<{ item_count: number; preview: string[] }>('/bots/feeds/test', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
};
