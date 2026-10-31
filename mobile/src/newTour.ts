import AsyncStorage from '@react-native-async-storage/async-storage';
import { Alert } from 'react-native';

import { parcelStore } from './stores';

/** Tournée calculée et avancement, enregistrés par `DriverRouteScreen`. */
export const ROUTE_STORAGE_KEY = 'route:v1';

/** Demande confirmation, puis vide la liste de colis et la tournée enregistrée. */
export function confirmClearParcels(onCleared?: () => void): void {
  const n = parcelStore.getSnapshot().length;
  Alert.alert(
    'Vider la liste ?',
    `${n} colis seront supprimés, ainsi que la tournée en cours.`,
    [
      { text: 'Annuler', style: 'cancel' },
      {
        text: 'Vider',
        style: 'destructive',
        onPress: () => {
          void AsyncStorage.removeItem(ROUTE_STORAGE_KEY).catch(() => undefined);
          parcelStore.clear();
          onCleared?.();
        },
      },
    ],
    { cancelable: true },
  );
}
