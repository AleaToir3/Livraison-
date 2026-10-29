import { config, httpHeaders } from './config';
import { buildOptimizeBody } from './routeBody';
import type { Depot, ExtractImageResponse, Parcel, RouteResponse } from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init: RequestInit, timeoutMs: number): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${config.httpBaseUrl.replace(/\/+$/, '')}${path}`, { ...init, signal: controller.signal });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
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
export function optimizeRoute(parcels: readonly Parcel[], depot: Depot = config.depot): Promise<RouteResponse> {
  return call<RouteResponse>(
    '/api/v1/optimize-route',
    {
      method: 'POST',
      headers: httpHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(buildOptimizeBody(parcels, depot)),
    },
    15000,
  );
}

/** Chemin lent : le ROI WebP part tel quel (octets bruts) vers Gemini via le backend. */
export function extractImage(webp: Uint8Array): Promise<ExtractImageResponse> {
  return call<ExtractImageResponse>(
    '/api/v1/extract-image',
    {
      method: 'POST',
      headers: httpHeaders({ 'Content-Type': 'image/webp' }),
      body: webp as unknown as RequestInit['body'],
    },
    10000,
  );
}
