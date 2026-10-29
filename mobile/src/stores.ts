import AsyncStorage from '@react-native-async-storage/async-storage';
import { useSyncExternalStore } from 'react';

import { ParcelStore } from './parcelStore';
import type { Parcel } from './types';

/** Colis de la journée, partagés entre le scanner et l'écran de tournée. */
export const parcelStore = new ParcelStore(AsyncStorage);

export function useParcels(): readonly Parcel[] {
  return useSyncExternalStore(parcelStore.subscribe, parcelStore.getSnapshot, parcelStore.getSnapshot);
}
