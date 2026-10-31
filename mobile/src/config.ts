/**
 * Paramètres de réglage fin du scanner. L'adresse du serveur, la clé API et le dépôt sont des réglages
 * utilisateur : voir `settings.ts` et l'écran Réglages.
 */
export interface ScannerTuning {
  /** Pause minimale entre deux lectures OCR (ms). Une lecture = un instantané + ML Kit sur l'appareil. */
  ocrIntervalMs: number;
  /** Nombre de lectures identiques consécutives avant d'envoyer un scan (anti-erreur d'OCR). */
  confirmReads: number;
  /** Absence minimale de l'étiquette du champ avant de pouvoir renvoyer la même adresse (ms). */
  dedupeMs: number;
  /** Étiquette visible mais illisible pendant ce délai (ms) : bascule automatique vers le chemin lent. */
  unreadableMs: number;
  autoFallback: boolean;
}

export const config: ScannerTuning = {
  ocrIntervalMs: 600,
  confirmReads: 2,
  dedupeMs: 2500,
  unreadableMs: 2500,
  autoFallback: true,
};
