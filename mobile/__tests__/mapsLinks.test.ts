import assert from 'node:assert/strict';
import { test } from 'node:test';

import { dropRepeats, mapsLegUrls, type LatLon } from '../src/mapsLinks';

/** N points distincts, à peu près alignés. */
const line = (n: number): LatLon[] => Array.from({ length: n }, (_, i) => ({ lat: 53 + i * 0.01, lon: -1.5 }));
const coord = (p: LatLon): string => `${p.lat.toFixed(6)},${p.lon.toFixed(6)}`;
const params = (url: string) => new URL(url).searchParams;
/** Élément d'un tableau, avec la preuve qu'il existe (le typage strict l'impose). */
function at<T>(items: readonly T[], i: number): T {
  const item = items[i];
  assert.ok(item !== undefined, `élément ${i} manquant`);
  return item;
}

test('aucun arrêt : aucun lien', () => {
  assert.deepEqual(mapsLegUrls([]), []);
});

test('un seul arrêt : destination seule, départ depuis la position actuelle', () => {
  const url = at(mapsLegUrls(line(1)), 0);
  const p = params(url);
  assert.equal(p.get('destination'), '53.000000,-1.500000');
  assert.equal(p.get('origin'), null);
  assert.equal(p.get('waypoints'), null);
  assert.equal(p.get('travelmode'), 'driving');
  assert.equal(new URL(url).origin + new URL(url).pathname, 'https://www.google.com/maps/dir/');
});

test('plusieurs arrêts : l’ordre est conservé, le dernier est la destination', () => {
  const pts = line(4);
  const urls = mapsLegUrls(pts);
  assert.equal(urls.length, 1);
  const p = params(at(urls, 0));
  assert.equal(p.get('destination'), coord(at(pts, 3)));
  assert.equal(p.get('waypoints'), pts.slice(0, 3).map(coord).join('|'));
});

test('deux arrêts consécutifs au même endroit ne comptent qu’une fois', () => {
  const a = { lat: 53.5, lon: -1.5 };
  const b = { lat: 53.6, lon: -1.6 };
  assert.deepEqual(dropRepeats([a, { ...a }, b, a]), [a, b, a]);
  const p = params(at(mapsLegUrls([a, { ...a }, b]), 0));
  assert.equal(p.get('waypoints'), coord(a));
  assert.equal(p.get('destination'), coord(b));
});

test('10 points tiennent en un lien : 9 étapes + l’arrivée', () => {
  const urls = mapsLegUrls(line(10));
  assert.equal(urls.length, 1);
  assert.equal(params(at(urls, 0)).get('waypoints')?.split('|').length, 9);
});

test('11 points : deux parties, la seconde repart de l’arrivée de la première', () => {
  const pts = line(11);
  const urls = mapsLegUrls(pts);
  assert.equal(urls.length, 2);
  const first = params(at(urls, 0));
  assert.equal(first.get('destination'), coord(at(pts, 9)));
  assert.equal(first.get('origin'), null);
  const second = params(at(urls, 1));
  assert.equal(second.get('origin'), coord(at(pts, 9)));
  assert.equal(second.get('destination'), coord(at(pts, 10)));
  assert.equal(second.get('waypoints'), null);
});

test('25 points : trois parties, chacune d’au plus 9 étapes, aucun point perdu', () => {
  const pts = line(25);
  const urls = mapsLegUrls(pts);
  assert.equal(urls.length, 3);
  const seen: string[] = [];
  for (const url of urls) {
    const p = params(url);
    const via = p.get('waypoints')?.split('|') ?? [];
    assert.ok(via.length <= 9);
    seen.push(...via, at([p.get('destination')], 0) as string);
  }
  assert.deepEqual(seen, pts.map(coord));
});
