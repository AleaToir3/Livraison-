/** Types partagés : miroir du protocole WebSocket et des réponses JSON du backend (`backend/main.py`). */

// --- WebSocket /ws/scan ------------------------------------------------------------------------
/** Message client -> serveur (clés courtes : ~30 octets). */
export interface ScanRequest {
  i: number; // identifiant de scan, croissant
  p: string; // code postal sans espace (LS61AB)
  b: string; // numéro (14A) ou nom de maison (ROSE COTTAGE)
  f?: string; // sous-bâtiment (FLAT 2)
  s?: string; // rue
}

export interface ScanOk {
  i: number | null;
  ok: 1;
  la: number;
  lo: number;
  src: string; // cache | ideal_postcodes | os_places | google_rooftop | synthetic | static
  pr: string; // uprn | rooftop | synthetic
  k: string; // clé de cache normalisée
  ms: number; // temps serveur
}

export interface ScanErr {
  i: number | null;
  ok: 0;
  e: string; // bad_json | bad_address | geocode_failed | disabled | too_large | unreadable | ...
}

export type ScanReply = ScanOk | ScanErr;

// --- POST /api/v1/extract-image --------------------------------------------------------------
export interface ExtractImageResponse {
  ok: 1;
  address: {
    postcode: string | null;
    building_number: string | null;
    sub_building: string | null;
    building_name: string | null;
    street: string | null;
    locality: string | null;
    recipient: string | null;
  };
  key: string | null;
  confidence: number;
  geo: { la: number; lo: number; src: string; pr: string } | null;
  ms: { vision: number; geocode: number; total: number };
}

// --- Colis scannés ---------------------------------------------------------------------------
export type ParcelStatus = 'pending' | 'ready' | 'failed';

export interface Parcel {
  id: string;
  postcode: string; // "LS6 1AB"
  building: string; // "14A" ou "ROSE COTTAGE"
  subBuilding?: string;
  street?: string;
  label: string; // "FLAT 2, 27 ALBION AVENUE, LS6 1AD"
  lat?: number;
  lon?: number;
  status: ParcelStatus;
  source: 'fast' | 'vision';
  scannedAt: number;
  latencyMs?: number; // aller-retour WebSocket mesuré côté mobile
  geoSource?: string;
  geoPrecision?: string;
}

// --- POST /api/v1/optimize-route ---------------------------------------------------------------
export interface Depot {
  lat: number;
  lon: number;
  label: string;
}

export interface RouteDelivery {
  stop_id: string;
  label: string;
  lat: number;
  lon: number;
  walk_s: number; // marche depuis la porte précédente (0 pour la porte du van)
}

export interface ParkingCluster {
  index: number;
  kind: 'SINGLE' | 'WALK_LOOP';
  park: { stop_id: string; label: string; lat: number; lon: number };
  drive_s: number;
  walk_loop_s: number;
  return_walk_s: number;
  service_s: number;
  deliveries: RouteDelivery[];
}

export interface PlanTotals {
  drive_s: number;
  walk_s: number;
  service_s: number;
  total_s: number;
  van_stops: number;
  parcels: number;
}

export interface RouteResponse {
  plan: {
    strategy: 'VAN_STRICT' | 'HYBRID_SPEC' | 'HYBRID_POCKETS';
    totals: PlanTotals;
    drive_back_s: number;
    clusters: ParkingCluster[];
  };
  comparison: {
    van_only_total_s: number;
    chosen_total_s: number;
    chosen_strategy: string;
    candidates_s: Record<string, number>;
    saved_s: number;
    saved_pct: number;
  };
  depot: Depot;
  unresolved: string[];
  geocode?: { requested: number; cache_hits: number; unresolved: number; synthetic: number; approximate?: number };
  timings_ms: Record<string, number>;
}
