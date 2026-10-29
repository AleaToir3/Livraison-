// Parité Python <-> TypeScript : le même fichier de fixtures valide les deux parseurs.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { join } from 'node:path';

import {
  POSTCODE_RE,
  cacheKey,
  displayAddress,
  isFastPathValid,
  makeCacheKey,
  normalizeBuildingToken,
  normalizePostcode,
  parseStructured,
  parseUkAddress,
} from '../src/ukAddressParser';

interface Fixture {
  name: string;
  text: string;
  expected: {
    postcode: string | null;
    building_number: string | null;
    sub_building: string | null;
    building_name: string | null;
    street: string | null;
    cache_key: string | null;
    valid: boolean;
  };
}

// `npm test` s'exécute depuis mobile/ ; les fixtures sont partagées avec le backend Python.
const fixtures: Fixture[] = JSON.parse(readFileSync(join(process.cwd(), '..', 'shared', 'address_fixtures.json'), 'utf8'));

for (const f of fixtures) {
  test(`fixture ${f.name}`, () => {
    const a = parseUkAddress(f.text);
    assert.equal(a.postcode, f.expected.postcode);
    assert.equal(a.buildingNumber, f.expected.building_number);
    assert.equal(a.subBuilding, f.expected.sub_building);
    assert.equal(a.buildingName, f.expected.building_name);
    assert.equal(a.street, f.expected.street);
    assert.equal(cacheKey(a), f.expected.cache_key);
    assert.equal(isFastPathValid(a), f.expected.valid);
  });
}

test('la regex officielle est celle du cahier des charges', () => {
  assert.equal(POSTCODE_RE.source, String.raw`\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b`);
});

test('normalisation du code postal et du bâtiment', () => {
  assert.equal(normalizePostcode('ls61ab'), 'LS6 1AB');
  assert.equal(normalizePostcode('sw1a2aa'), 'SW1A 2AA');
  assert.equal(normalizePostcode('ZZ9 9ZZ'), null);
  assert.equal(normalizePostcode(null), null);
  assert.equal(normalizeBuildingToken('14 a'), '14A');
  assert.equal(normalizeBuildingToken('12 - 14'), '12-14');
  assert.equal(normalizeBuildingToken('no. 9'), '9');
  assert.equal(normalizeBuildingToken('the  laurels'), 'THE LAURELS');
  assert.equal(makeCacheKey('LS6 1AB', 'ROSE COTTAGE'), 'LS61AB:ROSE-COTTAGE');
});

test('parseStructured distingue numéro et nom de maison', () => {
  const n = parseStructured('ls6 1ab', '14a', null, 'Albion Avenue');
  assert.deepEqual([n.postcode, n.buildingNumber, n.buildingName, n.street], ['LS6 1AB', '14A', null, 'ALBION AVENUE']);
  const h = parseStructured('LS6 1AB', 'Rose Cottage');
  assert.deepEqual([h.buildingNumber, h.buildingName], [null, 'ROSE COTTAGE']);
  const full = parseStructured('LS6 1AB', '14 Albion Avenue');
  assert.deepEqual([full.buildingNumber, full.street], ['14', 'ALBION AVENUE']);
  assert.equal(isFastPathValid(parseStructured('nope', '14')), false);
});

test('display() est lisible pour le livreur', () => {
  assert.equal(displayAddress(parseUkAddress('FLAT 2, 27 ALBION AVENUE\nLEEDS LS6 1AD')), 'FLAT 2, 27 ALBION AVENUE, LS6 1AD');
  assert.equal(displayAddress(parseUkAddress('ROSE COTTAGE\nPRIMROSE CLOSE\nLS6 2QA')), 'ROSE COTTAGE, PRIMROSE CLOSE, LS6 2QA');
});

test('le payload WebSocket du fast-path tient en moins de 50 octets', () => {
  const a = parseUkAddress('MR J SMITH\n14A ALBION AVENUE\nLEEDS\nLS6 1AB');
  const payload = JSON.stringify({ i: 17, p: a.postcode!.replace(' ', ''), b: a.buildingNumber ?? a.buildingName });
  assert.ok(payload.length < 50, payload);
});
