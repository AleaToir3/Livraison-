import assert from 'node:assert/strict';
import { test } from 'node:test';

import { blockCenterInView, textInGuide, uprightSize, type OcrBlock } from '../src/ocrRegion';

const view = { width: 400, height: 800 };
const guide = { x: 20, y: 176, width: 360, height: 240 }; // 90 % x 30 % de la vue, y = 22 %
const frame = { width: 1280, height: 720 }; // buffer paysage : image redressée 720 x 1280

const block = (text: string, cx: number, cy: number): OcrBlock => ({ blockText: text, blockFrame: { boundingCenterX: cx, boundingCenterY: cy } });

test('image redressée : le grand côté est vertical (app verrouillée en portrait)', () => {
  assert.deepEqual(uprightSize(1280, 720), { width: 720, height: 1280 });
  assert.deepEqual(uprightSize(720, 1280), { width: 720, height: 1280 });
});

test('projection : le centre de l\'image est le centre de la vue ; l\'aperçu « cover » recadre les côtés', () => {
  const up = uprightSize(1280, 720);
  assert.deepEqual(blockCenterInView(360, 640, up, view), { x: 200, y: 400 });
  // échelle = max(400/720, 800/1280) = 0.625 : la largeur de l'image (450 pt) dépasse la vue (400 pt)
  const left = blockCenterInView(0, 640, up, view);
  assert.equal(left.x, -25);
});

test('seuls les blocs dans le cadre sont lus ; un colis voisin hors cadre est ignoré', () => {
  // cadre de la vue : y 176..416 -> en pixels image (échelle 0,625, centre 640) : y 640 + (y-400)/0.625
  const inFrame = block('23 MIZZEN ROAD\nS43 4FX', 360, 640 + (300 - 400) / 0.625);
  const neighbour = block('99 OTHER STREET\nLS6 1AB', 360, 640 + (700 - 400) / 0.625);
  const text = textInGuide([neighbour, inFrame], 'TOUT LE TEXTE', frame, view, guide);
  assert.equal(text, '23 MIZZEN ROAD\nS43 4FX');
});

test('plusieurs blocs dans le cadre : triés de haut en bas', () => {
  const a = block('LIGNE BASSE', 360, 640 + (380 - 400) / 0.625);
  const b = block('LIGNE HAUTE', 360, 640 + (220 - 400) / 0.625);
  assert.equal(textInGuide([a, b], '', frame, view, guide), 'LIGNE HAUTE\nLIGNE BASSE');
});

test('aucun bloc dans le cadre (ou géométrie inconnue) : on garde tout le texte plutôt que rien', () => {
  const far = block('AILLEURS', 360, 1200);
  assert.equal(textInGuide([far], 'TOUT', frame, view, guide), 'TOUT');
  assert.equal(textInGuide([], 'TOUT', frame, view, guide), 'TOUT');
  assert.equal(textInGuide(undefined, 'TOUT', frame, view, guide), 'TOUT');
  assert.equal(textInGuide([{ blockText: 'X' }], 'TOUT', frame, view, guide), 'TOUT');
  assert.equal(textInGuide([far], 'TOUT', frame, { width: 0, height: 0 }, guide), 'TOUT');
});
