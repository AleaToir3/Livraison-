import assert from 'node:assert/strict';
import { test } from 'node:test';

import { textInGuide, uprightSize, type OcrBlock } from '../src/ocrRegion';

const view = { width: 400, height: 800 };
const guide = { x: 20, y: 176, width: 360, height: 240 }; // 90 % x 30 % de la vue, y = 22 %
const image = { width: 1080, height: 2160 }; // l'instantané a le rapport de la vue : échelle 2,7

/** Bloc dont le centre est (cx, cy) en points de la vue ; converti en pixels de l'instantané. */
const block = (text: string, cx: number, cy: number): OcrBlock => ({
  text,
  frame: { left: cx * 2.7 - 100, top: cy * 2.7 - 20, width: 200, height: 40 },
});

test('image redressée : le grand côté est vertical (app verrouillée en portrait)', () => {
  assert.deepEqual(uprightSize(1280, 720), { width: 720, height: 1280 });
  assert.deepEqual(uprightSize(720, 1280), { width: 720, height: 1280 });
});

test('seuls les blocs dans le cadre sont lus ; un colis voisin hors cadre est ignoré', () => {
  const inFrame = block('23 MIZZEN ROAD\nS43 4FX', 200, 300);
  const neighbour = block('99 OTHER STREET\nLS6 1AB', 200, 700);
  assert.equal(textInGuide([neighbour, inFrame], 'TOUT LE TEXTE', image, view, guide), '23 MIZZEN ROAD\nS43 4FX');
});

test('plusieurs blocs dans le cadre : triés de haut en bas', () => {
  const a = block('LIGNE BASSE', 200, 380);
  const b = block('LIGNE HAUTE', 200, 220);
  assert.equal(textInGuide([a, b], '', image, view, guide), 'LIGNE HAUTE\nLIGNE BASSE');
});

test('aucun bloc dans le cadre (ou géométrie inconnue) : on garde tout le texte plutôt que rien', () => {
  const far = block('AILLEURS', 200, 780);
  assert.equal(textInGuide([far], 'TOUT LE TEXTE', image, view, guide), 'TOUT LE TEXTE');
  assert.equal(textInGuide([], 'TOUT LE TEXTE', image, view, guide), 'TOUT LE TEXTE');
  assert.equal(textInGuide(undefined, 'TOUT LE TEXTE', image, view, guide), 'TOUT LE TEXTE');
  assert.equal(textInGuide([far], 'TOUT LE TEXTE', image, { width: 0, height: 0 }, guide), 'TOUT LE TEXTE');
});

test('un bloc sans cadre ou sans texte est ignoré', () => {
  const noFrame: OcrBlock = { text: 'SANS CADRE' };
  const noText: OcrBlock = { frame: { left: 500, top: 800, width: 100, height: 40 } };
  assert.equal(textInGuide([noFrame, noText], 'TOUT LE TEXTE', image, view, guide), 'TOUT LE TEXTE');
});

test('marge : un bloc juste hors du cadre mais dans la marge de 10 % est gardé', () => {
  const justAbove = block('LIGNE DU HAUT', 200, 176 - 10); // 10 pt au-dessus ; marge = 24 pt
  assert.equal(textInGuide([justAbove], '', image, view, guide), 'LIGNE DU HAUT');
});
