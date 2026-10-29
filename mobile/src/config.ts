import { Platform } from 'react-native';

import type { Depot } from './types';

export interface AppConfig {
  /** URL HTTP(S) du backend ; le WebSocket en est déduit (`https` -> `wss`). */
  httpBaseUrl: string;
  /** Clé envoyée dans `X-API-Key` (HTTP) et `?key=` (WebSocket). Vide = serveur ouvert (dev). */
  apiKey: string;
  /** Dépôt de départ/retour du van. À renseigner par agence (ou via `configure`). */
  depot: Depot;
  /** Cadence de l'OCR embarqué (images analysées par seconde). */
  ocrFps: number;
  /** Nombre de lectures identiques consécutives avant d'envoyer un scan (anti-erreur d'OCR). */
  confirmReads: number;
  /** Absence minimale de l'étiquette du champ avant de pouvoir renvoyer la même adresse (ms). */
  dedupeMs: number;
  /** Étiquette visible mais illisible pendant ce délai (ms) : bascule automatique vers le chemin lent. */
  unreadableMs: number;
  autoFallback: boolean;
}

// Émulateur Android : l'hôte est 10.0.2.2 ; simulateur iOS : localhost.
const devHost = Platform.select({ android: '10.0.2.2', default: 'localhost' });

export const config: AppConfig = {
  httpBaseUrl: `http://${devHost}:8000`,
  apiKey: '',
  depot: { lat: 53.7996, lon: -1.5491, label: 'Dépôt' },
  ocrFps: 8,
  confirmReads: 2,
  dedupeMs: 2500,
  unreadableMs: 2500,
  autoFallback: true,
};

export function configure(patch: Partial<AppConfig>): void {
  Object.assign(config, patch);
}

export function wsUrl(): string {
  const base = config.httpBaseUrl.replace(/\/+$/, '').replace(/^http/, 'ws');
  return config.apiKey ? `${base}/ws/scan?key=${encodeURIComponent(config.apiKey)}` : `${base}/ws/scan`;
}

export function httpHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return config.apiKey ? { 'X-API-Key': config.apiKey, ...extra } : extra;
}
