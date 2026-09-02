interface RadioSettings {
  freq: number;
  bw: number;
  sf: number;
  cr: number;
}

export interface RepeatFreqRange {
  min_mhz: number;
  max_mhz: number;
}

export interface RadioConfig {
  public_key: string;
  name: string;
  lat: number;
  lon: number;
  tx_power: number;
  max_tx_power: number;
  radio: RadioSettings;
  path_hash_mode: number;
  path_hash_mode_supported: boolean;
  advert_location_source?: 'off' | 'current';
  multi_acks_enabled?: boolean;
  telemetry_mode_base?: number;
  telemetry_mode_loc?: number;
  telemetry_mode_env?: number;
  repeat_enabled?: boolean;
  repeat_supported?: boolean;
  allowed_repeat_freqs?: RepeatFreqRange[];
}

export interface RadioConfigUpdate {
  name?: string;
  lat?: number;
  lon?: number;
  tx_power?: number;
  radio?: RadioSettings;
  path_hash_mode?: number;
  advert_location_source?: 'off' | 'current';
  multi_acks_enabled?: boolean;
  telemetry_mode_base?: number;
  telemetry_mode_loc?: number;
  telemetry_mode_env?: number;
  repeat_enabled?: boolean;
}

export type RadioDiscoveryTarget = 'repeaters' | 'sensors' | 'all';

export interface RadioDiscoveryResult {
  public_key: string;
  name: string | null;
  node_type: 'repeater' | 'sensor';
  heard_count: number;
  local_snr: number | null;
  local_rssi: number | null;
  remote_snr: number | null;
}

export interface RadioDiscoveryResponse {
  target: RadioDiscoveryTarget;
  duration_seconds: number;
  results: RadioDiscoveryResult[];
}

export interface RadioRegionDiscoveryRepeater {
  public_key: string;
  name: string | null;
  answered: boolean;
  regions: string[];
}

export interface RadioRegionDiscoveryResponse {
  repeaters_queried: number;
  repeaters_answered: number;
  /** Deduplicated union of flood-allowed region names across all repeaters. */
  regions: string[];
  results: RadioRegionDiscoveryRepeater[];
}

export type RadioAdvertMode = 'flood' | 'zero_hop';

export interface FanoutStatusEntry {
  name: string;
  type: string;
  status: string;
  last_error?: string | null;
}

export interface AppInfo {
  version: string;
  commit_hash: string | null;
}

export interface RadioStatsSnapshot {
  timestamp: number | null;
  battery_mv: number | null;
  uptime_secs: number | null;
  queue_len: number | null;
  errors: number | null;
  noise_floor: number | null;
  last_rssi: number | null;
  last_snr: number | null;
  tx_air_secs: number | null;
  rx_air_secs: number | null;
  packets_recv: number | null;
  packets_sent: number | null;
  flood_tx: number | null;
  direct_tx: number | null;
  flood_rx: number | null;
  direct_rx: number | null;
}

export interface HealthStatus {
  status: string;
  radio_connected: boolean;
  radio_initializing: boolean;
  radio_state?: 'connected' | 'initializing' | 'connecting' | 'disconnected' | 'paused';
  connection_info: string | null;
  app_info?: AppInfo | null;
  radio_device_info?: {
    model: string | null;
    firmware_build: string | null;
    firmware_version: string | null;
    max_contacts: number | null;
    max_channels: number | null;
  } | null;
  radio_stats?: RadioStatsSnapshot | null;
  database_size_mb: number;
  oldest_undecrypted_timestamp: number | null;
  fanout_statuses: Record<string, FanoutStatusEntry>;
  bots_disabled: boolean;
  bots_disabled_source?: 'env' | 'until_restart' | null;
  basic_auth_enabled?: boolean;
  virtual_node?: VirtualNodeStatus | null;
}

/** State of the virtual companion node other MeshCore apps can connect to over TCP. */
export interface VirtualNodeStatus {
  enabled: boolean;
  listening: boolean;
  host: string | null;
  port: number | null;
  read_only: boolean;
  replay_limit?: number;
  client_count: number;
  local_commands: number;
  cached_commands: number;
  forwarded_commands: number;
}

export interface FanoutConfig {
  id: string;
  type: string;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
  scope: Record<string, unknown>;
  sort_order: number;
  created_at: number;
}

export interface MaintenanceResult {
  packets_deleted: number;
  vacuumed: boolean;
}

