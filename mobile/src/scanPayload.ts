import { compactPostcode, type ParsedAddress } from './ukAddressParser';
import type { ScanRequest } from './types';

/** Au-delà, on retire la rue / l'appartement : le fast-path doit rester un message de quelques dizaines d'octets. */
export const MAX_SCAN_BYTES = 64;

/** Valeur de `Parcel.building` / du champ `b` : numéro, sinon nom de maison, sinon "" (adresse "UNIT 4, MILL ESTATE"). */
export function buildingToken(address: ParsedAddress): string {
  return address.buildingNumber ?? address.buildingName ?? '';
}

/**
 * Message WebSocket du fast-path : jamais d'image, uniquement le code postal et le bâtiment
 * (ex. `{"i":17,"p":"LS61AB","b":"14A"}` = 31 octets). La rue (aide pour le repli Google) et
 * l'appartement ne sont ajoutés que s'ils tiennent dans `MAX_SCAN_BYTES`.
 * Sans numéro ni nom de maison, le serveur reconstitue le bâtiment depuis `f` (unité) + `s` (rue).
 */
export function buildScanRequest(id: number, address: ParsedAddress): ScanRequest {
  if (address.postcode === null) throw new Error('code postal manquant');
  const b = buildingToken(address);
  const base: ScanRequest = { i: id, p: compactPostcode(address.postcode), b };
  if (b === '') {
    if (!address.subBuilding || !address.street) throw new Error('bâtiment manquant');
    return { ...base, f: address.subBuilding, s: address.street };
  }
  const variants: Partial<ScanRequest>[] = [
    { f: address.subBuilding ?? undefined, s: address.street ?? undefined },
    { s: address.street ?? undefined },
    { f: address.subBuilding ?? undefined },
  ];
  for (const extra of variants) {
    const filled = Object.fromEntries(Object.entries(extra).filter(([, v]) => v));
    if (Object.keys(filled).length === 0) continue;
    const candidate = { ...base, ...filled } as ScanRequest;
    if (JSON.stringify(candidate).length <= MAX_SCAN_BYTES) return candidate;
  }
  return base;
}
