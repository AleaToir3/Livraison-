import assert from 'node:assert/strict';
import { test } from 'node:test';

import { outputSize, roiToImageRect } from '../src/roiGeometry';

test('le ROI de la vue est projeté sur l\'image de l\'instantané (homothétie)', () => {
  const r = roiToImageRect({ x: 20, y: 200, width: 340, height: 200 }, { width: 400, height: 800 }, { width: 1200, height: 2400 });
  assert.deepEqual(r, { x: 60, y: 600, width: 1020, height: 600 });
});

test('le ROI reste dans l\'image même s\'il déborde de la vue', () => {
  const r = roiToImageRect({ x: -10, y: 700, width: 500, height: 300 }, { width: 400, height: 800 }, { width: 400, height: 800 });
  assert.deepEqual(r, { x: 0, y: 700, width: 400, height: 100 });
});

test('taille de sortie : réduite à 640 px, jamais agrandie', () => {
  assert.deepEqual(outputSize({ x: 0, y: 0, width: 1020, height: 600 }, 640), { width: 640, height: 376 });
  assert.deepEqual(outputSize({ x: 0, y: 0, width: 300, height: 90 }, 640), { width: 300, height: 90 });
});