export interface Contact {
  public_key: string;
  name: string | null;
  type: number;
  flags: number;
  direct_path: string | null;
  direct_path_len: number;
  direct_path_hash_mode: number;
  direct_path_updated_at?: number | null;
  route_override_path?: string | null;
  route_override_len?: number | null;
  route_override_hash_mode?: number | null;
  effective_route?: ContactRoute | null;
  effective_route_source?: 'override' | 'direct' | 'flood';
  direct_route?: ContactRoute | null;
  route_override?: ContactRoute | null;
  last_advert: number | null;
  lat: number | null;
  lon: number | null;
  last_seen: number | null;
  on_radio: boolean;
  favorite: boolean;
  mcmp_enabled?: boolean;
  mcmp_version?: number;
  /** Codec for outbound photos: 'ie4' (AVIF/JPEG fragments) or 'aeic' (neural). */
  image_codec?: 'ie4' | 'aeic';
  /**
   * Whether image/voice fragments may travel as `rmt1:` text when this node's
   * firmware has no CMD_SEND_RAW_DATA. On by default; costs ~2.5x the airtime.
   */
  raw_media_text_transport?: boolean;
  last_contacted: number | null;
  last_read_at: number | null;
  first_seen: number | null;
}

export interface ContactRoute {
  path: string;
  path_len: number;
  path_hash_mode: number;
}

export interface ContactAdvertPath {
  path: string;
  path_len: number;
  next_hop: string | null;
  first_seen: number;
  last_seen: number;
  heard_count: number;
}

export interface ContactAdvertPathSummary {
  public_key: string;
  paths: ContactAdvertPath[];
}

export interface ContactNameHistory {
  name: string;
  first_seen: number;
  last_seen: number;
}

export interface ContactActiveRoom {
  channel_key: string;
  channel_name: string;
  message_count: number;
}

export interface NearestRepeater {
  public_key: string;
  name: string | null;
  path_len: number;
  last_seen: number;
  heard_count: number;
}

/**
 * Clock drift is `advert_timestamp - our_receive_time`: positive means the node's
 * clock runs ahead of the server's. Propagation delay only ever pushes a reading
 * negative, so a healthy node sits at a small negative and each bucket keeps its
 * *largest* reading. Everything is relative to the server clock — see
 * `app/clock_drift.py`.
 */
export interface ClockDriftSample {
  bucket_start: number;
  drift_seconds: number;
  sample_count: number;
  /** Hops on the arrival this bucket kept; 0 = direct, so almost no delay to subtract. */
  path_len: number;
}

export type DriftSeverity = 'in_sync' | 'minor' | 'major' | 'severe';

/**
 * Scalar drift figures for one node over one window. Shared by the contact info
 * pane (`ContactClockDrift`) and the node stats page (`NodeClockDriftStats`),
 * which differ only in how much series detail they carry.
 */
export interface ClockDriftSummary {
  latest_drift_seconds: number;
  latest_observed_at: number;
  latest_advert_timestamp: number;
  latest_path_len: number;
  severity: DriftSeverity;
  /** Advert timestamp predates 2001: the clock was never set, not merely wrong. */
  clock_unset: boolean;
  window_seconds: number;
  first_observed_at: number;
  sample_count: number;
  bucket_count: number;
  direct_sample_count: number;
  min_drift_seconds: number;
  max_drift_seconds: number;
  mean_drift_seconds: number;
  /**
   * Trend in seconds/day, or null when too few samples span too little time.
   * Measured over the segment since the clock was last set when there is one —
   * see `rate_since_last_step`.
   */
  drift_rate_seconds_per_day: number | null;
  /** True when the trend covers only the readings after the most recent step. */
  rate_since_last_step: boolean;
  /** Times the clock was *set* rather than drifting. Non-zero = a resync isn't holding. */
  step_count: number;
  bucket_seconds: number;
}

export interface ContactClockDrift extends ClockDriftSummary {
  samples: ClockDriftSample[];
}

export interface ContactAnalyticsHourlyBucket {
  bucket_start: number;
  last_24h_count: number;
  last_week_average: number;
  all_time_average: number;
}

export interface ContactAnalyticsWeeklyBucket {
  bucket_start: number;
  message_count: number;
}

export interface ContactAnalytics {
  lookup_type: 'contact' | 'name';
  name: string;
  contact: Contact | null;
  name_first_seen_at: number | null;
  name_history: ContactNameHistory[];
  dm_message_count: number;
  channel_message_count: number;
  includes_direct_messages: boolean;
  most_active_rooms: ContactActiveRoom[];
  advert_paths: ContactAdvertPath[];
  advert_frequency: number | null;
  nearest_repeaters: NearestRepeater[];
  /** Null when this contact's clock has never been measured. */
  clock_drift: ContactClockDrift | null;
  hourly_activity: ContactAnalyticsHourlyBucket[];
  weekly_activity: ContactAnalyticsWeeklyBucket[];
}

