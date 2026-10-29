import type { Parcel } from './types';

/** Stockage clé/valeur asynchrone (AsyncStorage en production, un faux dans les tests). */
export interface KeyValueStorage {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
}

const KEY = 'parcels:v1';

/**
 * Colis scannés de la journée. Les tableaux sont immuables (un nouveau tableau à chaque changement) pour
 * que `useSyncExternalStore` détecte les mises à jour ; la persistance est différée pour ne pas
 * écrire à chaque réponse du serveur.
 */
export class ParcelStore {
  private parcels: readonly Parcel[] = [];
  private readonly listeners = new Set<() => void>();
  private saveTimer: ReturnType<typeof setTimeout> | null = null;
  private seq = 0;

  constructor(
    private readonly storage: KeyValueStorage,
    private readonly now: () => number = Date.now,
    private readonly saveDelayMs = 300,
  ) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): readonly Parcel[] => this.parcels;

  async hydrate(): Promise<void> {
    try {
      const raw = await this.storage.getItem(KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as Parcel[];
      if (!Array.isArray(saved)) return;
      // Les scans restés « pending » (app fermée avant la réponse) sont à refaire : on les marque en échec.
      this.parcels = saved.map((p) => (p.status === 'pending' ? { ...p, status: 'failed' as const } : p));
      this.seq = this.parcels.length;
      this.emit(false);
    } catch {
      /* stockage illisible : on repart d'une liste vide */
    }
  }

  add(input: Omit<Parcel, 'id' | 'scannedAt' | 'status'> & { status?: Parcel['status'] }): Parcel {
    const parcel: Parcel = {
      status: 'pending',
      ...input,
      id: `P${this.now().toString(36)}${(this.seq++).toString(36)}`,
      scannedAt: this.now(),
    };
    this.parcels = [...this.parcels, parcel];
    this.emit();
    return parcel;
  }

  update(id: string, patch: Partial<Parcel>): void {
    if (!this.parcels.some((p) => p.id === id)) return;
    this.parcels = this.parcels.map((p) => (p.id === id ? { ...p, ...patch, id: p.id } : p));
    this.emit();
  }

  remove(id: string): void {
    this.parcels = this.parcels.filter((p) => p.id !== id);
    this.emit();
  }

  clear(): void {
    this.parcels = [];
    this.emit();
  }

  /** Force l'écriture immédiate (fin de test, passage en arrière-plan). */
  async flush(): Promise<void> {
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = null;
    await this.storage.setItem(KEY, JSON.stringify(this.parcels));
  }

  private emit(persist = true): void {
    for (const l of this.listeners) l();
    if (!persist) return;
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => {
      this.saveTimer = null;
      this.storage.setItem(KEY, JSON.stringify(this.parcels)).catch(() => undefined);
    }, this.saveDelayMs);
  }
}
