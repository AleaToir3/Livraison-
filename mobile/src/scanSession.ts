import { cacheKey, isFastPathValid, parseUkAddress, type ParsedAddress } from './ukAddressParser';

export type Decision =
  | { kind: 'none' }
  | { kind: 'submit'; address: ParsedAddress; key: string }
  | { kind: 'fallback' }; // une étiquette est dans le champ mais reste illisible : chemin lent (ROI WebP)

export interface ScanSessionOptions {
  /** Lectures identiques consécutives exigées avant l'envoi (écarte 14 vs 11 d'un OCR instable). */
  confirmReads: number;
  /**
   * Une adresse déjà envoyée n'est renvoyée qu'après avoir quitté le champ pendant ce délai (ms) :
   * tant que l'étiquette reste devant la caméra, elle reste bloquée, même au-delà de ce délai.
   */
  dedupeMs: number;
  /** Étiquette illisible pendant ce délai (ms) => bascule vers le chemin lent. */
  unreadableMs: number;
  autoFallback: boolean;
  /** Délai minimal entre deux bascules automatiques (ms). */
  fallbackCooldownMs?: number;
  /** En deçà, le texte vu n'est pas une étiquette (rayonnage, mur...). */
  minLabelChars?: number;
}

function tokens(text: string): Set<string> {
  return new Set(text.toUpperCase().split(/[^A-Z0-9]+/).filter((t) => t.length > 1));
}

function similarity(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 || b.size === 0) return 0;
  let common = 0;
  for (const t of a) if (b.has(t)) common++;
  return common / (a.size + b.size - common);
}

/**
 * Décide, pour chaque texte renvoyé par l'OCR embarqué, s'il faut envoyer un scan, attendre ou basculer
 * vers le chemin lent. Logique pure (aucune API native) donc testable sous Node.
 */
export class ScanSession {
  private candidate: { key: string; hits: number } | null = null;
  private readonly submitted = new Map<string, number>(); // clé -> dernière fois vue après l'envoi
  private unreadableSince: number | null = null;
  private lastFallbackAt = -Infinity;
  private lastFallbackTokens: Set<string> | null = null;

  constructor(private readonly o: ScanSessionOptions) {}

  ingest(text: string, now: number): Decision {
    const address = parseUkAddress(text);
    if (isFastPathValid(address)) {
      this.unreadableSince = null;
      const key = cacheKey(address)!;
      const lastSeen = this.submitted.get(key);
      if (lastSeen !== undefined) {
        this.submitted.set(key, now); // toujours devant la caméra : on garde le blocage
        if (now - lastSeen < this.o.dedupeMs) return { kind: 'none' };
        this.submitted.delete(key); // absente assez longtemps : nouveau colis possible
      }
      this.candidate = this.candidate?.key === key ? { key, hits: this.candidate.hits + 1 } : { key, hits: 1 };
      if (this.candidate.hits < this.o.confirmReads) return { kind: 'none' };
      this.submitted.set(key, now);
      this.candidate = null;
      return { kind: 'submit', address, key };
    }

    this.candidate = null;
    const minChars = this.o.minLabelChars ?? 30;
    if (text.replace(/\s/g, '').length < minChars) {
      this.unreadableSince = null;
      return { kind: 'none' };
    }
    this.unreadableSince ??= now;
    if (!this.o.autoFallback || now - this.unreadableSince < this.o.unreadableMs) return { kind: 'none' };
    if (now - this.lastFallbackAt < (this.o.fallbackCooldownMs ?? 6000)) return { kind: 'none' };
    const seen = tokens(text);
    // Même étiquette toujours dans le champ après une tentative : on ne repaie pas un appel Gemini.
    if (this.lastFallbackTokens && now - this.lastFallbackAt < 30000 && similarity(seen, this.lastFallbackTokens) >= 0.6) {
      return { kind: 'none' };
    }
    this.lastFallbackAt = now;
    this.lastFallbackTokens = seen;
    this.unreadableSince = null;
    return { kind: 'fallback' };
  }

  /** Enregistre une bascule déclenchée à la main (bouton « Étiquette abîmée »). */
  noteManualFallback(now: number): void {
    this.lastFallbackAt = now;
    this.unreadableSince = null;
  }

  reset(): void {
    this.candidate = null;
    this.submitted.clear();
    this.unreadableSince = null;
    this.lastFallbackAt = -Infinity;
    this.lastFallbackTokens = null;
  }
}