export interface Channel {
  key: string;
  name: string;
  is_hashtag: boolean;
  on_radio: boolean;
  flood_scope_override?: string | null;
  path_hash_mode_override?: number | null;
  last_read_at: number | null;
  favorite: boolean;
  muted: boolean;
  mcmp_enabled?: boolean;
  mcmp_version?: number;
  /** Codec for outbound photos: 'ie4' (AVIF/JPEG fragments) or 'aeic' (neural). */
  image_codec?: 'ie4' | 'aeic';
}

export interface ChannelMessageCounts {
  last_1h: number;
  last_24h: number;
  last_48h: number;
  last_7d: number;
  all_time: number;
}

export interface ChannelTopSender {
  sender_name: string;
  sender_key: string | null;
  message_count: number;
}

export interface BulkCreateHashtagChannelsResult {
  created_channels: Channel[];
  existing_count: number;
  invalid_names: string[];
  decrypt_started: boolean;
  decrypt_total_packets: number;
  message: string;
}

export interface PathHashWidthStats {
  total_packets: number;
  single_byte: number;
  double_byte: number;
  triple_byte: number;
  single_byte_pct: number;
  double_byte_pct: number;
  triple_byte_pct: number;
}

export interface ChannelDetail {
  channel: Channel;
  message_counts: ChannelMessageCounts;
  first_message_at: number | null;
  unique_sender_count: number;
  top_senders_24h: ChannelTopSender[];
  path_hash_width_24h: PathHashWidthStats;
}

/** A single path that a message took to reach us */
export interface MessagePath {
  /** Hex-encoded routing path */
  path: string;
  /** Unix timestamp when this path was received */
  received_at: number;
  /** Hop count (number of intermediate nodes). Null for legacy data (infer as len(path)/2). */
  path_len?: number | null;
  /** Last-hop RSSI in dBm (null if not available, e.g. older data) */
  rssi?: number | null;
  /** Last-hop SNR in dB (null if not available, e.g. older data) */
  snr?: number | null;
}

export interface Message {
  id: number;
  type: 'PRIV' | 'CHAN';
  /** For PRIV: sender's PublicKey (or prefix). For CHAN: ChannelKey */
  conversation_key: string;
  text: string;
  sender_timestamp: number | null;
  received_at: number;
  /** List of routing paths this message arrived via. Null for outgoing messages. */
  paths: MessagePath[] | null;
  txt_type: number;
  signature: string | null;
  sender_key: string | null;
  outgoing: boolean;
  /** ACK count: 0 = not acked, 1+ = number of acks/flood echoes received */
  acked: number;
  sender_name: string | null;
  channel_name?: string | null;
  packet_id?: number | null;
  /** Region scope transport code (uint16) when this arrived via a transport-routed packet. */
  transport_code?: number | null;
  /** Resolved region name for the transport code, if it matched a known region. */
  region?: string | null;
  /** Codec the body rode under, or null when it went as plain text. */
  compression?: MessageCompression | null;
  /** UTF-8 size of the plaintext body. Null for messages stored before tracking existed. */
  plain_bytes?: number | null;
  /** UTF-8 size of the payload actually transmitted, container and all. */
  wire_bytes?: number | null;
  /**
   * Compressed-text segment the ratio is measured against. Equals wire_bytes for v2;
   * for v3 it excludes the container header, matching MCO Advanced's percentage.
   */
  payload_bytes?: number | null;
  /** Transmissions made for an outgoing message (1 = sent once, never retried). */
  send_attempts?: number | null;
  /** The attempt cap that this message's send run honoured. */
  send_max_attempts?: number | null;
  /** Outgoing send progress. Delivery is not here -- it stays derived from `acked`. */
  send_state?: MessageSendState | null;
  /**
   * Emoji reactions attached to this message (emoji -> count), MeshCore Open
   * Advanced compatible. Null/absent when nobody reacted.
   */
  reactions?: Record<string, number> | null;
  /**
   * The row is itself a reaction payload. Always false on messages the
   * frontend sees -- the backend hides reaction rows from every surface.
   */
  is_reaction?: boolean;
}

/** Compression codecs a message body can arrive or leave under. */
export type MessageCompression = 'mcmp2' | 'mcmp3';

/**
 * Where an outgoing message's send got to. Delivery is deliberately absent:
 * `acked > 0` is the single source of truth for that, so a late ACK on a
 * `failed` message still shows as delivered.
 */
export type MessageSendState = 'sending' | 'sent' | 'failed' | 'canceled';

export interface MessagesAroundResponse {
  messages: Message[];
  has_older: boolean;
  has_newer: boolean;
}

