/**
 * Scanner d'étiquettes UK « zéro latence ».
 *
 *  1. Boucle de lecture : un instantané de l'aperçu toutes les ~0,6 s, lu par Google ML Kit sur le téléphone
 *     (`@react-native-ml-kit/text-recognition`), sans quitter l'appareil. Pas de Frame Processor : il demande
 *     worklets-core, qui plantait en natif sur certains appareils.
 *  2. Le texte est parsé en local (`parseUkAddress`, miroir du parseur serveur).
 *  3. Fast-path (cas nominal) : code postal + numéro / nom de bâtiment valides => un message JSON de
 *     ~30 octets part sur le WebSocket déjà ouvert ; le serveur répond avec les coordonnées de la porte.
 *     Aucune image n'est envoyée.
 *  4. Slow-path (étiquette déchirée) : on découpe uniquement le cadre de visée dans un instantané, on le
 *     compresse en WebP (qualité 70) en mémoire, et on l'envoie à `POST /api/v1/extract-image` (Gemini).
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, AppState, Pressable, StyleSheet, Text, Vibration, View, type LayoutChangeEvent } from 'react-native';
import { unlink } from '@dr.pogodin/react-native-fs';
import TextRecognition from '@react-native-ml-kit/text-recognition';
import { Camera, useCameraDevice, useCameraFormat, useCameraPermission } from 'react-native-vision-camera';

import { config } from './config';
import { textInGuide, uprightSize } from './ocrRegion';
import { cropSnapshotToWebp, type RoiRect, type ViewSize } from './roiEncoder';
import { extractImage } from './routeApi';
import { buildScanRequest, buildingToken } from './scanPayload';
import { ScanSession } from './scanSession';
import type { ScanSocket, SocketStatus } from './scanSocket';
import { parcelStore, useParcels } from './stores';
import type { Parcel } from './types';
import { displayAddress, parseStructured, type ParsedAddress } from './ukAddressParser';

/** Lectures ratées d'affilée avant d'afficher une alerte (sinon une caméra bloquée resterait muette). */
const OCR_FAILURES_BEFORE_HINT = 5;

