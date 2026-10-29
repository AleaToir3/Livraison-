import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ScanSession } from '../src/scanSession';

const opts = { confirmReads: 2, dedupeMs: 4000, unreadableMs: 2500, autoFallback: true };
const LABEL = 'MR J SMITH\n14 ALBION AVENUE\nLEEDS\nLS6 1AB';
const TORN = 'MR J SMITH\n14 ALB...\n~~ TORN ~~\nJD0123456 GB TRACKED SIGNED FOR';

test('une adresse n\'est envoyée qu\'après deux lectures identiques', () => {
  const s = new ScanSession(opts);
  assert.equal(s.ingest(LABEL, 0).kind, 'none');
  const d = s.ingest(LABEL, 125);
  assert.equal(d.kind, 'submit');
  assert.equal(d.kind === 'submit' && d.key, 'LS61AB:14');
});

test('une lecture instable (14 puis 11) ne déclenche rien', () => {
  const s = new ScanSession(opts);
  assert.equal(s.ingest(LABEL, 0).kind, 'none');
  assert.equal(s.ingest(LABEL.replace('14', '11'), 125).kind, 'none');
  assert.equal(s.ingest(LABEL, 250).kind, 'none');
  assert.equal(s.ingest(LABEL, 375).kind, 'submit');
});

test('une étiquette qui reste devant la caméra n\'est jamais renvoyée, même après plusieurs secondes', () => {
  const s = new ScanSession(opts);
  s.ingest(LABEL, 0);
  assert.equal(s.ingest(LABEL, 100).kind, 'submit');
  for (let t = 225; t < 20000; t += 125) assert.equal(s.ingest(LABEL, t).kind, 'none');
});

test('après avoir quitté le champ assez longtemps, la même adresse peut être rescannée (2 colis, même adresse)', () => {
  const s = new ScanSession(opts);
  s.ingest(LABEL, 0);
  assert.equal(s.ingest(LABEL, 100).kind, 'submit');
  assert.equal(s.ingest('MUR BLANC', 1000).kind, 'none');
  assert.equal(s.ingest(LABEL, 6000).kind, 'none', 'première lecture : confirmation en cours');
  assert.equal(s.ingest(LABEL, 6125).kind, 'submit');
});

test('étiquette illisible persistante : bascule vers le chemin lent, une seule fois', () => {
  const s = new ScanSession(opts);
  assert.equal(s.ingest(TORN, 0).kind, 'none');
  assert.equal(s.ingest(TORN, 2000).kind, 'none');
  assert.equal(s.ingest(TORN, 2600).kind, 'fallback');
  // la même étiquette reste dans le champ : pas de second appel payant
  for (let t = 3000; t < 20000; t += 500) assert.equal(s.ingest(TORN, t).kind, 'none');
});

test('un texte court (rayonnage, mur) ne déclenche jamais le chemin lent', () => {
  const s = new ScanSession(opts);
  for (let t = 0; t < 10000; t += 125) assert.equal(s.ingest('EXIT 3', t).kind, 'none');
});

test('un texte lisible entre deux illisibles remet le compteur à zéro', () => {
  const s = new ScanSession(opts);
  s.ingest(TORN, 0);
  s.ingest(LABEL, 1000);
  assert.equal(s.ingest(TORN, 2600).kind, 'none');
});

test('autoFallback désactivé : jamais de bascule automatique', () => {
  const s = new ScanSession({ ...opts, autoFallback: false });
  for (let t = 0; t < 10000; t += 250) assert.equal(s.ingest(TORN, t).kind, 'none');
});