/** Outcome of a per-message action: retry, cancel or delete. */
export interface MessageActionResponse {
  status: string;
  message_id: number;
  /**
   * The resulting row when the action produced one. A channel retry with a fresh
   * timestamp creates a new message, so this id can differ from the one asked for.
   */
  message?: Message | null;
  /**
   * Whether background retransmissions were still scheduled and have now stopped.
   * False means the send had already finished; the message is marked cancelled either way.
   */
  stopped_pending_sends: boolean;
}

export interface ResendChannelMessageResponse {
  status: string;
  message_id: number;
  message?: Message;
}

type ConversationType =
  | 'contact'
  | 'channel'
  | 'raw'
  | 'map'
  | 'visualizer'
  | 'search'
  | 'trace'
  | 'bots'
  | 'statistics'
  /** Per-node stats page; `id` is the node's public key. */
  | 'nodeStats';

export interface Conversation {
  type: ConversationType;
  /** PublicKey for contacts, ChannelKey for channels, 'raw'/'map' for special views */
  id: string;
  name: string;
  /** For map view: public key prefix to focus on */
  mapFocusKey?: string;
  /** For bots view: bot id to open in the editor */
  botId?: string;
}

export interface RawPacket {
  id: number;
  /** Per-observation WS identity (unique per RF arrival, may be absent in older payloads) */
  observation_id?: number;
  timestamp: number;
  data: string; // hex
  payload_type: string;
  snr: number | null; // Signal-to-noise ratio in dB
  rssi: number | null; // Received signal strength in dBm
  decrypted: boolean;
  decrypted_info: {
    channel_name: string | null;
    sender: string | null;
    channel_key: string | null;
    contact_key: string | null;
    sender_timestamp: number | null;
    message: string | null;
  } | null;
  /** Region scope transport code (uint16) for TransportFlood/TransportDirect packets. */
  transport_code?: number | null;
  /** Resolved region name for the transport code, if it matched a known region. */
  region?: string | null;
}

export interface AppSettings {
  max_radio_contacts: number;
  auto_decrypt_dm_on_advert: boolean;
  last_message_times: Record<string, number>;
  advert_interval: number;
  last_advert_time: number;
  flood_scope: string;
  known_regions: string[];
  blocked_keys: string[];
  blocked_names: string[];
  discovery_blocked_types: number[];
  tracked_telemetry_repeaters: string[];
  tracked_telemetry_contacts: string[];
  clock_sync_repeaters: string[];
  auto_resend_channel: boolean;
  max_message_retries: number;
  telemetry_interval_hours: number;
  telemetry_routed_hourly: boolean;
}

/** Bounds and default for `max_message_retries`, mirroring app/send_attempts.py. */
export const MIN_MESSAGE_RETRIES = 1;
export const DEFAULT_MESSAGE_RETRIES = 3;
export const MAX_MESSAGE_RETRIES = 10;

export interface AppSettingsUpdate {
  max_radio_contacts?: number;
  auto_decrypt_dm_on_advert?: boolean;
  advert_interval?: number;
  auto_resend_channel?: boolean;
  max_message_retries?: number;
  flood_scope?: string;
  known_regions?: string[];
  blocked_keys?: string[];
  blocked_names?: string[];
  discovery_blocked_types?: number[];
  telemetry_interval_hours?: number;
  telemetry_routed_hourly?: boolean;
}

export interface TelemetrySchedule {
  preferred_hours: number;
  effective_hours: number;
  options: number[];
  tracked_count: number;
  max_tracked: number;
  next_run_at: number | null;
  routed_hourly: boolean;
  next_routed_run_at: number | null;
}

export interface TrackedTelemetryResponse {
  tracked_telemetry_repeaters: string[];
  names: Record<string, string>;
  schedule: TelemetrySchedule;
  clock_sync_repeaters: string[];
}

export interface ClockSyncRepeaterResponse {
  clock_sync_repeaters: string[];
}

/** Contact type constants */
export const CONTACT_TYPE_REPEATER = 2;
export const CONTACT_TYPE_ROOM = 3;

export interface NeighborInfo {
  pubkey_prefix: string;
  name: string | null;
  snr: number;
  last_heard_seconds: number;
}

export interface AclEntry {
  pubkey_prefix: string;
  name: string | null;
  permission: number;
  permission_name: string;
}

export interface CommandResponse {
  command: string;
  response: string;
  sender_timestamp: number | null;
}

// --- Granular repeater endpoint types ---

export interface RepeaterLoginResponse {
  status: string;
  authenticated: boolean;
  message: string | null;
}

/** Room poll subscription status. Never carries the stored credential value. */
export interface RoomPollStatus {
  room_key: string;
  has_stored_credential: boolean;
  is_guest_credential: boolean;
  poll_enabled: boolean;
  interval_seconds: number;
  last_poll_at: number | null;
  last_result: string | null;
  last_error: string | null;
  consecutive_errors: number;
}

