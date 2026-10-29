/**
 * Client WebSocket persistant vers `/ws/scan`.
 *
 * La connexion reste ouverte pendant toute la session pour supprimer le handshake TCP/TLS à chaque
 * colis. Reconnexion automatique (backoff exponentiel avec jitter), file d'attente hors-ligne,
 * ping applicatif toutes les 15 s pour détecter les connexions mortes (NAT mobile).
 */
import type { ScanReply, ScanRequest } from './types';

export type SocketStatus = 'connecting' | 'open' | 'closed';

/** Sous-ensemble de l'API WebSocket standard (permet d'injecter un faux socket dans les tests). */
export interface WebSocketLike {
  binaryType: string;
  readyState: number;
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: unknown) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  send(data: string | ArrayBuffer | ArrayBufferView): void;
  close(): void;
}

export interface ScanSocketOptions {
  createSocket: (url: string) => WebSocketLike;
  url: () => string;
  requestTimeoutMs?: number;
  imageTimeoutMs?: number;
  pingIntervalMs?: number;
  pongTimeoutMs?: number;
  maxQueue?: number;
  random?: () => number;
}

interface Pending {
  resolve: (reply: ScanReply) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

const OPEN = 1;

export class ScanSocket {
  private ws: WebSocketLike | null = null;
  private status: SocketStatus = 'closed';
  private wanted = false;
  private backoffMs = 250;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private pongTimer: ReturnType<typeof setTimeout> | null = null;
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly queue: (string | Uint8Array)[] = [];
  private readonly listeners = new Set<(s: SocketStatus) => void>();
  private readonly o: Required<Omit<ScanSocketOptions, 'createSocket' | 'url'>> &
    Pick<ScanSocketOptions, 'createSocket' | 'url'>;

  constructor(options: ScanSocketOptions) {
    this.o = {
      requestTimeoutMs: 4000,
      imageTimeoutMs: 8000,
      pingIntervalMs: 15000,
      pongTimeoutMs: 5000,
      maxQueue: 200,
      random: Math.random,
      ...options,
    };
  }

  // --- cycle de vie --------------------------------------------------------------------------
  start(): void {
    this.wanted = true;
    this.ensureConnected();
  }

  stop(): void {
    this.wanted = false;
    this.clearTimers();
    this.ws?.close();
    this.ws = null;
    this.setStatus('closed');
    this.failAll(new Error('socket arrêté'));
  }

  /** À appeler quand l'app revient au premier plan : une connexion mobile peut être morte sans le dire. */
  ensureConnected(): void {
    if (!this.wanted) return;
    if (this.ws && this.ws.readyState === OPEN) return;
    if (this.status === 'connecting') return;
    this.connect();
  }

  getStatus(): SocketStatus {
    return this.status;
  }

  onStatus(listener: (status: SocketStatus) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  // --- envoi --------------------------------------------------------------------------------
  /** Prochain identifiant de scan (entier 32 bits, croissant). */
  nextScanId(): number {
    const id = this.nextId;
    this.nextId = (this.nextId % 0xfffffffe) + 1;
    return id;
  }

  /** Envoie un scan fast-path ; la promesse est résolue par la réponse portant le même `i`. */
  scan(request: ScanRequest): Promise<ScanReply> {
    return this.request(request.i, JSON.stringify(request), this.o.requestTimeoutMs);
  }

  /** Trame binaire : 4 octets d'identifiant (big-endian) + image WebP du ROI. */
  scanImage(id: number, webp: Uint8Array): Promise<ScanReply> {
    const frame = new Uint8Array(4 + webp.length);
    new DataView(frame.buffer).setUint32(0, id, false);
    frame.set(webp, 4);
    return this.request(id, frame, this.o.imageTimeoutMs);
  }

  private request(id: number, data: string | Uint8Array, timeoutMs: number): Promise<ScanReply> {
    return new Promise<ScanReply>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('délai dépassé'));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.dispatch(data);
    });
  }

  private dispatch(data: string | Uint8Array): void {
    if (this.ws && this.ws.readyState === OPEN) {
      this.ws.send(typeof data === 'string' ? data : data);
      return;
    }
    if (this.queue.length >= this.o.maxQueue) this.queue.shift();
    this.queue.push(data);
    this.ensureConnected();
  }

  // --- connexion -----------------------------------------------------------------------------
  private connect(): void {
    this.clearReconnect();
    this.setStatus('connecting');
    let ws: WebSocketLike;
    try {
      ws = this.o.createSocket(this.o.url());
    } catch {
      this.scheduleReconnect();
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.backoffMs = 250;
      this.setStatus('open');
      this.startPing();
      for (const data of this.queue.splice(0)) ws.send(typeof data === 'string' ? data : data);
    };
    ws.onmessage = (ev) => {
      if (this.ws !== ws || typeof ev.data !== 'string') return;
      this.onMessage(ev.data);
    };
    const lost = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.clearPing();
      this.setStatus('closed');
      this.scheduleReconnect();
    };
    ws.onclose = lost;
    ws.onerror = lost;
  }

  private onMessage(text: string): void {
    let msg: { t?: string; i?: number | null };
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.t === 'pong') {
      this.clearPong();
      return;
    }
    if (typeof msg.i === 'number') {
      const pending = this.pending.get(msg.i);
      if (pending) {
        clearTimeout(pending.timer);
        this.pending.delete(msg.i);
        pending.resolve(msg as ScanReply);
      }
    }
  }

  private scheduleReconnect(): void {
    if (!this.wanted || this.reconnectTimer) return;
    const delay = this.backoffMs * (0.5 + this.o.random());
    this.backoffMs = Math.min(this.backoffMs * 2, 8000);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private startPing(): void {
    this.clearPing();
    this.pingTimer = setInterval(() => {
      if (!this.ws || this.ws.readyState !== OPEN) return;
      this.ws.send('{"t":"ping"}');
      this.clearPong();
      this.pongTimer = setTimeout(() => this.ws?.close(), this.o.pongTimeoutMs);
    }, this.o.pingIntervalMs);
  }

  // --- minuteries ----------------------------------------------------------------------------
  private clearPong(): void {
    if (this.pongTimer) clearTimeout(this.pongTimer);
    this.pongTimer = null;
  }

  private clearPing(): void {
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
    this.clearPong();
  }

  private clearReconnect(): void {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private clearTimers(): void {
    this.clearPing();
    this.clearReconnect();
  }

  private failAll(error: Error): void {
    for (const [id, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(error);
      this.pending.delete(id);
    }
    this.queue.length = 0;
  }

  private setStatus(status: SocketStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const l of this.listeners) l(status);
  }
}
