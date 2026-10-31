/**
 * Écran livreur : la tournée par stationnements (ParkingClusters).
 *
 *   « Garez le van ici »  ->  « Livrez ces N portes à pied »  ->  « Reprenez le van »
 *
 * Gros boutons, un tap pour valider un colis, état conservé si l'app est fermée (AsyncStorage).
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Linking,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  Vibration,
  View,
  useColorScheme,
} from 'react-native';

import { LocationError, MY_POSITION_LABEL, currentPosition } from './location';
import { mapsLegUrls } from './mapsLinks';
import { ROUTE_STORAGE_KEY, confirmClearParcels } from './newTour';
import { ApiError, optimizeRoute } from './routeApi';
import { parcelSignature } from './routeBody';
import {
  EMPTY_PROGRESS,
  activeClusterIndex,
  clusterAllDelivered,
  clusterDelivered,
  completeCluster,
  counts,
  formatDuration,
  toggleDelivery,
  type Progress,
} from './routeProgress';
import type { Settings } from './settings';
import { parcelStore, settingsStore, useParcels, useSettings } from './stores';
import type { ParkingCluster, RouteResponse } from './types';

type Load =
  | { state: 'loading' }
  | { state: 'ready'; route: RouteResponse }
  | { state: 'error'; message: string };

interface Saved {
  signature: string;
  route: RouteResponse;
  progress: Progress;
}

function palette(dark: boolean) {
  return dark
    ? { bg: '#0d0f12', card: '#1b1f25', text: '#f4f6f8', sub: '#aab2bd', accent: '#ffd400', onAccent: '#111', ok: '#2fbf71', line: '#2b313a', warn: '#ff8a65' }
    : { bg: '#f1f3f6', card: '#ffffff', text: '#12161b', sub: '#59626e', accent: '#ffd400', onAccent: '#111', ok: '#128a4a', line: '#d9dee5', warn: '#c2410c' };
}

function openNavigation(lat: number, lon: number): void {
  const native = Platform.select({
    ios: `http://maps.apple.com/?daddr=${lat},${lon}&dirflg=d`,
    default: `google.navigation:q=${lat},${lon}&mode=d`,
  });
  Linking.openURL(native).catch(() =>
    Linking.openURL(`https://www.google.com/maps/dir/?api=1&destination=${lat},${lon}&travelmode=driving`),
  );
}

function openUrl(url: string): void {
  Linking.openURL(url).catch(() => undefined);
}

export interface DriverRouteScreenProps {
  onBack: () => void;
}

export function DriverRouteScreen({ onBack }: DriverRouteScreenProps): React.JSX.Element {
  const c = palette(useColorScheme() === 'dark');
  const parcels = useParcels();
  const { startFromGps, returnToDepot } = useSettings();
  const signature = useMemo(() => parcelSignature(parcels), [parcels]);
  const [load, setLoad] = useState<Load>({ state: 'loading' });
  const [progress, setProgress] = useState<Progress>(EMPTY_PROGRESS);
  const listRef = useRef<FlatList<ParkingCluster>>(null);

  const compute = useCallback(async () => {
    setLoad({ state: 'loading' });
    try {
      const settings = settingsStore.getSnapshot();
      const origin = settings.startFromGps ? { ...(await currentPosition()), label: MY_POSITION_LABEL } : settings.depot;
      const route = await optimizeRoute(parcels, origin, settings.returnToDepot);
      setProgress(EMPTY_PROGRESS);
      setLoad({ state: 'ready', route });
    } catch (error) {
      const known = error instanceof ApiError || error instanceof LocationError;
      setLoad({ state: 'error', message: known ? error.message : 'erreur inattendue' });
    }
  }, [parcels]);

  // Reprise d'une tournée enregistrée si la liste de colis n'a pas changé, sinon nouveau calcul.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(ROUTE_STORAGE_KEY);
        const saved = raw ? (JSON.parse(raw) as Saved) : null;
        if (!cancelled && saved && saved.signature === signature) {
          setProgress(saved.progress);
          setLoad({ state: 'ready', route: saved.route });
          return;
        }
      } catch {
        /* enregistrement illisible : on recalcule */
      }
      if (!cancelled) await compute();
    })();
    return () => {
      cancelled = true;
    };
    // Un seul passage à l'ouverture de l'écran : la liste de colis est figée pendant la tournée.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (load.state !== 'ready') return;
    const timer = setTimeout(() => {
      const saved: Saved = { signature, route: load.route, progress };
      AsyncStorage.setItem(ROUTE_STORAGE_KEY, JSON.stringify(saved)).catch(() => undefined);
    }, 250);
    return () => clearTimeout(timer);
  }, [load, progress, signature]);

  const route = load.state === 'ready' ? load.route : null;
  const active = route ? activeClusterIndex(route, progress) : -1;

  // Tous les stationnements restants dans l'ordre, puis le retour au départ si la tournée est fermée : un seul lien
  // Google Maps (ou quelques-uns au-delà de 10 étapes).
  const remaining = route ? route.plan.clusters.filter((cl) => !progress.completed[cl.index]).length : 0;
  const returnsHome = route?.return_to_depot !== false;
  const legUrls = useMemo(() => {
    if (!route || remaining === 0) return [];
    const parks = route.plan.clusters.filter((cl) => !progress.completed[cl.index]).map((cl) => cl.park);
    return mapsLegUrls(returnsHome ? [...parks, route.depot] : parks);
  }, [route, progress, remaining, returnsHome]);

  // Changer le départ ou le retour recalcule la tournée ; les colis déjà cochés seraient perdus, d'où la confirmation.
  const hasProgress = route ? counts(route, progress).done > 0 : false;
  const changeOption = useCallback(
    (patch: Partial<Pick<Settings, 'startFromGps' | 'returnToDepot'>>) => {
      const apply = () => {
        settingsStore.save({ ...settingsStore.getSnapshot(), ...patch }).catch(() => undefined);
        void compute();
      };
      if (!hasProgress) return apply();
      Alert.alert('Recalculer la tournée ?', 'Les colis déjà cochés seront décochés.', [
        { text: 'Annuler', style: 'cancel' },
        { text: 'Recalculer', onPress: apply },
      ]);
    },
    [compute, hasProgress],
  );

  useEffect(() => {
    if (active < 0) return;
    const timer = setTimeout(() => {
      try {
        listRef.current?.scrollToIndex({ index: active, animated: true, viewPosition: 0.02 });
      } catch {
        /* la liste n'est pas encore mesurée : onScrollToIndexFailed prend le relais */
      }
    }, 150);
    return () => clearTimeout(timer);
  }, [active]);

  const onToggle = useCallback(
    (stopId: string) => {
      if (!route) return;
      setProgress((p) => {
        const next = toggleDelivery(route, p, stopId);
        if (next.delivered[stopId]) Vibration.vibrate(15);
        return next;
      });
    },
    [route],
  );

  const onComplete = useCallback(
    (index: number) => {
      if (!route) return;
      Vibration.vibrate(30);
      setProgress((p) => completeCluster(route, p, index));
    },
    [route],
  );

  const removeUnresolved = useCallback(
    (ids: string[]) => {
      ids.forEach((id) => parcelStore.remove(id));
      setTimeout(() => void compute(), 0);
    },
    [compute],
  );

  // ----------------------------------------------------------------------------------------------
  const header = (
    <View style={[styles.header, { backgroundColor: c.card, borderColor: c.line }]}>
      <Pressable accessibilityRole="button" accessibilityLabel="Retour au scan" onPress={onBack} style={styles.back}>
        <Text style={[styles.backText, { color: c.text }]}>← Scan</Text>
      </Pressable>
      {route ? <Summary route={route} progress={progress} colors={c} /> : null}
      <TourOptions
        startFromGps={startFromGps}
        returnToDepot={returnToDepot}
        disabled={load.state === 'loading'}
        onChange={changeOption}
        colors={c}
      />
      {legUrls.length > 0 ? <TourLauncher urls={legUrls} stops={remaining} returns={returnsHome} colors={c} /> : null}
    </View>
  );

  if (load.state === 'loading') {
    return (
      <View style={[styles.fill, { backgroundColor: c.bg }]}>
        {header}
        <View style={styles.center}>
          <ActivityIndicator size="large" color={c.accent} />
          <Text style={[styles.body, { color: c.sub }]}>Calcul de la tournée…</Text>
        </View>
      </View>
    );
  }

  if (load.state === 'error') {
    return (
      <View style={[styles.fill, { backgroundColor: c.bg }]}>
        {header}
        <View style={styles.center}>
          <Text style={[styles.title, { color: c.text }]}>Tournée indisponible</Text>
          <Text style={[styles.body, { color: c.warn }]}>{load.message}</Text>
          <Pressable accessibilityRole="button" onPress={() => void compute()} style={[styles.bigButton, { backgroundColor: c.accent }]}>
            <Text style={[styles.bigButtonText, { color: c.onAccent }]}>Réessayer</Text>
          </Pressable>
        </View>
      </View>
    );
  }

  const done = active < 0;
  const home = load.route.depot;
  const startedHere = home.label === MY_POSITION_LABEL;
  return (
    <View style={[styles.fill, { backgroundColor: c.bg }]}>
      {header}
      <FlatList
        ref={listRef}
        data={load.route.plan.clusters}
        extraData={progress}
        keyExtractor={(cl) => String(cl.index)}
        contentContainerStyle={styles.list}
        onScrollToIndexFailed={(info) =>
          listRef.current?.scrollToOffset({ offset: info.averageItemLength * info.index, animated: true })
        }
        ListHeaderComponent={
          <>
            {(load.route.geocode?.approximate ?? 0) > 0 ? (
              <View style={[styles.warnBox, { borderColor: c.accent, backgroundColor: c.card }]}>
                <Text style={[styles.warnTitle, { color: c.text }]}>
                  {load.route.geocode!.approximate} position(s) approximative(s)
                </Text>
                <Text style={[styles.body, { color: c.sub }]}>
                  Ces colis sont placés au centre de leur code postal, pas devant la porte. L'ordre de la tournée reste
                  utile, mais la marche entre portes n'est pas fiable. Une clé d'adresses précises est à renseigner sur le serveur.
                </Text>
              </View>
            ) : null}
          {load.route.unresolved.length > 0 ? (
            <View style={[styles.warnBox, { borderColor: c.warn, backgroundColor: c.card }]}>
              <Text style={[styles.warnTitle, { color: c.warn }]}>
                {load.route.unresolved.length} colis non localisé(s), exclus de la tournée
              </Text>
              <Pressable
                accessibilityRole="button"
                onPress={() => removeUnresolved(load.route.unresolved)}
                style={[styles.smallButton, { borderColor: c.warn }]}
              >
                <Text style={{ color: c.warn, fontWeight: '700', fontSize: 16 }}>Retirer de la liste et recalculer</Text>
              </Pressable>
            </View>
          ) : null}
          </>
        }
        renderItem={({ item }) => (
          <ClusterCard
            cluster={item}
            state={progress.completed[item.index] ? 'done' : item.index === active ? 'active' : 'upcoming'}
            progress={progress}
            colors={c}
            onToggle={onToggle}
            onComplete={onComplete}
          />
        )}
        ListFooterComponent={
          <View style={[styles.card, { backgroundColor: c.card, borderColor: done ? c.ok : c.line }]}>
            {returnsHome ? (
              <>
                <Text style={[styles.cardTitle, { color: c.text }]}>
                  {done ? 'Tournée terminée' : 'Puis'} : {startedHere ? 'retour à votre point de départ' : 'retour au dépôt'}
                </Text>
                <Text style={[styles.body, { color: c.sub }]}>
                  {startedHere ? 'Point de départ' : home.label} · {formatDuration(load.route.plan.drive_back_s)} de route
                </Text>
                <Pressable
                  accessibilityRole="button"
                  onPress={() => openNavigation(home.lat, home.lon)}
                  style={[styles.smallButton, { borderColor: c.line }]}
                >
                  <Text style={{ color: c.text, fontWeight: '700', fontSize: 16 }}>
                    {startedHere ? 'Naviguer vers le point de départ' : 'Naviguer vers le dépôt'}
                  </Text>
                </Pressable>
              </>
            ) : (
              <Text style={[styles.cardTitle, { color: c.text }]}>
                {done ? 'Tournée terminée' : 'La tournée se termine au dernier arrêt'}
              </Text>
            )}
            <Pressable accessibilityRole="button" onPress={() => void compute()} style={styles.link}>
              <Text style={{ color: c.sub, fontSize: 14 }}>Recalculer la tournée</Text>
            </Pressable>
            <Pressable accessibilityRole="button" onPress={() => confirmClearParcels(onBack)} style={styles.link}>
              <Text style={{ color: c.warn, fontSize: 15, fontWeight: '700' }}>Nouvelle tournée (vider la liste)</Text>
            </Pressable>
          </View>
        }
      />
    </View>
  );
}