export interface RoomPollConfigRequest {
  enabled?: boolean;
  interval_seconds?: number;
  credential_action?: 'keep' | 'set' | 'clear';
  credential?: string | null;
}

export interface RepeaterStatusResponse {
  battery_volts: number;
  tx_queue_len: number;
  noise_floor_dbm: number;
  last_rssi_dbm: number;
  last_snr_db: number;
  packets_received: number;
  packets_sent: number;
  airtime_seconds: number;
  rx_airtime_seconds: number;
  uptime_seconds: number;
  sent_flood: number;
  sent_direct: number;
  recv_flood: number;
  recv_direct: number;
  flood_dups: number;
  direct_dups: number;
  full_events: number;
  recv_errors: number | null;
  telemetry_history: TelemetryHistoryEntry[];
}

export interface RepeaterNeighborsResponse {
  neighbors: NeighborInfo[];
  // Total neighbor count reported by the repeater firmware, independent of how many
  // entries were actually returned. Exceeds neighbors.length when a multi-chunk fetch
  // is incomplete. Null on older firmware / failed fetches.
  reported_count?: number | null;
}

export interface RepeaterAclResponse {
  acl: AclEntry[];
}

export interface RepeaterNodeInfoResponse {
  name: string | null;
  lat: string | null;
  lon: string | null;
  clock_utc: string | null;
}

export interface RepeaterRadioSettingsResponse {
  firmware_version: string | null;
  radio: string | null;
  tx_power: string | null;
  airtime_factor: string | null;
  // Configured duty-cycle limit (e.g. "25.0%"), firmware-derived from airtime_factor.
  // Only present on firmware >= 1.15; null on older nodes.
  duty_cycle_limit: string | null;
  repeat_enabled: string | null;
  flood_max: string | null;
}

export interface RepeaterAdvertIntervalsResponse {
  advert_interval: string | null;
  flood_advert_interval: string | null;
}

export interface RepeaterOwnerInfoResponse {
  owner_info: string | null;
  firmware_version: string | null;
  name: string | null;
  guest_password: string | null;
}

export interface RepeaterRegionEntry {
  name: string;
  depth: number;
  flood_allowed: boolean;
  is_home: boolean;
}

export interface RepeaterRegionsResponse {
  regions: RepeaterRegionEntry[];
  raw: string | null;
  truncated: boolean;
  /** 'cli' = full admin hierarchy; 'anon' = guest flood-allowed names only. */
  source: 'cli' | 'anon' | null;
}

export interface LppSensor {
  channel: number;
  type_name: string;
  value: number | Record<string, number>;
}

export interface RepeaterLppTelemetryResponse {
  sensors: LppSensor[];
}

export interface ContactTelemetryResponse {
  sensors: LppSensor[];
  fetched_at: number;
  telemetry_history: TelemetryHistoryEntry[];
}

export interface TrackedTelemetryContactsResponse {
  tracked_telemetry_contacts: string[];
  names: Record<string, string>;
  schedule: TelemetrySchedule;
}

export type PaneName =
  | 'status'
  | 'nodeInfo'
  | 'neighbors'
  | 'acl'
  | 'radioSettings'
  | 'advertIntervals'
  | 'ownerInfo'
  | 'lppTelemetry'
  | 'regions';

export interface PaneState {
  loading: boolean;
  attempt: number;
  error: string | null;
  fetched_at?: number | null;
}

export interface TelemetryLppSensor {
  channel: number;
  type_name: string;
  value: number;
}

export interface TelemetryHistoryEntry {
  timestamp: number;
  data: Record<string, number> & { lpp_sensors?: TelemetryLppSensor[] };
}

export interface PushSubscriptionInfo {
  id: string;
  endpoint: string;
  p256dh: string;
  auth: string;
  label: string;
  created_at: number;
  last_success_at: number | null;
  failure_count: number;
}

export interface TraceResponse {
  remote_snr: number | null;
  local_snr: number | null;
  path_len: number;
}

export interface RadioTraceNode {
  role: 'repeater' | 'custom' | 'local';
  public_key: string | null;
  name: string | null;
  observed_hash: string | null;
  snr: number | null;
}

export interface RadioTraceHopRequest {
  public_key?: string | null;
  hop_hex?: string | null;
}

export interface RadioTraceResponse {
  path_len: number;
  timeout_seconds: number;
  nodes: RadioTraceNode[];
}

export interface PathDiscoveryRoute {
  path: string;
  path_len: number;
  path_hash_mode: number;
}

export interface PathDiscoveryResponse {
  contact: Contact;
  forward_path: PathDiscoveryRoute;
  return_path: PathDiscoveryRoute;
}

