import assert from 'node:assert/strict';
import { test } from 'node:test';

import { ScanSocket, type SocketStatus, type WebSocketLike } from '../src/scanSocket';

class FakeSocket implements WebSocketLike {
  binaryType = 'blob';
  readyState = 0;
  sent: (string | ArrayBuffer | ArrayBufferView)[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  onclose: ((ev: unknown) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  constructor(public url: string) {}
  send(data: string | ArrayBuffer | ArrayBufferView): void {
    assert.equal(this.readyState, 1, 'envoi sur un socket non ouvert');
    this.sent.push(data);
  }
  close(): void {
    this.readyState = 3;
    this.onclose?.({});
  }
  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }
  reply(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
}

function harness(overrides: Record<string, unknown> = {}) {
  const sockets: FakeSocket[] = [];
  const socket = new ScanSocket({
    createSocket: (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    },
    url: () => 'ws://test/ws/scan',
    random: () => 0,
    ...overrides,
  });
  return { socket, sockets };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

test('les scans émis avant l\'ouverture sont mis en file puis envoyés dans l\'ordre', async () => {
  const { socket, sockets } = harness();
  socket.start();
  const a = socket.scan({ i: 1, p: 'LS61AB', b: '14' });
  const b = socket.scan({ i: 2, p: 'LS61AB', b: '16' });
  assert.equal(sockets[0]!.sent.length, 0);
  sockets[0]!.open();
  assert.deepEqual(sockets[0]!.sent.map((m) => JSON.parse(m as string).i), [1, 2]);
  assert.equal(sockets[0]!.binaryType, 'arraybuffer');
  sockets[0]!.reply({ i: 2, ok: 1, la: 53.8, lo: -1.5, src: 'cache', pr: 'uprn', k: 'x', ms: 0.3 });
  sockets[0]!.reply({ i: 1, ok: 0, e: 'geocode_failed' });
  assert.equal(((await b) as { ok: number }).ok, 1);
  assert.equal(((await a) as { e?: string }).e, 'geocode_failed');
  socket.stop();
});

test('une requête sans réponse est rejetée après le délai', async () => {
  const { socket, sockets } = harness({ requestTimeoutMs: 30 });
  socket.start();
  sockets[0]!.open();
  await assert.rejects(socket.scan({ i: 7, p: 'LS61AB', b: '1' }), /délai/);
  socket.stop();
});

test('reconnexion automatique après une coupure, avec statuts successifs', async () => {
  const { socket, sockets } = harness();
  const seen: SocketStatus[] = [];
  socket.onStatus((s) => seen.push(s));
  socket.start();
  sockets[0]!.open();
  sockets[0]!.close();
  await sleep(250);
  assert.equal(sockets.length, 2, 'un second socket doit être créé');
  sockets[1]!.open();
  assert.deepEqual(seen, ['connecting', 'open', 'closed', 'connecting', 'open']);
  const p = socket.scan({ i: 3, p: 'LS61AB', b: '3' });
  sockets[1]!.reply({ i: 3, ok: 1, la: 1, lo: 2, src: 'cache', pr: 'uprn', k: 'k', ms: 0.1 });
  assert.equal(((await p) as { ok: number }).ok, 1);
  socket.stop();
});

test('une image part en trame binaire : 4 octets d\'identifiant big-endian puis le WebP', async () => {
  const { socket, sockets } = harness();
  socket.start();
  sockets[0]!.open();
  const webp = new Uint8Array([0x52, 0x49, 0x46, 0x46, 9, 9]);
  const p = socket.scanImage(0x01020304, webp);
  const frame = sockets[0]!.sent[0] as Uint8Array;
  assert.deepEqual([...frame.slice(0, 4)], [1, 2, 3, 4]);
  assert.deepEqual([...frame.slice(4)], [...webp]);
  sockets[0]!.reply({ i: 0x01020304, ok: 1 });
  assert.equal(((await p) as { ok: number }).ok, 1);
  socket.stop();
});

test('sans pong, la connexion morte est fermée puis rouverte', async () => {
  const { socket, sockets } = harness({ pingIntervalMs: 20, pongTimeoutMs: 20 });
  socket.start();
  sockets[0]!.open();
  await sleep(60);
  assert.ok(sockets[0]!.sent.includes('{"t":"ping"}'));
  await sleep(250);
  assert.ok(sockets.length >= 2, 'reconnexion attendue');
  socket.stop();
});

test('un pong maintient la connexion', async () => {
  const { socket, sockets } = harness({ pingIntervalMs: 20, pongTimeoutMs: 30 });
  socket.start();
  sockets[0]!.open();
  const keep = setInterval(() => sockets[0]!.reply({ t: 'pong' }), 10);
  await sleep(150);
  clearInterval(keep);
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0]!.readyState, 1);
  socket.stop();
});

test('stop() rejette les requêtes en attente et empêche toute reconnexion', async () => {
  const { socket, sockets } = harness();
  socket.start();
  const p = socket.scan({ i: 1, p: 'LS61AB', b: '1' });
  socket.stop();
  await assert.rejects(p, /arrêté/);
  await sleep(200);
  assert.equal(sockets.length, 1);
  assert.equal(socket.getStatus(), 'closed');
});

test('identifiants de scan croissants', () => {
  const { socket } = harness();
  assert.deepEqual([socket.nextScanId(), socket.nextScanId(), socket.nextScanId()], [1, 2, 3]);
});