// =============================================================================================
function Summary({ route, progress, colors: c }: { route: RouteResponse; progress: Progress; colors: ReturnType<typeof palette> }) {
  const { done, total } = counts(route, progress);
  const t = route.plan.totals;
  const saved = route.comparison.saved_s;
  return (
    <View style={styles.summary}>
      <Text style={[styles.summaryMain, { color: c.text }]}>
        {done}/{total} colis · {t.van_stops} arrêts · {formatDuration(t.total_s)}
      </Text>
      <View style={[styles.bar, { backgroundColor: c.line }]}>
        <View style={[styles.barFill, { backgroundColor: c.ok, width: `${total ? (100 * done) / total : 0}%` }]} />
      </View>
      {saved >= 1 ? (
        <Text style={[styles.summarySub, { color: c.ok }]}>
          Gain vs GPS classique : {formatDuration(saved)} ({route.comparison.saved_pct.toFixed(1)} %)
        </Text>
      ) : null}
    </View>
  );
}

/** Deux choix qui changent le calcul : d'où part la tournée, et si elle revient au départ. */
function TourOptions({
  startFromGps,
  returnToDepot,
  disabled,
  onChange,
  colors: c,
}: {
  startFromGps: boolean;
  returnToDepot: boolean;
  disabled: boolean;
  onChange: (patch: Partial<Pick<Settings, 'startFromGps' | 'returnToDepot'>>) => void;
  colors: ReturnType<typeof palette>;
}) {
  const start = startFromGps ? 'ma position' : 'dépôt';
  const back = returnToDepot ? 'oui' : 'non';
  return (
    <View style={styles.options}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Départ : ${start}. Appuyer pour changer.`}
        disabled={disabled}
        onPress={() => onChange({ startFromGps: !startFromGps })}
        style={[styles.chip, { borderColor: c.line }, disabled && styles.disabled]}
      >
        <Text style={[styles.chipText, { color: c.text }]}>Départ : {start}</Text>
      </Pressable>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Retour au départ : ${back}. Appuyer pour changer.`}
        disabled={disabled}
        onPress={() => onChange({ returnToDepot: !returnToDepot })}
        style={[styles.chip, { borderColor: c.line }, disabled && styles.disabled]}
      >
        <Text style={[styles.chipText, { color: c.text }]}>Retour au départ : {back}</Text>
      </Pressable>
    </View>
  );
}