export interface UnreadCounts {
  counts: Record<string, number>;
  mentions: Record<string, boolean>;
  last_message_times: Record<string, number>;
  last_read_ats: Record<string, number | null>;
  /** stateKey -> id of the oldest unread message. Locates the unread divider. */
  first_unread_ids: Record<string, number | null>;
}

interface BusyChannel {
  channel_key: string;
  channel_name: string;
  message_count: number;
}

interface ContactActivityCounts {
  last_hour: number;
  last_24_hours: number;
  last_week: number;
  /** Count over the selected statistics window. */
  window: number;
}

export interface NoiseFloorSample {
  /** Start of the bucket, not of an individual reading. */
  timestamp: number;
  /** Mean dBm across the bucket. */
  noise_floor_dbm: number;
  min_dbm?: number | null;
  max_dbm?: number | null;
}

export interface NoiseFloorHistoryStats {
  sample_interval_seconds: number;
  /** Bucket width; equals sample_interval_seconds while the window is short. */
  bucket_seconds: number;
  coverage_seconds: number;
  latest_noise_floor_dbm: number | null;
  latest_timestamp: number | null;
  samples: NoiseFloorSample[];
}

interface PacketBucket {
  timestamp: number;
  count: number;
}

export interface PacketsOverTime {
  bucket_seconds: number;
  buckets: PacketBucket[];
}

/**
 * Windows the statistics endpoint accepts, narrowest first. Keys are sent as
 * ``?window=``; labels are what the selector shows.
 */
export const STATS_WINDOWS = [
  { key: '1h', label: '1h', title: 'Last hour', phrase: 'the last hour' },
  { key: '1d', label: '24h', title: 'Last 24 hours', phrase: 'the last 24 hours' },
  { key: '1w', label: '7d', title: 'Last 7 days', phrase: 'the last 7 days' },
  { key: '1M', label: '30d', title: 'Last 30 days', phrase: 'the last 30 days' },
  { key: '3M', label: '90d', title: 'Last 90 days', phrase: 'the last 90 days' },
  { key: '1y', label: '1y', title: 'Last year', phrase: 'the last year' },
  { key: 'all', label: 'All', title: 'Everything retained', phrase: 'the stored history' },
] as const;

export type StatsWindow = (typeof STATS_WINDOWS)[number]['key'];

export const DEFAULT_STATS_WINDOW: StatsWindow = '1d';

/**
 * Regional flood-scope adoption over the selected window. Two views with different
 * denominators that will not agree — traffic spans all channels including
 * undecryptable ones (so it carries a false-positive floor from corrupt RF
 * captures), while senders requires decryption and is therefore noise-free but
 * limited to channels we hold keys for.
 */
export interface RegionScopeStats {
  total_messages: number;
  scoped_messages: number;
  scoped_pct: number;
  /** Estimated false positives in scoped_messages. At or below this = not adoption. */
  false_positive_floor: number;
  total_senders: number;
  scoped_senders: number;
  scoped_senders_pct: number;
  /** True when the packet scan hit its row cap — traffic counts are a sample. */
  truncated?: boolean;
}

export interface RepeaterClockDriftEntry {
  public_key: string;
  name: string | null;
  drift_seconds: number;
  observed_at: number;
  sample_count: number;
  bucket_count: number;
  drift_rate_seconds_per_day: number | null;
  severity: DriftSeverity;
  clock_unset: boolean;
}

export interface ClockDriftBucket {
  timestamp: number;
  mean_abs_drift_seconds: number;
  max_abs_drift_seconds: number;
  repeater_count: number;
}

export interface ClockDriftHistogramBin {
  label: string;
  count: number;
}

/**
 * Repeater clock drift over the selected window. `median_drift_seconds` is the
 * signed median across repeaters: one node far off is that node's problem, all of
 * them off the same way is this server's clock.
 */
export interface RepeaterClockDriftStats {
  repeaters_total: number;
  repeaters_with_samples: number;
  repeaters_unset_clock: number;
  sample_count: number;
  oldest_sample_at: number | null;
  newest_sample_at: number | null;
  in_sync: number;
  minor: number;
  major: number;
  severe: number;
  mean_abs_drift_seconds: number;
  median_abs_drift_seconds: number;
  median_drift_seconds: number;
  furthest_behind: RepeaterClockDriftEntry | null;
  furthest_ahead: RepeaterClockDriftEntry | null;
  worst_offenders: RepeaterClockDriftEntry[];
  fastest_rates: RepeaterClockDriftEntry[];
  /** Clocks that were never set. Excluded from the mean and the rankings above. */
  unset_clocks: RepeaterClockDriftEntry[];
  histogram: ClockDriftHistogramBin[];
  over_time: ClockDriftBucket[];
  bucket_seconds: number;
}

