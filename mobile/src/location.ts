/**
 * Position GPS du téléphone : point de départ de la tournée quand le livreur n'est pas au dépôt.
 * Une seule lecture à la demande (pas de suivi en continu) : pas de service en arrière-plan, peu de batterie.
 */
import Geolocation from '@react-native-community/geolocation';
import { PermissionsAndroid, Platform } from 'react-native';

import { isInUk } from './settings';

/** Nom donné au point de départ quand c'est la position GPS (affiché sur l'écran Tournée). */
export const MY_POSITION_LABEL = 'Ma position';

const DENIED = 'Localisation refusée : autorisez-la dans les réglages du téléphone, ou choisissez « Départ : dépôt ».';

/** Erreur dont le message est destiné au livreur. */
export class LocationError extends Error {}

async function ensurePermission(): Promise<void> {
  if (Platform.OS === 'android') {
    const result = await PermissionsAndroid.requestMultiple([
      'android.permission.ACCESS_FINE_LOCATION',
      'android.permission.ACCESS_COARSE_LOCATION',
    ]);
    // Android 12+ : le livreur peut n'accorder que la position approximative, c'est suffisant pour partir d'ici.
    const granted = Object.values(result).some((r) => r === PermissionsAndroid.RESULTS.GRANTED);
    if (!granted) throw new LocationError(DENIED);
    return;
  }
  await new Promise<void>((resolve, reject) => {
    Geolocation.requestAuthorization(resolve, () => reject(new LocationError(DENIED)));
  });
}

function readPosition(): Promise<{ lat: number; lon: number }> {
  return new Promise((resolve, reject) => {
    Geolocation.getCurrentPosition(
      (position) => resolve({ lat: position.coords.latitude, lon: position.coords.longitude }),
      (error) =>
        reject(
          new LocationError(
            error.code === error.PERMISSION_DENIED
              ? DENIED
              : 'Position introuvable : activez la localisation (GPS) du téléphone et réessayez.',
          ),
        ),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
    );
  });
}

/** Position actuelle ; refuse une position hors du Royaume-Uni (le serveur ne sait calculer que là). */
export async function currentPosition(): Promise<{ lat: number; lon: number }> {
  await ensurePermission();
  const position = await readPosition();
  if (!isInUk(position.lat, position.lon)) {
    throw new LocationError('Votre position est hors du Royaume-Uni : choisissez « Départ : dépôt » ou rapprochez-vous de la zone de livraison.');
  }
  return position;
}
