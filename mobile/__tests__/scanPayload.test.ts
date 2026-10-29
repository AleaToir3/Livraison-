import assert from 'node:assert/strict';
import { test } from 'node:test';

import { MAX_SCAN_BYTES, buildScanRequest, buildingToken } from '../src/scanPayload';
import { parseUkAddress } from '../src/ukAddressParser';

const bytes = (o: unknown) => Buffer.byteLength(JSON.stringify(o));

test('fast-path standard : moins de 50 octets, sans image', () => {
  const a = parseUkAddress('MR J SMITH\n14A KINGSWAY RD\nLEEDS\nLS6 1AB');
  const req = buildScanRequest(17, a);
  assert.deepEqual(req, { i: 17, p: 'LS61AB', b: '14A', s: 'KINGSWAY RD' });
  assert.ok(bytes(req) < 50, String(bytes(req)));
});

test('la rue est retirée si elle fait dépasser la limite', () => {
  const a = parseUkAddress('MR J SMITH\n14 ALBION AVENUE EXTENSION ROAD\nLEEDS\nLS6 1AB');
  const req = buildScanRequest(123456, a);
  assert.ok(bytes(req) <= MAX_SCAN_BYTES);
  const long = parseUkAddress('MR J SMITH\n14 THE VERY LONG ESTABLISHED GARDENS CRESCENT\nLEEDS\nLS6 1AB');
  assert.deepEqual(buildScanRequest(1, long), { i: 1, p: 'LS61AB', b: '14' });
});

test('nom de maison', () => {
  const a = parseUkAddress('MISS L BROWN\nROSE COTTAGE\nPRIMROSE CLOSE\nLEEDS\nLS6 2QA');
  assert.equal(buildingToken(a), 'ROSE COTTAGE');
  const req = buildScanRequest(5, a);
  assert.equal(req.b, 'ROSE COTTAGE');
  assert.equal(req.p, 'LS62QA');
});

test('adresse « UNIT 4, MILL INDUSTRIAL ESTATE » : bâtiment vide, unité et rue envoyées', () => {
  const a = parseUkAddress('ACME LTD\nUNIT 4\nMILL INDUSTRIAL ESTATE\nLEEDS\nLS11 5AB');
  assert.equal(buildingToken(a), '');
  assert.deepEqual(buildScanRequest(9, a), { i: 9, p: 'LS115AB', b: '', f: 'UNIT 4', s: 'MILL INDUSTRIAL ESTATE' });
});

test('sans code postal ou sans bâtiment, aucun message', () => {
  assert.throws(() => buildScanRequest(1, parseUkAddress('hello')), /code postal/);
  assert.throws(() => buildScanRequest(1, parseUkAddress('ROSE HILL\nALBION AVENUE\nLEEDS\nLS6 1AB')), /bâtiment/);
});