export interface StatisticsResponse {
  /** Window the snapshot was built for. */
  window: StatsWindow;
  /** Seconds the window covers; null for the unbounded 'all'. */
  window_seconds: number | null;
  busiest_channels: BusyChannel[];
  contact_count: number;
  repeater_count: number;
  channel_count: number;
  total_packets: number;
  decrypted_packets: number;
  undecrypted_packets: number;
  total_dms: number;
  total_channel_messages: number;
  total_outgoing: number;
  contacts_heard: ContactActivityCounts;
  repeaters_heard: ContactActivityCounts;
  known_channels_active: ContactActivityCounts;
  path_hash_width: {
    total_packets: number;
    single_byte: number;
    double_byte: number;
    triple_byte: number;
    single_byte_pct: number;
    double_byte_pct: number;
    triple_byte_pct: number;
    /** True when only the most recent packets in the window were parsed. */
    truncated?: boolean;
  };
  region_scope: RegionScopeStats;
  multibyte_rollout: MultibyteRolloutStats;
  packets_over_time: PacketsOverTime;
  noise_floor: NoiseFloorHistoryStats;
  repeater_clock_drift: RepeaterClockDriftStats;
}

/** Contact-level multibyte path adoption (nodes, not traffic). */
export interface MultibyteRolloutStats {
  contacts_with_route: number;
  contacts_multibyte: number;
  single_byte: number;
  double_byte: number;
  triple_byte: number;
  repeaters_with_route: number;
  repeaters_multibyte: number;
}

// ---------------------------------------------------------------------------
// Node stats page
//
// One page, one node, one window selector. The response is a bag of independent
// optional sections rather than a fixed shape: adding a stat later means adding
// a field here and a component on the page, and never touching the sections
// already in it. A section with nothing to say is null and is omitted.
// ---------------------------------------------------------------------------

/** One bucket of the detailed drift series, with the spread inside it. */
export interface ClockDriftBand {
  bucket_start: number;
  /** Best (largest, least-delayed) reading in the bucket. */
  drift_seconds: number;
  min_drift_seconds: number;
  max_drift_seconds: number;
  sample_count: number;
  reading_count: number;
  direct_reading_count: number;
}

/** A discontinuity between consecutive readings — a clock being *set*. */
export interface ClockDriftStep {
  at: number;
  from_drift_seconds: number;
  to_drift_seconds: number;
  /** Signed; positive means the clock moved forward. */
  delta_seconds: number;
  /** Time between the two readings — a jump across a long gap is weaker evidence. */
  gap_seconds: number;
}

/**
 * Readings grouped by the hop count they arrived over. Propagation delay only
 * biases a reading negative, so a mean falling away as hops rise is that bias
 * made visible.
 */
export interface ClockDriftHopBucket {
  path_len: number;
  reading_count: number;
  mean_drift_seconds: number;
  min_drift_seconds: number;
  max_drift_seconds: number;
}

export interface NodeClockDriftStats extends ClockDriftSummary {
  series: ClockDriftBand[];
  /** Largest first. */
  steps: ClockDriftStep[];
  histogram: ClockDriftHistogramBin[];
  hop_breakdown: ClockDriftHopBucket[];
}

export interface NodeStatsResponse {
  public_key: string;
  name: string | null;
  type: number;
  window: StatsWindow;
  /** Seconds the window covers; null for the unbounded 'all'. */
  window_seconds: number | null;
  generated_at: number;
  /** Null when this node's clock has never been measured. */
  clock_drift: NodeClockDriftStats | null;
}

/** The node stats page defaults wider than the mesh snapshot — a trend needs lever arm. */
export const NODE_STATS_DEFAULT_WINDOW: StatsWindow = '1M';

// ---------------------------------------------------------------------------
// Bots workspace
// ---------------------------------------------------------------------------

export interface BotUiTrigger {
  kind: 'keyword' | 'cron';
  spec: string;
}

interface BotSettingsSchemaFieldBase {
  key: string;
  label: string;
  default?: unknown;
  help?: string;
  show_when?: { key: string; value: string };
}

export interface BotSettingsValueField extends BotSettingsSchemaFieldBase {
  type: 'text' | 'password' | 'int' | 'float' | 'number' | 'bool' | 'select' | 'url';
  min?: number;
  max?: number;
  options?: { value: string; label: string }[];
}

export interface BotSettingsGeneratedUrlField extends BotSettingsSchemaFieldBase {
  type: 'generated_url';
  template: string;
  warning?: string;
  copy_label?: string;
  testable?: boolean;
  test_label?: string;
}

export type BotSettingsSchemaField = BotSettingsValueField | BotSettingsGeneratedUrlField;

