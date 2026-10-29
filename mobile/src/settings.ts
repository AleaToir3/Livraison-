/**
 * Réglages saisis sur le téléphone : adresse du serveur, clé API, dépôt.
 * Stockés localement (AsyncStorage) : aucun secret dans le code ni dans Git, et le même APK sert pour
 * n'importe quel serveur.
 */
import type { Depot } from './types';

export interface Settings {
  serverUrl: string; // https://mon-serveur.onrender.com (sans slash final)
  apiKey: string;
  depot: Depot;
}

export const DEFAULT_SETTINGS: Settings = {
  serverUrl: '',
  apiKey: '',
  depot: { lat: 53.7996, lon: -1.5491, label: 'Dépôt' },
};

export interface KeyValueStorage {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
}

const KEY = 'settings:v1';

export type UrlCheck = { ok: true; url: string } | { ok: false; error: string };

/**
 * Nettoie l'adresse saisie : espaces, slash final, schéma manquant (https par défaut).
 * Le HTTP simple n'est accepté que pour un réseau local (192.168.x.x, 10.x, localhost) : iOS et Android
 * récents bloquent le HTTP en clair vers Internet.
 */
export function normalizeServerUrl(input: string): UrlCheck {
  let text = input.trim();
  if (!text) return { ok: false, error: 'Adresse vide' };
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = `https://${text}`;
  const m = /^(https?):\/\/([^/\s?#]+)((?:\/[^\s?#]*)?)$/i.exec(text);
  if (!m) return { ok: false, error: "Adresse invalide (exemple : https://mon-serveur.onrender.com)" };
  const scheme = m[1]!.toLowerCase();
  const host = m[2]!;
  const hostname = host.replace(/:\d+$/, '').toLowerCase();
  const local = hostname === 'localhost' || /^(10\.|192\.168\.|127\.)/.test(hostname) || /^172\.(1[6-9]|2\d|3[01])\./.test(hostname);
  if (scheme === 'http' && !local) {
    return { ok: false, error: 'Utilisez https:// (le HTTP simple est bloqué par les téléphones, sauf en réseau local)' };
  }
  if (!hostname.includes('.') && !local) return { ok: false, error: "Nom de serveur incomplet (il manque par exemple « .onrender.com »)" };
  return { ok: true, url: `${scheme}://${host}${m[3]!.replace(/\/+$/, '')}` };
}

export function parseCoordinate(text: string, min: number, max: number): number | null {
  const n = Number(text.trim().replace(',', '.'));
  return Number.isFinite(n) && n >= min && n <= max ? n : null;
}

export function isConfigured(s: Settings): boolean {
  return s.serverUrl !== '';
}

/** Adresse du WebSocket de scan déduite de l'adresse du serveur (`https` -> `wss`). */
export function webSocketUrl(s: Settings): string {
  if (!s.serverUrl) return '';
  const base = s.serverUrl.replace(/^http/i, 'ws');
  return s.apiKey ? `${base}/ws/scan?key=${encodeURIComponent(s.apiKey)}` : `${base}/ws/scan`;
}

export function authHeaders(s: Settings, extra: Record<string, string> = {}): Record<string, string> {
  return s.apiKey ? { 'X-API-Key': s.apiKey, ...extra } : extra;
}

export class SettingsStore {
  private current: Settings = DEFAULT_SETTINGS;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly storage: KeyValueStorage) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): Settings => this.current;

  async load(): Promise<Settings> {
    try {
      const raw = await this.storage.getItem(KEY);
      if (raw) {
        const saved = JSON.parse(raw) as Partial<Settings>;
        this.current = {
          serverUrl: typeof saved.serverUrl === 'string' ? saved.serverUrl : '',
          apiKey: typeof saved.apiKey === 'string' ? saved.apiKey : '',
          depot: { ...DEFAULT_SETTINGS.depot, ...(saved.depot ?? {}) },
        };
        this.emit();
      }
    } catch {
      /* réglages illisibles : valeurs par défaut */
    }
    return this.current;
  }

  async save(next: Settings): Promise<void> {
    this.current = next;
    this.emit();
    await this.storage.setItem(KEY, JSON.stringify(next));
  }

  private emit(): void {
    for (const l of this.listeners) l();
  }
}
