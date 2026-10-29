/**
 * Ne garder que le texte situé dans le cadre jaune.
 *
 * Quand le livreur zoome sur l'adresse, l'OCR voit pourtant toute l'image : sans filtre, l'étiquette d'un
 * colis voisin pourrait être scannée à la place. Chaque bloc reconnu (ML Kit) a un centre en pixels de l'image
 * redressée ; on le projette dans la vue (l'aperçu est en mode « cover », centré) et on garde les blocs dont
 * le centre tombe dans le cadre.
 *
 * Préférence « douce » : si AUCUN bloc ne tombe dans le cadre, on garde tout le texte. Ainsi une projection
 * fausse (orientation inattendue sur un appareil) dégrade le filtre au lieu de bloquer tout scan.
 *
 * Les fonctions portent la directive 'worklet' : elles s'exécutent dans le Frame Processor, sur le thread caméra.
 */

export interface OcrBlock {
  blockText?: string;
  blockFrame?: { boundingCenterX?: number; boundingCenterY?: number };
}

export interface Size {
  width: number;
  height: number;
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Taille de l'image redressée pour une app verrouillée en portrait : le grand côté est vertical. */
export function uprightSize(frameWidth: number, frameHeight: number): Size {
  'worklet';
  return { width: Math.min(frameWidth, frameHeight), height: Math.max(frameWidth, frameHeight) };
}

/** Centre d'un bloc, converti des pixels de l'image redressée vers les points de la vue (aperçu « cover »). */
export function blockCenterInView(
  centerX: number,
  centerY: number,
  upright: Size,
  view: Size,
): { x: number; y: number } {
  'worklet';
  const scale = Math.max(view.width / upright.width, view.height / upright.height);
  return {
    x: (centerX - upright.width / 2) * scale + view.width / 2,
    y: (centerY - upright.height / 2) * scale + view.height / 2,
  };
}

export function textInGuide(
  blocks: readonly OcrBlock[] | null | undefined,
  fallbackText: string,
  frame: Size,
  view: Size,
  guide: Rect,
  margin = 0.1,
): string {
  'worklet';
  if (!blocks || blocks.length === 0 || view.width <= 0 || view.height <= 0) return fallbackText;
  const upright = uprightSize(frame.width, frame.height);
  const left = guide.x - guide.width * margin;
  const right = guide.x + guide.width * (1 + margin);
  const top = guide.y - guide.height * margin;
  const bottom = guide.y + guide.height * (1 + margin);

  const inside: { y: number; text: string }[] = [];
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]!;
    const f = b.blockFrame;
    if (!b.blockText || !f || f.boundingCenterX === undefined || f.boundingCenterY === undefined) continue;
    const c = blockCenterInView(f.boundingCenterX, f.boundingCenterY, upright, view);
    if (c.x >= left && c.x <= right && c.y >= top && c.y <= bottom) inside.push({ y: c.y, text: b.blockText });
  }
  if (inside.length === 0) return fallbackText;
  inside.sort((a, b) => a.y - b.y);
  return inside.map((b) => b.text).join('\n');
}
