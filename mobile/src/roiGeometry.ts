/** Géométrie du ROI (pure, sans dépendance native : testable sous Node). */

/** Rectangle en points de la vue caméra (celle que le livreur voit à l'écran). */
export interface RoiRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ViewSize {
  width: number;
  height: number;
}

/**
 * Projette le ROI (coordonnées de la vue) sur l'image de l'instantané. Fonction pure, testée.
 * L'instantané reproduit la vue (même rapport largeur/hauteur) : une simple homothétie suffit.
 */
export function roiToImageRect(roi: RoiRect, view: ViewSize, image: ViewSize): RoiRect {
  const sx = image.width / view.width;
  const sy = image.height / view.height;
  const x = Math.max(0, Math.min(image.width - 1, roi.x * sx));
  const y = Math.max(0, Math.min(image.height - 1, roi.y * sy));
  return {
    x,
    y,
    width: Math.max(1, Math.min(image.width - x, roi.width * sx)),
    height: Math.max(1, Math.min(image.height - y, roi.height * sy)),
  };
}

/** Taille de sortie : on réduit, on n'agrandit jamais ; hauteur paire non nulle. */
export function outputSize(src: RoiRect, maxWidth: number): ViewSize {
  const width = Math.max(1, Math.round(Math.min(maxWidth, src.width)));
  return { width, height: Math.max(1, Math.round((width * src.height) / src.width)) };
}
