/**
 * Ne garder que le texte situé dans le cadre jaune.
 *
 * Quand le livreur zoome sur l'adresse, l'OCR voit pourtant tout l'instantané : sans filtre, l'étiquette d'un
 * colis voisin pourrait être scannée à la place. L'instantané reproduit l'aperçu tel qu'affiché (même rapport
 * largeur/hauteur, voir `roiGeometry`) : on projette le cadre sur l'image par une simple homothétie, et on garde
 * les blocs ML Kit dont le centre tombe dans le cadre.
 *
 * Préférence « douce » : si AUCUN bloc ne tombe dans le cadre, on garde tout le texte. Ainsi une projection
 * fausse (orientation inattendue sur un appareil) dégrade le filtre au lieu de bloquer tout scan.
 */
import { roiToImageRect, type RoiRect, type ViewSize } from './roiGeometry';

/** Sous-ensemble de `TextBlock` (@react-native-ml-kit/text-recognition) dont on a besoin. */
export interface OcrBlock {
  text?: string;
  frame?: { left: number; top: number; width: number; height: number };
}

/** Taille de l'image redressée pour une app verrouillée en portrait : le grand côté est vertical. */
export function uprightSize(width: number, height: number): ViewSize {
  return { width: Math.min(width, height), height: Math.max(width, height) };
}

export function textInGuide(
  blocks: readonly OcrBlock[] | null | undefined,
  fallbackText: string,
  image: ViewSize,
  view: ViewSize,
  guide: RoiRect,
  margin = 0.1,
): string {
  if (!blocks || blocks.length === 0 || view.width <= 0 || view.height <= 0 || image.width <= 0 || image.height <= 0) {
    return fallbackText;
  }
  const g = roiToImageRect(guide, view, image);
  const left = g.x - g.width * margin;
  const right = g.x + g.width * (1 + margin);
  const top = g.y - g.height * margin;
  const bottom = g.y + g.height * (1 + margin);

  const inside: { y: number; text: string }[] = [];
  for (const b of blocks) {
    const f = b.frame;
    if (!b.text || !f) continue;
    const cx = f.left + f.width / 2;
    const cy = f.top + f.height / 2;
    if (cx >= left && cx <= right && cy >= top && cy <= bottom) inside.push({ y: cy, text: b.text });
  }
  if (inside.length === 0) return fallbackText;
  inside.sort((a, b) => a.y - b.y);
  return inside.map((b) => b.text).join('\n');
}