/** « Lancer tous les arrêts » : un tap ouvre Google Maps avec la suite des arrêts (et le retour au départ si la tournée est fermée). */
function TourLauncher({
  urls,
  stops,
  returns,
  colors: c,
}: {
  urls: string[];
  stops: number;
  returns: boolean;
  colors: ReturnType<typeof palette>;
}) {
  const [only] = urls;
  if (urls.length === 1 && only) {
    return (
      <View style={styles.launcher}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`Lancer les ${stops} arrêts restants dans Google Maps`}
          onPress={() => openUrl(only)}
          style={[styles.launchButton, { backgroundColor: c.accent }]}
        >
          <Text style={[styles.launchText, { color: c.onAccent }]}>
            Lancer {stops > 1 ? `les ${stops} arrêts` : 'le dernier arrêt'} dans Google Maps
          </Text>
        </Pressable>
        <Text style={[styles.launchNote, { color: c.sub }]}>
          {returns ? "Dans l'ordre, puis retour au départ." : "Dans l'ordre, sans retour."} Appuyez sur « Démarrer » dans Maps.
        </Text>
      </View>
    );
  }
  return (
    <View style={styles.launcher}>
      <Text style={[styles.launchNote, { color: c.text }]}>
        {stops} arrêts : Google Maps en accepte 10 à la fois. Lancez la partie 1, puis la suivante quand elle est finie.
      </Text>
      <View style={styles.launchRow}>
        {urls.map((url, i) => (
          <Pressable
            key={url}
            accessibilityRole="button"
            accessibilityLabel={`Lancer la partie ${i + 1} sur ${urls.length} dans Google Maps`}
            onPress={() => openUrl(url)}
            style={[styles.launchButton, styles.grow, { backgroundColor: c.accent }]}
          >
            <Text style={[styles.launchText, { color: c.onAccent }]}>
              Partie {i + 1}/{urls.length}
            </Text>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

interface CardProps {
  cluster: ParkingCluster;
  state: 'done' | 'active' | 'upcoming';
  progress: Progress;
  colors: ReturnType<typeof palette>;
  onToggle: (stopId: string) => void;
  onComplete: (index: number) => void;
}

const ClusterCard = React.memo(function ClusterCard({ cluster, state, progress, colors: c, onToggle, onComplete }: CardProps) {
  const n = cluster.deliveries.length;
  const walkLoop = cluster.kind === 'WALK_LOOP';
  const delivered = clusterDelivered(cluster, progress);

  if (state === 'done') {
    return (
      <View style={[styles.doneRow, { backgroundColor: c.card, borderColor: c.line }]}>
        <Text style={[styles.doneIcon, { color: c.ok }]}>✓</Text>
        <Text style={[styles.doneText, { color: c.sub }]} numberOfLines={1}>
          {cluster.index + 1}. {cluster.park.label}
          {walkLoop ? ` · ${n} portes` : ''}
        </Text>
      </View>
    );
  }

  const upcoming = state === 'upcoming';
  return (
    <View
      style={[
        styles.card,
        { backgroundColor: c.card, borderColor: state === 'active' ? c.accent : c.line, opacity: upcoming ? 0.72 : 1 },
        state === 'active' && styles.cardActive,
      ]}
    >
      <View style={styles.stepRow}>
        <View style={[styles.badge, { backgroundColor: c.accent }]}>
          <Text style={[styles.badgeText, { color: c.onAccent }]}>{cluster.index + 1}</Text>
        </View>
        <View style={styles.grow}>
          <Text style={[styles.cardTitle, { color: c.text }]}>Garez le van ici</Text>
          <Text style={[styles.place, { color: c.text }]}>{cluster.park.label}</Text>
          <Text style={[styles.body, { color: c.sub }]}>Conduite {formatDuration(cluster.drive_s)}</Text>
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`Naviguer vers ${cluster.park.label}`}
          onPress={() => openNavigation(cluster.park.lat, cluster.park.lon)}
          style={[styles.nav, { borderColor: c.line }]}
        >
          <Text style={{ color: c.text, fontWeight: '700', fontSize: 15 }}>Naviguer</Text>
        </Pressable>
      </View>

      <Text style={[styles.sectionTitle, { color: c.sub }]}>
        {walkLoop ? `Livrez ces ${n} portes à pied` : 'Livrez ce colis'}
        {n > 1 ? ` (${delivered}/${n})` : ''}
      </Text>

      {cluster.deliveries.map((d, k) => {
        const isDone = !!progress.delivered[d.stop_id];
        return (
          <Pressable
            key={d.stop_id}
            accessibilityRole="checkbox"
            accessibilityState={{ checked: isDone }}
            accessibilityLabel={`${d.label}, ${isDone ? 'remis' : 'à remettre'}`}
            android_ripple={{ color: c.line }}
            onPress={() => onToggle(d.stop_id)}
            style={[styles.door, { borderColor: isDone ? c.ok : c.line, backgroundColor: isDone ? 'rgba(47,191,113,0.12)' : 'transparent' }]}
          >
            <View style={[styles.check, { borderColor: isDone ? c.ok : c.sub, backgroundColor: isDone ? c.ok : 'transparent' }]}>
              <Text style={styles.checkText}>{isDone ? '✓' : ''}</Text>
            </View>
            <View style={styles.grow}>
              <Text style={[styles.doorLabel, { color: c.text }]}>{d.label}</Text>
              <Text style={[styles.body, { color: c.sub }]}>
                {k === 0 && !walkLoop ? 'Devant le van' : k === 0 ? 'Devant le van' : `+${formatDuration(d.walk_s)} à pied`}
              </Text>
            </View>
          </Pressable>
        );
      })}

      {walkLoop ? (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Reprenez le van"
          disabled={!clusterAllDelivered(cluster, progress)}
          onPress={() => onComplete(cluster.index)}
          style={[
            styles.bigButton,
            { backgroundColor: c.accent },
            !clusterAllDelivered(cluster, progress) && styles.disabled,
          ]}
        >
          <Text style={[styles.bigButtonText, { color: c.onAccent }]}>
            Reprenez le van · retour à pied {formatDuration(cluster.return_walk_s)}
          </Text>
        </Pressable>
      ) : null}
    </View>
  );
});

const styles = StyleSheet.create({
  fill: { flex: 1 },
  grow: { flex: 1 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 24, gap: 12 },
  header: { paddingHorizontal: 14, paddingTop: 8, paddingBottom: 10, borderBottomWidth: StyleSheet.hairlineWidth },
  back: { minHeight: 44, justifyContent: 'center', alignSelf: 'flex-start' },
  backText: { fontSize: 17, fontWeight: '700' },
  summary: { gap: 6 },
  summaryMain: { fontSize: 18, fontWeight: '800' },
  summarySub: { fontSize: 14, fontWeight: '600' },
  bar: { height: 8, borderRadius: 4, overflow: 'hidden' },
  barFill: { height: 8, borderRadius: 4 },
  list: { padding: 12, gap: 12 },
  card: { borderWidth: 2, borderRadius: 18, padding: 14, gap: 10 },
  cardActive: { shadowColor: '#000', shadowOpacity: 0.18, shadowRadius: 10, shadowOffset: { width: 0, height: 4 }, elevation: 5 },
  stepRow: { flexDirection: 'row', gap: 12, alignItems: 'flex-start' },
  badge: { width: 40, height: 40, borderRadius: 20, alignItems: 'center', justifyContent: 'center' },
  badgeText: { fontSize: 20, fontWeight: '900' },
  cardTitle: { fontSize: 20, fontWeight: '800' },
  place: { fontSize: 18, fontWeight: '600', marginTop: 2 },
  sectionTitle: { fontSize: 15, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.4, marginTop: 4 },
  body: { fontSize: 15 },
  title: { fontSize: 22, fontWeight: '800', textAlign: 'center' },
  nav: { minHeight: 44, paddingHorizontal: 12, borderWidth: 2, borderRadius: 12, alignItems: 'center', justifyContent: 'center' },
  door: { minHeight: 64, borderWidth: 2, borderRadius: 14, paddingHorizontal: 12, paddingVertical: 8, flexDirection: 'row', alignItems: 'center', gap: 12 },
  check: { width: 34, height: 34, borderRadius: 17, borderWidth: 3, alignItems: 'center', justifyContent: 'center' },
  checkText: { color: '#fff', fontSize: 20, fontWeight: '900' },
  doorLabel: { fontSize: 18, fontWeight: '700' },
  bigButton: { minHeight: 60, borderRadius: 16, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 16, marginTop: 4 },
  bigButtonText: { fontSize: 18, fontWeight: '900', textAlign: 'center' },
  disabled: { opacity: 0.35 },
  doneRow: { flexDirection: 'row', alignItems: 'center', gap: 10, borderWidth: 1, borderRadius: 12, paddingHorizontal: 12, minHeight: 44 },
  doneIcon: { fontSize: 18, fontWeight: '900' },
  doneText: { flex: 1, fontSize: 15 },
  warnBox: { borderWidth: 2, borderRadius: 14, padding: 12, gap: 8, marginBottom: 4 },
  warnTitle: { fontSize: 16, fontWeight: '800' },
  smallButton: { minHeight: 48, borderWidth: 2, borderRadius: 12, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 12 },
  link: { minHeight: 44, alignItems: 'center', justifyContent: 'center' },
  options: { flexDirection: 'row', gap: 8, marginTop: 10 },
  chip: { flex: 1, minHeight: 44, borderWidth: 2, borderRadius: 12, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 6 },
  chipText: { fontSize: 14, fontWeight: '700', textAlign: 'center' },
  launcher: { gap: 6, marginTop: 10 },
  launchRow: { flexDirection: 'row', gap: 8 },
  launchButton: { minHeight: 52, borderRadius: 14, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 14 },
  launchText: { fontSize: 17, fontWeight: '900', textAlign: 'center' },
  launchNote: { fontSize: 13 },
});
