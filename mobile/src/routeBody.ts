import type { Depot, Parcel } from './types';

/**
 * Corps de `POST /api/v1/optimize-route`. Les colis déjà géocodés partent avec leurs coordonnées ; les autres
 * avec leur adresse (le serveur retente le géocodage et signale ceux qu'il ne sait pas situer dans `unresolved`).
 */
export function buildOptimizeBody(parcels: readonly Parcel[], depot: Depot): Record<string, unknown> {
  return {
    depot: { lat: depot.lat, lon: depot.lon, label: depot.label },
    stops: parcels.map((p) => {
        const stop: Record<string, unknown> = { id: p.id, label: p.label };
        if (p.lat !== undefined && p.lon !== undefined) {
          stop.lat = p.lat;
          stop.lon = p.lon;
        } else {
          stop.postcode = p.postcode;
          stop.building = p.building || undefined;
          stop.sub_building = p.subBuilding;
          stop.street = p.street;
        }
        return stop;
    }),
  };
}

/** Empreinte de la liste de colis : permet de savoir si une tournée enregistrée est encore valable. */
export function parcelSignature(parcels: readonly Parcel[]): string {
  return parcels
    .map((p) => p.id)
    .sort()
    .join('|');
}
