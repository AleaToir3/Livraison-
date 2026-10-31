import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  DEFAULT_SETTINGS,
  SettingsStore,
  UK_BOUNDS,
  authHeaders,
  isConfigured,
  isInUk,
  normalizeServerUrl,
  parseCoordinate,
  webSocketUrl,
  type KeyValueStorage,
} from '../src/settings';

const memory = (): KeyValueStorage & { data: Map<string, string> } => {
  const data = new Map<string, string>();
  return { data, getItem: async (k) => data.get(k) ?? null, setItem: async (k, v) => void data.set(k, v) };
};

test('adresse du serveur : nettoyage et https par défaut', () => {
  assert.deepEqual(normalizeServerUrl(' uk-api.onrender.com/ '), { ok: true, url: 'https://uk-api.onrender.com' });
  assert.deepEqual(normalizeServerUrl('https://x.up.railway.app///'), { ok: true, url: 'https://x.up.railway.app' });
  assert.deepEqual(normalizeServerUrl('HTTPS://Api.Example.co.uk:8443/base/'), { ok: true, url: 'https://Api.Example.co.uk:8443/base' });
});

test('le HTTP simple n\'est accepté qu\'en réseau local', () => {
  assert.equal(normalizeServerUrl('http://uk-api.onrender.com').ok, false);
  for (const ok of ['http://192.168.1.20:8000', 'http://10.0.2.2:8000', 'http://localhost:8000', 'http://172.16.0.5:8000']) {
    assert.equal(normalizeServerUrl(ok).ok, true, ok);
  }
});

test('adresses invalides', () => {
  for (const bad of ['', '   ', 'https://', 'ftp://x.com', 'mon serveur', 'monserveur', 'https://a b.com']) {
    assert.equal(normalizeServerUrl(bad).ok, false, JSON.stringify(bad));
  }
});

test('coordonnées', () => {
  assert.equal(parseCoordinate('53,7996', 49, 61), 53.7996);
  assert.equal(parseCoordinate(' -1.5491 ', -9, 2), -1.5491);
  assert.equal(parseCoordinate('abc', 49, 61), null);
  assert.equal(parseCoordinate('10', 49, 61), null);
});

test('WebSocket et en-têtes déduits des réglages', () => {
  const s = { ...DEFAULT_SETTINGS, serverUrl: 'https://uk.onrender.com', apiKey: 'a b&c' };
  assert.equal(webSocketUrl(s), 'wss://uk.onrender.com/ws/scan?key=a%20b%26c');
  assert.equal(webSocketUrl({ ...s, apiKey: '' }), 'wss://uk.onrender.com/ws/scan');
  assert.equal(webSocketUrl({ ...s, serverUrl: 'http://192.168.1.2:8000' }), 'ws://192.168.1.2:8000/ws/scan?key=a%20b%26c');
  assert.equal(webSocketUrl(DEFAULT_SETTINGS), '');
  assert.deepEqual(authHeaders(s, { 'Content-Type': 'x' }), { 'X-API-Key': 'a b&c', 'Content-Type': 'x' });
  assert.deepEqual(authHeaders({ ...s, apiKey: '' }), {});
  assert.equal(isConfigured(DEFAULT_SETTINGS), false);
  assert.equal(isConfigured(s), true);
});

test('SettingsStore : enregistre, recharge, ignore un contenu corrompu', async () => {
  const storage = memory();
  const store = new SettingsStore(storage);
  let notified = 0;
  store.subscribe(() => notified++);
  await store.save({
    serverUrl: 'https://x.onrender.com',
    apiKey: 'k',
    depot: { lat: 51.5, lon: -0.1, label: 'Londres' },
    startFromGps: true,
    returnToDepot: false,
  });
  assert.equal(notified, 1);
  const again = new SettingsStore(storage);
  assert.equal((await again.load()).depot.label, 'Londres');
  assert.equal(again.getSnapshot().serverUrl, 'https://x.onrender.com');
  assert.equal(again.getSnapshot().startFromGps, true);
  assert.equal(again.getSnapshot().returnToDepot, false);

  storage.data.set('settings:v1', '{oups');
  const broken = new SettingsStore(storage);
  assert.deepEqual(await broken.load(), DEFAULT_SETTINGS);
  storage.data.set('settings:v1', JSON.stringify({ serverUrl: 42, apiKey: null }));
  const odd = new SettingsStore(storage);
  assert.equal((await odd.load()).serverUrl, '');
});

test('réglages enregistrés par une ancienne version : départ au dépôt, retour au départ', async () => {
  const storage = memory();
  storage.data.set('settings:v1', JSON.stringify({ serverUrl: 'https://x.onrender.com', apiKey: 'k' }));
  const store = new SettingsStore(storage);
  const loaded = await store.load();
  assert.equal(loaded.startFromGps, false);
  assert.equal(loaded.returnToDepot, true);
  assert.equal(DEFAULT_SETTINGS.startFromGps, false);
  assert.equal(DEFAULT_SETTINGS.returnToDepot, true);
});

test('isInUk : le cadre du Royaume-Uni accepté par le serveur', () => {
  assert.equal(isInUk(52.63, -1.13), true); // Leicester
  assert.equal(isInUk(53.7996, -1.5491), true); // dépôt par défaut
  assert.equal(isInUk(48.85, 2.35), false); // Paris
  assert.equal(isInUk(UK_BOUNDS.latMax + 0.01, -1.5), false);
  assert.equal(isInUk(53.0, UK_BOUNDS.lonMax + 0.01), false);
});
