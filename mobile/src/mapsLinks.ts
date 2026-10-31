/**
 * Lien Google Maps « plusieurs arrêts » : le livreur lance toute la suite d'arrêts d'un seul tap.
 *
 * Un lien Google Maps accepte au plus 9 étapes intermédiaires en plus de l'arrivée. Au-delà, la tournée est
 * découpée en parties : chacune démarre là où la précédente s'arrête (`origin`), la première part de la position
 * actuelle (pas d'`origin`). L'ordre des étapes est conservé tel quel : Google Maps ne le réorganise pas.
 */
export interface LatLon {
  lat: number;
  lon: number;
}

export const MAX_WAYPOINTS = 9;

const coord = (p: LatLon): string => `${p.lat.toFixed(6)},${p.lon.toFixed(6)}`;

/** Deux arrêts consécutifs au même endroit (« Conduite 0 s ») ne font qu'une étape dans Google Maps. */
export function dropRepeats(points: readonly LatLon[]): LatLon[] {
  const out: LatLon[] = [];
  for (const p of points) {
    const last = out[out.length - 1];
    if (!last || coord(last) !== coord(p)) out.push(p);
  }
  return out;
}

/** Un lien par partie de tournée (vide si aucun arrêt). */
export function mapsLegUrls(points: readonly LatLon[], maxWaypoints = MAX_WAYPOINTS): string[] {
  const stops = dropRepeats(points);
  const size = maxWaypoints + 1; // les étapes + l'arrivée
  const urls: string[] = [];
  for (let start = 0; start < stops.length; start += size) {
    const waypoints = stops.slice(start, start + size);
    const destination = waypoints.pop(); // la partie n'est jamais vide : `start < stops.length`
    if (!destination) break;
    const origin = start > 0 ? stops[start - 1] : undefined;
    const params = ['api=1', 'travelmode=driving', `destination=${coord(destination)}`];
    if (origin) params.push(`origin=${coord(origin)}`);
    if (waypoints.length > 0) params.push(`waypoints=${encodeURIComponent(waypoints.map(coord).join('|'))}`);
    urls.push(`https://www.google.com/maps/dir/?${params.join('&')}`);
  }
  return urls;
}
