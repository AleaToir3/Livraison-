import type { ParkingCluster, RouteResponse } from './types';

/** Avancement du livreur : colis remis (un tap) et stationnements terminés (« Reprenez le van »). */
export interface Progress {
  delivered: Record<string, true>;
  completed: Record<number, true>;
}

export const EMPTY_PROGRESS: Progress = { delivered: {}, completed: {} };

export function clusterDelivered(cluster: ParkingCluster, progress: Progress): number {
  return cluster.deliveries.reduce((n, d) => n + (progress.delivered[d.stop_id] ? 1 : 0), 0);
}

export function clusterAllDelivered(cluster: ParkingCluster, progress: Progress): boolean {
  return clusterDelivered(cluster, progress) === cluster.deliveries.length;
}

/**
 * Un tap bascule « remis / non remis ». Pour un stationnement à une seule porte, remettre le colis
 * termine l'arrêt (rien à « reprendre ») ; pour une boucle à pied, c'est « Reprenez le van » qui le termine.
 */
export function toggleDelivery(route: RouteResponse, progress: Progress, stopId: string): Progress {
  const cluster = route.plan.clusters.find((c) => c.deliveries.some((d) => d.stop_id === stopId));
  if (!cluster) return progress;
  const delivered = { ...progress.delivered };
  const completed = { ...progress.completed };
  if (delivered[stopId]) {
    delete delivered[stopId];
    delete completed[cluster.index];
  } else {
    delivered[stopId] = true;
    if (cluster.kind === 'SINGLE') completed[cluster.index] = true;
  }
  return { delivered, completed };
}

/** « Reprenez le van » : possible seulement quand toutes les portes de la boucle sont remises. */
export function completeCluster(route: RouteResponse, progress: Progress, index: number): Progress {
  const cluster = route.plan.clusters[index];
  if (!cluster || !clusterAllDelivered(cluster, progress)) return progress;
  return { ...progress, completed: { ...progress.completed, [index]: true } };
}

/** Premier stationnement non terminé (le prochain à faire), ou -1 quand la tournée est finie. */
export function activeClusterIndex(route: RouteResponse, progress: Progress): number {
  return route.plan.clusters.findIndex((c) => !progress.completed[c.index]);
}

export function counts(route: RouteResponse, progress: Progress): { done: number; total: number } {
  const total = route.plan.clusters.reduce((n, c) => n + c.deliveries.length, 0);
  const done = route.plan.clusters.reduce((n, c) => n + clusterDelivered(c, progress), 0);
  return { done, total };
}

/** « 1 min 20 s », « 45 s », « 1 h 05 ». */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 === 0 ? `${m} min` : `${m} min ${String(s % 60).padStart(2, '0')} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')}`;
}
