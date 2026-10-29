/**
 * Chemin lent : découpe du ROI de l'adresse dans un instantané de la caméra et compression en mémoire
 * en WebP (qualité 70 ; < 15 Ko visés) avec Skia. Aucun fichier intermédiaire n'est écrit en dehors
 * de l'instantané temporaire de VisionCamera.
 */
import { ImageFormat, Skia, type SkImage } from '@shopify/react-native-skia';

import { outputSize, roiToImageRect, type RoiRect, type ViewSize } from './roiGeometry';

export type { RoiRect, ViewSize } from './roiGeometry';

export interface WebpOptions {
  /** Largeur maximale du ROI encodé (px). 640 px suffisent à lire une adresse imprimée. */
  maxWidth?: number;
  /** Qualités essayées dans l'ordre jusqu'à passer sous `targetBytes`. */
  qualities?: number[];
  targetBytes?: number;
}

/**
 * iOS peut livrer l'instantané en paysage (pixels du capteur + orientation EXIF) alors que la vue est en
 * portrait : on le redresse de 90° dans le sens horaire. À VALIDER SUR APPAREIL (voir README).
 */
function uprightIfNeeded(image: SkImage, view: ViewSize): SkImage {
  const viewIsPortrait = view.height >= view.width;
  const imageIsPortrait = image.height() >= image.width();
  if (viewIsPortrait === imageIsPortrait) return image;
  const surface = Skia.Surface.MakeOffscreen(image.height(), image.width());
  if (!surface) return image;
  const canvas = surface.getCanvas();
  canvas.translate(image.height(), 0);
  canvas.rotate(90, 0, 0);
  canvas.drawImage(image, 0, 0);
  surface.flush();
  return surface.makeImageSnapshot();
}

export async function cropSnapshotToWebp(
  snapshotPath: string,
  roi: RoiRect,
  view: ViewSize,
  options: WebpOptions = {},
): Promise<Uint8Array> {
  const { maxWidth = 640, qualities = [70, 55, 40], targetBytes = 15 * 1024 } = options;
  const data = await Skia.Data.fromURI(snapshotPath.startsWith('file://') ? snapshotPath : `file://${snapshotPath}`);
  const decoded = Skia.Image.MakeImageFromEncoded(data);
  if (!decoded) throw new Error('instantané illisible');
  const image = uprightIfNeeded(decoded, view);

  const src = roiToImageRect(roi, view, { width: image.width(), height: image.height() });
  const size = outputSize(src, maxWidth);
  const surface = Skia.Surface.MakeOffscreen(size.width, size.height);
  if (!surface) throw new Error('surface Skia indisponible');
  surface.getCanvas().drawImageRect(
    image,
    Skia.XYWHRect(src.x, src.y, src.width, src.height),
    Skia.XYWHRect(0, 0, size.width, size.height),
    Skia.Paint(),
  );
  surface.flush();
  const cropped = surface.makeImageSnapshot();

  let bytes = new Uint8Array(0);
  for (const quality of qualities) {
    bytes = cropped.encodeToBytes(ImageFormat.WEBP, quality);
    if (bytes.length <= targetBytes) break;
  }
  return bytes;
}
