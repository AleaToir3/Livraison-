import AsyncStorage from '@react-native-async-storage/async-storage';
import { useSyncExternalStore } from 'react';

import { ParcelStore } from './parcelStore';
import { SettingsStore, type Settings } from './settings';
import type { Parcel } from './types';

/** Colis de la journée, partagés entre le scanner et l'écran de tournée. */
export const parcelStore = new ParcelStore(AsyncStorage);

/** Réglages saisis sur le téléphone (serveur, clé API, dépôt). */
export const settingsStore = new SettingsStore(AsyncStorage);

export function useParcels(): readonly Parcel[] {
  return useSyncExternalStore(parcelStore.subscribe, parcelStore.getSnapshot, parcelStore.getSnapshot);
}

export function useSettings(): Settings {
  return useSyncExternalStore(settingsStore.subscribe, settingsStore.getSnapshot, settingsStore.getSnapshot);
}
