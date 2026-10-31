import assert from 'node:assert/strict';
import { test } from 'node:test';

import { buildOptimizeBody } from '../src/routeBody';
import type { Parcel } from '../src/types';

const parcel = (over: Partial<Parcel>): Parcel =>
  ({ id: 'p1', label: '1 Rue A, LS6 1AB', postcode: 'LS6 1AB', building: '1', status: 'ok', source: 'fast', scannedAt: 0, ...over }) as Parcel;

const here = { lat: 52.64, lon: -1.12, label: 'Ma position' };

test('le point de départ part comme « depot » et le retour est demandé par défaut', () => {
  const body = buildOptimizeBody([parcel({ lat: 52.6, lon: -1.1 })], here);
  assert.deepEqual(body.depot, here);
  assert.deepEqual(body.options, { return_to_depot: true });
});

test('tournée ouverte : return_to_depot vaut false', () => {
  const body = buildOptimizeBody([parcel({ lat: 52.6, lon: -1.1 })], here, false);
  assert.deepEqual(body.options, { return_to_depot: false });
});

test('colis géocodé : coordonnées ; sinon adresse', () => {
  const body = buildOptimizeBody([parcel({ id: 'a', lat: 52.6, lon: -1.1 }), parcel({ id: 'b' })], here);
  const [a, b] = body.stops as Record<string, unknown>[];
  assert.equal(a?.lat, 52.6);
  assert.equal(a?.postcode, undefined);
  assert.equal(b?.postcode, 'LS6 1AB');
  assert.equal(b?.lat, undefined);
});
