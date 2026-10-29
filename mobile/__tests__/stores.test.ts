import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ParcelStore, type KeyValueStorage } from '../src/parcelStore';
import { buildOptimizeBody, parcelSignature } from '../src/routeBody';
import {
  EMPTY_PROGRESS,
  activeClusterIndex,
  completeCluster,
  counts,
  formatDuration,
  toggleDelivery,
} from '../src/routeProgress';
import type { Parcel, RouteResponse } from '../src/types';

function memoryStorage(): KeyValueStorage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return { data, getItem: async (k) => data.get(k) ?? null, setItem: async (k, v) => void data.set(k, v) };
}

const base = { postcode: 'LS6 1AB', building: '14', label: '14 ALBION AVENUE, LS6 1AB', source: 'fast' as const };

test('ParcelStore : ajout, mise à jour, notification et persistance', async () => {
  const storage = memoryStorage();
  const store = new ParcelStore(storage, () => 1_700_000_000_000, 5);
  let notified = 0;
  store.subscribe(() => notified++);
  const a = store.add(base);
  const b = store.add({ ...base, building: '16' });
  assert.notEqual(a.id, b.id);
  assert.equal(a.status, 'pending');
  store.update(a.id, { status: 'ready', lat: 53.8, lon: -1.5 });
  assert.equal(store.getSnapshot().find((p) => p.id === a.id)!.lat, 53.8);
  assert.equal(notified, 3);
  const before = store.getSnapshot();
  store.update('absent', { status: 'failed' });
  assert.equal(store.getSnapshot(), before, 'aucune mise à jour pour un id inconnu');
  await new Promise((r) => setTimeout(r, 30));
  assert.equal((JSON.parse(storage.data.get('parcels:v1')!) as Parcel[]).length, 2);

  const reloaded = new ParcelStore(storage, () => 1_700_000_000_001);
  await reloaded.hydrate();
  assert.deepEqual(
    reloaded.getSnapshot().map((p) => p.status),
    ['ready', 'failed'],
    'un scan resté en attente avant la fermeture est marqué en échec',
  );
  reloaded.remove(a.id);
  assert.equal(reloaded.getSnapshot().length, 1);
  reloaded.clear();
  assert.equal(reloaded.getSnapshot().length, 0);
});

test('corps optimize-route : coordonnées si connues, sinon adresse (le serveur retente)', () => {
  const p = (over: Partial<Parcel>): Parcel => ({ id: 'x', scannedAt: 0, status: 'pending', ...base, ...over });
  const parcels = [
    p({ id: 'a', status: 'ready', lat: 53.8, lon: -1.5 }),
    p({ id: 'b', building: '' , subBuilding: 'UNIT 4', street: 'MILL ESTATE' }),
    p({ id: 'c', status: 'failed' }),
  ];
  const body = buildOptimizeBody(parcels, { lat: 53.7, lon: -1.6, label: 'Dépôt' }) as { stops: Record<string, unknown>[] };
  assert.equal(body.stops.length, 3);
  assert.deepEqual(body.stops[0], { id: 'a', label: base.label, lat: 53.8, lon: -1.5 });
  assert.equal(body.stops[1]!.sub_building, 'UNIT 4');
  assert.equal(body.stops[1]!.building, undefined);
  assert.equal(body.stops[2]!.postcode, 'LS6 1AB');
  assert.equal(parcelSignature(parcels), 'a|b|c');
});

const route: RouteResponse = {
  plan: {
    strategy: 'HYBRID_POCKETS',
    totals: { drive_s: 600, walk_s: 90, service_s: 150, total_s: 840, van_stops: 2, parcels: 4 },
    drive_back_s: 60,
    clusters: [
      {
        index: 0, kind: 'SINGLE', park: { stop_id: 's1', label: 'A', lat: 1, lon: 1 }, drive_s: 60, walk_loop_s: 0,
        return_walk_s: 0, service_s: 30,
        deliveries: [{ stop_id: 's1', label: 'A', lat: 1, lon: 1, walk_s: 0 }],
      },
      {
        index: 1, kind: 'WALK_LOOP', park: { stop_id: 's2', label: 'B', lat: 2, lon: 2 }, drive_s: 120, walk_loop_s: 90,
        return_walk_s: 30, service_s: 90,
        deliveries: [
          { stop_id: 's2', label: 'B', lat: 2, lon: 2, walk_s: 0 },
          { stop_id: 's3', label: 'C', lat: 2, lon: 2, walk_s: 30 },
          { stop_id: 's4', label: 'D', lat: 2, lon: 2, walk_s: 30 },
        ],
      },
    ],
  },
  comparison: { van_only_total_s: 900, chosen_total_s: 840, chosen_strategy: 'HYBRID_POCKETS', candidates_s: {}, saved_s: 60, saved_pct: 6.7 },
  depot: { lat: 0, lon: 0, label: 'Dépôt' },
  unresolved: [],
  timings_ms: {},
};

test('progression : un colis seul termine son arrêt, une boucle exige « Reprenez le van »', () => {
  let pr = EMPTY_PROGRESS;
  assert.equal(activeClusterIndex(route, pr), 0);
  pr = toggleDelivery(route, pr, 's1');
  assert.equal(activeClusterIndex(route, pr), 1, 'arrêt à une porte terminé par la remise');
  pr = toggleDelivery(route, pr, 's2');
  pr = toggleDelivery(route, pr, 's3');
  assert.equal(completeCluster(route, pr, 1), pr, 'toutes les portes ne sont pas remises : rien ne change');
  pr = toggleDelivery(route, pr, 's4');
  assert.equal(activeClusterIndex(route, pr), 1, 'les portes sont remises mais le van n\'est pas repris');
  pr = completeCluster(route, pr, 1);
  assert.equal(activeClusterIndex(route, pr), -1);
  assert.deepEqual(counts(route, pr), { done: 4, total: 4 });
  pr = toggleDelivery(route, pr, 's3'); // annulation d'une remise rouvre l'arrêt
  assert.equal(activeClusterIndex(route, pr), 1);
  assert.deepEqual(counts(route, pr), { done: 3, total: 4 });
  assert.equal(toggleDelivery(route, pr, 'inconnu'), pr);
});

test('formatDuration', () => {
  assert.equal(formatDuration(0), '0 s');
  assert.equal(formatDuration(45), '45 s');
  assert.equal(formatDuration(60), '1 min');
  assert.equal(formatDuration(80), '1 min 20 s');
  assert.equal(formatDuration(3900), '1 h 05');
});