/** Le chemin d'un instantané peut venir avec ou sans `file://` selon la plateforme. */
const stripFileScheme = (path: string): string => path.replace(/^file:\/\//, '');

/** Cadre de visée : 90 % de la largeur, 30 % de la hauteur, légèrement au-dessus du centre. */
export function guideRect(view: ViewSize): RoiRect {
  return { x: view.width * 0.05, y: view.height * 0.22, width: view.width * 0.9, height: view.height * 0.3 };
}

export interface UKFastScannerProps {
  socket: ScanSocket;
  /** false quand l'écran n'est pas visible : la caméra et l'OCR sont alors coupés. */
  active: boolean;
  onOpenRoute: () => void;
  onOpenSettings: () => void;
}

const STATUS_LABEL: Record<SocketStatus, { text: string; color: string }> = {
  open: { text: 'Connecté', color: '#1faa59' },
  connecting: { text: 'Connexion…', color: '#e0a100' },
  closed: { text: 'Hors ligne : scans mis en file', color: '#d93025' },
};

export function UKFastScanner({ socket, active, onOpenRoute, onOpenSettings }: UKFastScannerProps): React.JSX.Element {
  const parcels = useParcels();
  const { hasPermission, requestPermission } = useCameraPermission();
  const device = useCameraDevice('back');
  const format = useCameraFormat(device, [{ videoResolution: { width: 1280, height: 720 } }, { fps: 30 }]);
  const camera = useRef<Camera>(null);

  const [view, setView] = useState<ViewSize>({ width: 0, height: 0 });
  const [appState, setAppState] = useState(AppState.currentState);
  const [socketStatus, setSocketStatus] = useState<SocketStatus>(socket.getStatus());
  const [torch, setTorch] = useState(false);
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<string | null>(null);
  const [zoomFactor, setZoomFactor] = useState(1);
  const busyRef = useRef(false);
  const viewRef = useRef(view);
  viewRef.current = view;

  const session = useMemo(
    () =>
      new ScanSession({
        confirmReads: config.confirmReads,
        dedupeMs: config.dedupeMs,
        unreadableMs: config.unreadableMs,
        autoFallback: config.autoFallback,
      }),
    [],
  );

  useEffect(() => {
    if (!hasPermission) void requestPermission();
  }, [hasPermission, requestPermission]);

  useEffect(() => socket.onStatus(setSocketStatus), [socket]);

  useEffect(() => {
    const sub = AppState.addEventListener('change', (next) => {
      setAppState(next);
      if (next === 'active') socket.ensureConnected();
      else void parcelStore.flush();
    });
    return () => sub.remove();
  }, [socket]);

  const flash = useCallback((message: string) => {
    setHint(message);
    setTimeout(() => setHint((h) => (h === message ? null : h)), 2500);
  }, []);

  // --- fast-path ---------------------------------------------------------------------------------
  const submitFast = useCallback(
    (address: ParsedAddress) => {
      const parcel = parcelStore.add({
        postcode: address.postcode!,
        building: buildingToken(address),
        subBuilding: address.subBuilding ?? undefined,
        street: address.street ?? undefined,
        label: displayAddress(address),
        source: 'fast',
      });
      Vibration.vibrate(25);
      const sentAt = Date.now();
      socket
        .scan(buildScanRequest(socket.nextScanId(), address))
        .then((reply) => {
          if (reply.ok === 1) {
            parcelStore.update(parcel.id, {
              status: 'ready',
              lat: reply.la,
              lon: reply.lo,
              latencyMs: Date.now() - sentAt,
              geoSource: reply.src,
              geoPrecision: reply.pr,
            });
          } else {
            parcelStore.update(parcel.id, { status: 'failed' });
            flash(`Adresse non localisée (${reply.e})`);
          }
        })
        .catch(() => {
          parcelStore.update(parcel.id, { status: 'failed' });
          flash('Pas de réponse du serveur : colis conservé, à relancer');
        });
    },
    [socket, flash],
  );

  // --- slow-path ------------------------------------------------------------------------------------
  const captureFallback = useCallback(
    async (manual: boolean) => {
      const size = viewRef.current;
      if (busyRef.current || !camera.current || size.width === 0) return;
      busyRef.current = true;
      setBusy(true);
      if (manual) session.noteManualFallback(Date.now());
      flash('Lecture de l\'étiquette abîmée…');
      let snapshotPath: string | undefined;
      try {
        const snapshot = await camera.current.takeSnapshot({ quality: 85 });
        snapshotPath = snapshot.path;
        const webp = await cropSnapshotToWebp(snapshot.path, guideRect(size), size);
        const result = await extractImage(webp);
        const a = result.address;
        const parsed = parseStructured(a.postcode, a.building_number ?? a.building_name, a.sub_building, a.street);
        parcelStore.add({
          postcode: parsed.postcode ?? '',
          building: buildingToken(parsed),
          subBuilding: parsed.subBuilding ?? undefined,
          street: parsed.street ?? undefined,
          label: displayAddress(parsed),
          source: 'vision',
          status: result.geo ? 'ready' : 'failed',
          lat: result.geo?.la,
          lon: result.geo?.lo,
          geoSource: result.geo?.src,
          geoPrecision: result.geo?.pr,
          latencyMs: Math.round(result.ms.total),
        });
        Vibration.vibrate(40);
        flash(`Lu par vision : ${displayAddress(parsed)}`);
      } catch (error) {
        flash(`Lecture impossible : ${error instanceof Error ? error.message : 'erreur'}`);
      } finally {
        if (snapshotPath) void unlink(stripFileScheme(snapshotPath)).catch(() => {});
        busyRef.current = false;
        setBusy(false);
      }
    },
    [session, flash],
  );

  // --- OCR : un instantané, lu par ML Kit, toutes les `ocrIntervalMs` ---------------------------------------
  const handleOcrText = useCallback(
    (text: string) => {
      const decision = session.ingest(text, Date.now());
      if (decision.kind === 'submit') submitFast(decision.address);
      else if (decision.kind === 'fallback') void captureFallback(false);
    },
    [session, submitFast, captureFallback],
  );
  const handleOcrTextRef = useRef(handleOcrText);
  handleOcrTextRef.current = handleOcrText;
  const failuresRef = useRef(0);

  // Le livreur zoome sur l'adresse : l'OCR ne doit lire que le cadre jaune, pas l'étiquette d'un colis voisin.
  const guideBox = useMemo(() => guideRect(view), [view]);

  /** Une lecture. Retourne sans bruit si la caméra n'est pas prête ; l'échec d'une image ne coupe pas la boucle. */
  const readLabelOnce = useCallback(async () => {
    const size = viewRef.current;
    if (!camera.current || size.width === 0) return;
    let path: string | undefined;
    try {
      const snapshot = await camera.current.takeSnapshot({ quality: 70 });
      path = snapshot.path;
      const result = await TextRecognition.recognize(`file://${stripFileScheme(path)}`);
      failuresRef.current = 0;
      if (!result.text) return;
      const text = textInGuide(result.blocks, result.text, uprightSize(snapshot.width, snapshot.height), size, guideRect(size));
      if (text.length > 4) handleOcrTextRef.current(text);
    } catch (error) {
      failuresRef.current += 1;
      if (failuresRef.current === OCR_FAILURES_BEFORE_HINT) {
        flash(`Lecture caméra impossible : ${error instanceof Error ? error.message : 'erreur'}`);
      }
    } finally {
      if (path) void unlink(stripFileScheme(path)).catch(() => {});
    }
  }, [flash]);

  const scanning = active && appState === 'active' && view.width > 0 && Boolean(device);
  useEffect(() => {
    if (!scanning) return;
    let stopped = false;
    void (async () => {
      while (!stopped) {
        const startedAt = Date.now();
        // Pas de lecture pendant le chemin lent : les deux utilisent l'instantané de la caméra.
        if (!busyRef.current) await readLabelOnce();
        const pause = config.ocrIntervalMs - (Date.now() - startedAt);
        if (pause > 0) await new Promise<void>((resolve) => setTimeout(resolve, pause));
      }
    })();
    return () => {
      stopped = true;
    };
  }, [scanning, readLabelOnce]);

  const onLayout = useCallback((e: LayoutChangeEvent) => {
    const { width, height } = e.nativeEvent.layout;
    setView({ width, height });
  }, []);

  // --- rendu ------------------------------------------------------------------------------------------
  if (!hasPermission) {
    return (
      <View style={styles.center}>
        <Text style={styles.title}>Accès à la caméra requis</Text>
        <Text style={styles.body}>Le scan des étiquettes se fait sur l'appareil ; aucune photo n'est envoyée en temps normal.</Text>
        <Pressable accessibilityRole="button" style={styles.primary} onPress={() => void requestPermission()}>
          <Text style={styles.primaryText}>Autoriser la caméra</Text>
        </Pressable>
      </View>
    );
  }
  if (!device) {
    return (
      <View style={styles.center}>
        <Text style={styles.title}>Aucune caméra arrière détectée</Text>
      </View>
    );
  }

  const guide = guideBox;
  const recent = parcels.slice(-3).reverse();
  const status = STATUS_LABEL[socketStatus];

  return (
    <View style={styles.fill} onLayout={onLayout}>
      {view.width > 0 ? (
        <Camera
          ref={camera}
          style={StyleSheet.absoluteFill}
          device={device}
          format={format}
          isActive={active && appState === 'active'}
          // takeSnapshot() lit le bitmap de l'aperçu : il est vide avec le mode par défaut « surface-view » sur Android.
          androidPreviewViewType="texture-view"
          video // requis par takeSnapshot() sur iOS
          torch={torch ? 'on' : 'off'}
          zoom={Math.min(device.maxZoom, Math.max(device.minZoom, device.neutralZoom * zoomFactor))}
          enableZoomGesture
        />
      ) : null}

      <View pointerEvents="none" style={[styles.guide, { left: guide.x, top: guide.y, width: guide.width, height: guide.height }]} />

      <Pressable accessibilityRole="button" accessibilityLabel="Réglages" onPress={onOpenSettings} style={styles.gear}>
        <Text style={styles.gearText}>⚙</Text>
      </Pressable>

      <View style={styles.zoomColumn}>
        {[1, 2, 3].map((z) => (
          <Pressable
            key={z}
            accessibilityRole="button"
            accessibilityLabel={`Zoom ${z} fois`}
            onPress={() => setZoomFactor(z)}
            style={[styles.zoomButton, zoomFactor === z && styles.zoomButtonOn]}
          >
            <Text style={[styles.zoomText, zoomFactor === z && styles.zoomTextOn]}>{z}×</Text>
          </Pressable>
        ))}
      </View>

      <View style={styles.topBar} pointerEvents="none">
        <View style={[styles.pill, { backgroundColor: status.color }]}>
          <Text style={styles.pillText}>{status.text}</Text>
        </View>
        <View style={[styles.pill, styles.pillDark]}>
          <Text style={styles.pillText}>{parcels.length} colis</Text>
        </View>
      </View>

      {hint || busy ? (
        <View style={styles.hint} pointerEvents="none">
          {busy ? <ActivityIndicator color="#fff" /> : null}
          <Text style={styles.hintText}>{hint ?? 'Traitement…'}</Text>
        </View>
      ) : (
        <View style={styles.hint} pointerEvents="none">
          <Text style={styles.hintText}>Cadrez l'adresse dans le rectangle</Text>
        </View>
      )}

      <View style={styles.bottom}>
        {recent.map((p) => (
          <ScanRow key={p.id} parcel={p} />
        ))}
        <View style={styles.buttons}>
          <Pressable accessibilityRole="button" accessibilityLabel="Lampe" style={styles.secondary} onPress={() => setTorch((t) => !t)}>
            <Text style={styles.secondaryText}>{torch ? 'Lampe ON' : 'Lampe'}</Text>
          </Pressable>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Étiquette abîmée, lire par vision"
            style={[styles.secondary, busy && styles.disabled]}
            disabled={busy}
            onPress={() => void captureFallback(true)}
          >
            <Text style={styles.secondaryText}>Étiquette abîmée</Text>
          </Pressable>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Voir la tournée, ${parcels.length} colis`}
            style={[styles.primary, styles.grow, parcels.length === 0 && styles.disabled]}
            disabled={parcels.length === 0}
            onPress={onOpenRoute}
          >
            <Text style={styles.primaryText}>Tournée ({parcels.length}) →</Text>
          </Pressable>
        </View>
      </View>
    </View>
  );
}

function ScanRow({ parcel }: { parcel: Parcel }): React.JSX.Element {
  const icon = parcel.status === 'ready' ? (parcel.geoPrecision === 'postcode' || parcel.geoPrecision === 'synthetic' ? '≈' : '✓') : parcel.status === 'pending' ? '…' : '!';
  const color = parcel.status === 'ready' ? '#1faa59' : parcel.status === 'pending' ? '#e0a100' : '#d93025';
  const approximate = parcel.geoPrecision === 'postcode' || parcel.geoPrecision === 'synthetic';
  const detail =
    parcel.status === 'ready' && approximate
      ? `position approximative (${parcel.geoPrecision === 'postcode' ? 'centre du code postal' : 'fictive'})`
      : parcel.status === 'ready'
      ? `${parcel.source === 'vision' ? 'vision' : parcel.geoSource === 'cache' ? 'cache' : parcel.geoSource ?? ''} · ${parcel.latencyMs ?? '?'} ms`
      : parcel.status === 'pending'
        ? 'localisation…'
        : 'à relancer';
  return (
    <View style={styles.row}>
      <Text style={[styles.rowIcon, { color }]}>{icon}</Text>
      <Text style={styles.rowLabel} numberOfLines={1}>
        {parcel.label}
      </Text>
      <Text style={styles.rowDetail}>{detail}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1, backgroundColor: '#000' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 24, backgroundColor: '#111' },
  title: { color: '#fff', fontSize: 22, fontWeight: '700', textAlign: 'center', marginBottom: 12 },
  body: { color: '#ccc', fontSize: 16, textAlign: 'center', marginBottom: 24 },
  guide: { position: 'absolute', borderWidth: 3, borderColor: '#ffd400', borderRadius: 12 },
  topBar: { position: 'absolute', top: 12, left: 12, right: 64, flexDirection: 'row', justifyContent: 'space-between' },
  gear: { position: 'absolute', top: 8, right: 10, width: 48, height: 48, borderRadius: 24, backgroundColor: 'rgba(0,0,0,0.6)', alignItems: 'center', justifyContent: 'center', zIndex: 5 },
  gearText: { color: '#fff', fontSize: 26 },
  zoomColumn: { position: 'absolute', right: 10, top: '34%', gap: 8, zIndex: 5 },
  zoomButton: { width: 48, height: 48, borderRadius: 24, backgroundColor: 'rgba(0,0,0,0.6)', alignItems: 'center', justifyContent: 'center' },
  zoomButtonOn: { backgroundColor: '#ffd400' },
  zoomText: { color: '#fff', fontSize: 17, fontWeight: '800' },
  zoomTextOn: { color: '#111' },
  pill: { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 16 },
  pillDark: { backgroundColor: 'rgba(0,0,0,0.65)' },
  pillText: { color: '#fff', fontSize: 14, fontWeight: '600' },
  hint: { position: 'absolute', top: '15%', alignSelf: 'center', flexDirection: 'row', gap: 8, alignItems: 'center', backgroundColor: 'rgba(0,0,0,0.65)', paddingHorizontal: 14, paddingVertical: 8, borderRadius: 12 },
  hintText: { color: '#fff', fontSize: 16, fontWeight: '600' },
  bottom: { position: 'absolute', left: 0, right: 0, bottom: 0, padding: 12, gap: 8, backgroundColor: 'rgba(0,0,0,0.55)' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  rowIcon: { fontSize: 20, fontWeight: '800', width: 22, textAlign: 'center' },
  rowLabel: { flex: 1, color: '#fff', fontSize: 16 },
  rowDetail: { color: '#bbb', fontSize: 13 },
  buttons: { flexDirection: 'row', gap: 8, alignItems: 'stretch' },
  grow: { flex: 1 },
  primary: { minHeight: 56, paddingHorizontal: 18, borderRadius: 14, backgroundColor: '#ffd400', alignItems: 'center', justifyContent: 'center' },
  primaryText: { color: '#111', fontSize: 18, fontWeight: '800' },
  secondary: { minHeight: 56, paddingHorizontal: 14, borderRadius: 14, backgroundColor: 'rgba(255,255,255,0.18)', alignItems: 'center', justifyContent: 'center' },
  secondaryText: { color: '#fff', fontSize: 15, fontWeight: '700' },
  disabled: { opacity: 0.4 },
});