/** `all` / `none` / an allow- or deny-list of conversation keys. */
export type BotScopeSelection = 'all' | 'none' | { only?: string[]; except?: string[] };

export interface Bot {
  id: string;
  name: string;
  category: string;
  description: string;
  /** The few extra lines under the one-liner, shown on the editor's Settings tab. */
  long_description: string;
  code: string;
  enabled: boolean;
  admin_only: boolean;
  respond_to_dms: boolean;
  /**
   * Where the bot listens. `rooms` is absent on scopes written before rooms
   * existed, which the backend reads as no room: rooms are opt-in, and a new
   * bot starts with an empty pick list.
   */
  scope: { channels: BotScopeSelection; rooms?: BotScopeSelection };
  cooldown_seconds: number;
  per_user_cooldown_seconds: number;
  queue_threshold_seconds: number;
  settings_schema: BotSettingsSchemaField[];
  settings: Record<string, unknown>;
  ui_triggers: BotUiTrigger[];
  builtin_key: string | null;
  builtin_version: string | null;
  modified: boolean;
  last_error: string | null;
  sort_order: number;
  created_at: number;
  updated_at: number;
  declared_keywords: string[];
  declared_crons: string[];
  declared_events: string[];
  declared_webhooks: string[];
  is_legacy: boolean;
  load_error: string | null;
  runs_24h: number;
}

export interface BotUpdatePayload {
  name?: string;
  category?: string;
  description?: string;
  long_description?: string;
  code?: string;
  enabled?: boolean;
  admin_only?: boolean;
  respond_to_dms?: boolean;
  scope?: Bot['scope'];
  cooldown_seconds?: number;
  per_user_cooldown_seconds?: number;
  queue_threshold_seconds?: number;
  settings?: Record<string, unknown>;
  ui_triggers?: BotUiTrigger[];
}

export interface BotLibraryEntry {
  key: string;
  name: string;
  category: string;
  description: string;
  long_description: string;
  version: string;
  installed: boolean;
}

export interface BotRun {
  id: number;
  bot_id: string;
  bot_name: string;
  started_at: number;
  duration_ms: number | null;
  trigger: string;
  sender_name: string | null;
  sender_key: string | null;
  channel_key: string | null;
  channel_name: string | null;
  is_dm: boolean;
  result: string;
  replies: number;
  error: string | null;
  test_run: boolean;
}

export interface BotTestResponse {
  matched: boolean;
  trigger: string | null;
  duration_ms: number;
  replies: {
    is_dm: boolean;
    destination: string | null;
    channel_key: string | null;
    text: string;
    region: string | null;
  }[];
  error: string | null;
  logs: string[];
}

export interface BotSchedule {
  id: string;
  label: string;
  cron: string;
  channel_key: string;
  flood_scope: string | null;
  message: string;
  enabled: boolean;
  last_run_at: number | null;
  last_result: string | null;
  created_at: number;
  next_run_at: number | null;
  channel_name: string | null;
}

export interface BotFeed {
  id: string;
  name: string;
  feed_type: 'rss' | 'api';
  url: string;
  channel_key: string;
  interval_seconds: number;
  format: string;
  items_path: string | null;
  enabled: boolean;
  last_item_id: string | null;
  last_check_at: number | null;
  last_error: string | null;
  error_count: number;
  items_posted: number;
  max_posts_per_check: number;
  created_at: number;
  channel_name: string | null;
}

export interface BotAdminUser {
  public_key: string;
  name: string;
}

export interface BotEngineSettings {
  command_prefix: string;
  require_prefix: boolean;
  mention_mode: 'also' | 'only' | 'off';
  global_reply_seconds: number;
  per_user_seconds: number;
  tx_spacing_seconds: number;
  max_response_hops: number;
  default_language: string;
  auto_detect_language: boolean;
  banned_users: string[];
  profanity_mode: 'off' | 'censor' | 'drop';
  admin_users: BotAdminUser[];
}

export interface BotEngineStatus {
  settings: BotEngineSettings;
  disabled_until_restart: boolean;
  disabled_by_env: boolean;
  total_bots: number;
  enabled_bots: number;
  erroring_bots: number;
  runs_24h: number;
}

export interface BotLogEntry {
  timestamp: number;
  level: string;
  source: string;
  message: string;
}

export interface BotStatsRanked {
  label: string;
  count: number;
}

export interface BotStats {
  runs: number;
  replies: number;
  reply_rate: number;
  errors: number;
  unique_users: number;
  avg_duration_ms: number;
  top_bots: BotStatsRanked[];
  top_channels: BotStatsRanked[];
  top_users: BotStatsRanked[];
  error_bots: BotStatsRanked[];
  runs_by_hour: { timestamp: number; count: number }[];
}
