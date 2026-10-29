import React, { useEffect, useMemo, useState } from 'react';
import { SafeAreaView, StatusBar, StyleSheet, View } from 'react-native';

import { DriverRouteScreen } from './src/DriverRouteScreen';
import { UKFastScanner } from './src/UKFastScanner';
import { wsUrl } from './src/config';
import { ScanSocket, type WebSocketLike } from './src/scanSocket';
import { parcelStore } from './src/stores';

export default function App(): React.JSX.Element {
  const [screen, setScreen] = useState<'scan' | 'route'>('scan');

  // Une seule connexion WebSocket pour toute la session : pas de handshake TCP/TLS à chaque colis.
  const socket = useMemo(
    () => new ScanSocket({ createSocket: (url) => new WebSocket(url) as unknown as WebSocketLike, url: wsUrl }),
    [],
  );

  useEffect(() => {
    void parcelStore.hydrate();
    socket.start();
    return () => socket.stop();
  }, [socket]);

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar barStyle="light-content" backgroundColor="#000" />
      {/* Le scanner reste monté : la caméra et la connexion survivent à l'aller-retour vers la tournée. */}
      <View style={[styles.fill, screen !== 'scan' && styles.hidden]}>
        <UKFastScanner socket={socket} active={screen === 'scan'} onOpenRoute={() => setScreen('route')} />
      </View>
      {screen === 'route' ? <DriverRouteScreen onBack={() => setScreen('scan')} /> : null}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#000' },
  fill: { flex: 1 },
  hidden: { display: 'none' },
});
