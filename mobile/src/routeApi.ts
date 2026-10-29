import { buildOptimizeBody } from './routeBody';
import { authHeaders, type Settings } from './settings';
import { settingsStore } from './stores';
import type { Depot, ExtractImageResponse, Parcel, RouteResponse } from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
  }
}

async function request<T>(settings: Settings, path: string, init: RequestInit, timeoutMs: number): Promise<T> {
  if (!settings.serverUrl) throw new ApiError("Serveur non configuré : ouvrez les Réglages", null);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${settings.serverUrl}${path}`, { ...init, signal: controller.signal });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      if (response.status === 401) detail = 'clé API refusée : vérifiez les Réglages';
      else {
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === 'string') detail = body.detail;
          else if (body.detail && typeof body.detail === 'object') {
            const d = body.detail as { message?: string; kind?: string };
            detail = d.message ?? d.kind ?? detail;
          }
        } catch {
          /* corps non JSON */
        }
      }
      throw new ApiError(detail, response.status);
    }
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === 'AbortError') throw new ApiError('le serveur ne répond pas', null);
    throw new ApiError('réseau indisponible', null);
  } finally {
    clearTimeout(timer);
  }
}

/** Optimise la tournée : géocodage groupé (cache), matrice hybride, OR-Tools, ParkingClusters. */
export function optimizeRoute(parcels: readonly Parcel[], depot?: Depot): Promise<RouteResponse> {
  const settings = settingsStore.getSnapshot();
  return request<RouteResponse>(
    settings,
    '/api/v1/optimize-route',
    {
      method: 'POST',
      headers: authHeaders(settings, { 'Content-Type': 'application/json' }),
      body: JSON.stringify(buildOptimizeBody(parcels, depot ?? settings.depot)),
    },
    20000,
  );
}

/** Chemin lent : le ROI WebP part tel quel (octets bruts) vers Gemini via le backend. */
export function extractImage(webp: Uint8Array): Promise<ExtractImageResponse> {
  const settings = settingsStore.getSnapshot();
  return request<ExtractImageResponse>(
    settings,
    '/api/v1/extract-image',
    {
      method: 'POST',
      headers: authHeaders(settings, { 'Content-Type': 'image/webp' }),
      body: webp as unknown as RequestInit['body'],
    },
    12000,
  );
}

export interface ServerCheck {
  reachable: boolean;
  /** null tant que le serveur n'a pas répondu ; false si la clé est refusée. */
  keyAccepted: boolean | null;
  providers: string[];
  vision: boolean;
  error?: string;
  ms: number;
}

/**
 * Bouton « Tester la connexion » : joint le serveur (`/healthz`), puis vérifie la clé en appelant un point
 * protégé avec un corps invalide (401 = clé refusée ; toute autre réponse = clé acceptée).
 */
export async function checkServer(serverUrl: string, apiKey: string): Promise<ServerCheck> {
  const t0 = Date.now();
  const settings: Settings = { serverUrl, apiKey, depot: { lat: 0, lon: 0, label: '' } };
  let health: { providers?: string[]; vision?: boolean };
  try {
    health = await request<typeof health>(settings, '/healthz', { method: 'GET' }, 15000);
  } catch (error) {
    return {
      reachable: false,
      keyAccepted: null,
      providers: [],
      vision: false,
      error: error instanceof ApiError ? error.message : 'erreur inattendue',
      ms: Date.now() - t0,
    };
  }
  let keyAccepted = true;
  try {
    await request(settings, '/api/v1/extract-image', { method: 'POST', headers: authHeaders(settings), body: 'x' }, 10000);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) keyAccepted = false;
  }
  return {
    reachable: true,
    keyAccepted,
    providers: health.providers ?? [],
    vision: !!health.vision,
    ms: Date.now() - t0,
  };
}
