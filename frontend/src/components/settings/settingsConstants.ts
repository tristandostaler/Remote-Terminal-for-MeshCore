import {
  Database,
  Info,
  MonitorCog,
  RadioTower,
  Share2,
  ShieldCheck,
  SlidersHorizontal,
  Smartphone,
  type LucideIcon,
} from 'lucide-react';

export type SettingsSection =
  | 'radio'
  | 'local'
  | 'https'
  | 'radio-app'
  | 'virtual-node'
  | 'database'
  | 'fanout'
  | 'about';

export const SETTINGS_SECTION_ORDER: SettingsSection[] = [
  'radio',
  'local',
  'https',
  'fanout',
  'radio-app',
  'virtual-node',
  'database',
  'about',
];

export const SETTINGS_SECTION_LABELS: Record<SettingsSection, string> = {
  radio: 'Radio',
  local: 'Local Configuration',
  https: 'HTTPS / TLS',
  'radio-app': 'Radio-App Management',
  'virtual-node': 'Virtual Node',
  database: 'Database',
  fanout: 'Integrations',
  about: 'About',
};

export const SETTINGS_SECTION_ICONS: Record<SettingsSection, LucideIcon> = {
  radio: RadioTower,
  local: MonitorCog,
  https: ShieldCheck,
  'radio-app': SlidersHorizontal,
  'virtual-node': Smartphone,
  database: Database,
  fanout: Share2,
  about: Info,
};
