import React, { useEffect, useMemo, useState } from 'react';
import { ActivityIndicator, SafeAreaView, StatusBar, StyleSheet, View } from 'react-native';

import { DriverRouteScreen } from './src/DriverRouteScreen';
import { SettingsScreen } from './src/SettingsScreen';
import { UKFastScanner } from './src/UKFastScanner';
import { ScanSocket, type WebSocketLike } from './src/scanSocket';
import { isConfigured, webSocketUrl } from './src/settings';
import { parcelStore, settingsStore, useSettings } from './src/stores';

type Screen = 'scan' | 'route' | 'settings';

export default function App(): React.JSX.Element {
  const [screen, setScreen] = useState<Screen>('scan');
  const [booted, setBooted] = useState(false);
  const settings = useSettings();

  // Une seule connexion WebSocket pour toute la session : pas de handshake TCP/TLS à chaque colis.
  const socket = useMemo(
    () =>
      new ScanSocket({
        createSocket: (url) => new WebSocket(url) as unknown as WebSocketLike,
        url: () => webSocketUrl(settingsStore.getSnapshot()),
      }),
    [],
  );

  useEffect(() => {
    void (async () => {
      await Promise.all([settingsStore.load(), parcelStore.hydrate()]);
      setBooted(true);
    })();
  }, []);

  // (Re)connexion quand le serveur ou la clé changent ; rien tant que le serveur n'est pas renseigné.
  useEffect(() => {
    if (!booted) return;
    socket.stop();
    if (isConfigured(settings)) socket.start();
    return () => socket.stop();
  }, [booted, settings.serverUrl, settings.apiKey, socket, settings]);

  if (!booted) {
    return (
      <View style={[styles.root, styles.center]}>
        <ActivityIndicator size="large" color="#ffd400" />
      </View>
    );
  }

  const configured = isConfigured(settings);
  return (
    <SafeAreaView style={styles.root}>
      <StatusBar barStyle="light-content" backgroundColor="#000" />
      {!configured || screen === 'settings' ? (
        <SettingsScreen firstRun={!configured} onClose={() => setScreen('scan')} />
      ) : (
        <>
          {/* Le scanner reste monté : la caméra et la connexion survivent à l'aller-retour vers la tournée. */}
          <View style={[styles.fill, screen !== 'scan' && styles.hidden]}>
            <UKFastScanner
              socket={socket}
              active={screen === 'scan'}
              onOpenRoute={() => setScreen('route')}
              onOpenSettings={() => setScreen('settings')}
            />
          </View>
          {screen === 'route' ? <DriverRouteScreen onBack={() => setScreen('scan')} /> : null}
        </>
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#000' },
  center: { alignItems: 'center', justifyContent: 'center' },
  fill: { flex: 1 },
  hidden: { display: 'none' },
});
