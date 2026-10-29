/**
 * Réglages : adresse du serveur, clé API, dépôt. Enregistrés sur le téléphone (jamais dans le code).
 * « Tester la connexion » vérifie le serveur ET la clé avant d'enregistrer.
 */
import React, { useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';

import { checkServer, type ServerCheck } from './routeApi';
import { normalizeServerUrl, parseCoordinate, type Settings } from './settings';
import { settingsStore, useSettings } from './stores';

export interface SettingsScreenProps {
  /** Premier lancement : pas de bouton Retour, tant que le serveur n'est pas renseigné. */
  firstRun: boolean;
  onClose: () => void;
}

export function SettingsScreen({ firstRun, onClose }: SettingsScreenProps): React.JSX.Element {
  const saved = useSettings();
  const [serverUrl, setServerUrl] = useState(saved.serverUrl);
  const [apiKey, setApiKey] = useState(saved.apiKey);
  const [showKey, setShowKey] = useState(false);
  const [depotLabel, setDepotLabel] = useState(saved.depot.label);
  const [depotLat, setDepotLat] = useState(String(saved.depot.lat));
  const [depotLon, setDepotLon] = useState(String(saved.depot.lon));
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<ServerCheck | null>(null);
  const [error, setError] = useState<string | null>(null);

  /** Valide les champs ; retourne les réglages prêts à enregistrer, ou null en affichant l'erreur. */
  const validate = (): Settings | null => {
    const url = normalizeServerUrl(serverUrl);
    if (!url.ok) {
      setError(url.error);
      return null;
    }
    const lat = parseCoordinate(depotLat, 49.8, 60.95);
    const lon = parseCoordinate(depotLon, -8.7, 1.85);
    if (lat === null || lon === null) {
      setError('Dépôt : latitude entre 49,8 et 60,95 et longitude entre -8,7 et 1,85 (Royaume-Uni)');
      return null;
    }
    setError(null);
    return { serverUrl: url.url, apiKey: apiKey.trim(), depot: { lat, lon, label: depotLabel.trim() || 'Dépôt' } };
  };

  const test = async () => {
    const next = validate();
    if (!next) return;
    setServerUrl(next.serverUrl);
    setChecking(true);
    setResult(null);
    setResult(await checkServer(next.serverUrl, next.apiKey));
    setChecking(false);
  };

  const save = async () => {
    const next = validate();
    if (!next) return;
    await settingsStore.save(next);
    onClose();
  };

  return (
    <ScrollView style={styles.root} contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
      <Text style={styles.title}>{firstRun ? 'Bienvenue : connectez l\'app à votre serveur' : 'Réglages'}</Text>
      {firstRun ? (
        <Text style={styles.help}>
          Collez l'adresse du serveur (donnée par votre hébergeur) et la clé API. Ces informations restent sur ce
          téléphone.
        </Text>
      ) : null}

      <Text style={styles.label}>Adresse du serveur</Text>
      <TextInput
        style={styles.input}
        value={serverUrl}
        onChangeText={setServerUrl}
        placeholder="https://mon-serveur.onrender.com"
        placeholderTextColor="#778"
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="url"
        accessibilityLabel="Adresse du serveur"
      />

      <Text style={styles.label}>Clé API</Text>
      <View style={styles.row}>
        <TextInput
          style={[styles.input, styles.grow]}
          value={apiKey}
          onChangeText={setApiKey}
          placeholder="(laisser vide si le serveur est ouvert)"
          placeholderTextColor="#778"
          autoCapitalize="none"
          autoCorrect={false}
          secureTextEntry={!showKey}
          accessibilityLabel="Clé API"
        />
        <Pressable accessibilityRole="button" onPress={() => setShowKey((v) => !v)} style={styles.small}>
          <Text style={styles.smallText}>{showKey ? 'Masquer' : 'Voir'}</Text>
        </Pressable>
      </View>

      <Text style={styles.label}>Dépôt (départ et retour du van)</Text>
      <TextInput style={styles.input} value={depotLabel} onChangeText={setDepotLabel} placeholder="Nom du dépôt" placeholderTextColor="#778" />
      <View style={styles.row}>
        <TextInput style={[styles.input, styles.grow]} value={depotLat} onChangeText={setDepotLat} keyboardType="numbers-and-punctuation" placeholder="Latitude" placeholderTextColor="#778" accessibilityLabel="Latitude du dépôt" />
        <TextInput style={[styles.input, styles.grow]} value={depotLon} onChangeText={setDepotLon} keyboardType="numbers-and-punctuation" placeholder="Longitude" placeholderTextColor="#778" accessibilityLabel="Longitude du dépôt" />
      </View>

      {error ? <Text style={styles.error}>{error}</Text> : null}

      <Pressable accessibilityRole="button" onPress={() => void test()} disabled={checking} style={[styles.secondary, checking && styles.disabled]}>
        {checking ? <ActivityIndicator color="#fff" /> : <Text style={styles.secondaryText}>Tester la connexion</Text>}
      </Pressable>

      {result ? <CheckResult check={result} /> : null}

      <Pressable accessibilityRole="button" onPress={() => void save()} style={styles.primary}>
        <Text style={styles.primaryText}>Enregistrer</Text>
      </Pressable>
      {!firstRun ? (
        <Pressable accessibilityRole="button" onPress={onClose} style={styles.link}>
          <Text style={styles.linkText}>Annuler</Text>
        </Pressable>
      ) : null}
    </ScrollView>
  );
}

function CheckResult({ check }: { check: ServerCheck }): React.JSX.Element {
  const ok = check.reachable && check.keyAccepted !== false;
  const lines: string[] = [];
  if (!check.reachable) lines.push(`Serveur injoignable : ${check.error ?? 'erreur'}`);
  else {
    lines.push(`Serveur joint en ${check.ms} ms`);
    lines.push(check.keyAccepted === false ? 'Clé API REFUSÉE' : 'Clé API acceptée');
    lines.push(
      check.providers.length > 0
        ? `Adresses : ${check.providers.join(', ')}`
        : 'Adresses : aucun fournisseur configuré sur le serveur',
    );
    if (check.providers.includes('synthetic')) lines.push('Attention : positions FICTIVES (mode test)');
    else if (!check.providers.some((p) => ['ideal_postcodes', 'os_places', 'google_rooftop'].includes(p))) {
      lines.push('Positions approximatives (centre du code postal) : pas de clé d\'adresses précises');
    }
    lines.push(check.vision ? 'Lecture des étiquettes abîmées : activée' : 'Lecture des étiquettes abîmées : désactivée (pas de clé Gemini)');
  }
  return (
    <View style={[styles.result, { borderColor: ok ? '#1faa59' : '#d93025' }]}>
      {lines.map((l) => (
        <Text key={l} style={styles.resultLine}>
          {l}
        </Text>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#101216' },
  content: { padding: 18, gap: 8 },
  title: { color: '#fff', fontSize: 24, fontWeight: '800', marginBottom: 4 },
  help: { color: '#b9c0cb', fontSize: 16, marginBottom: 8 },
  label: { color: '#dfe5ee', fontSize: 16, fontWeight: '700', marginTop: 10 },
  input: { minHeight: 52, borderRadius: 12, borderWidth: 2, borderColor: '#2f3642', backgroundColor: '#1a1e25', color: '#fff', fontSize: 17, paddingHorizontal: 12 },
  row: { flexDirection: 'row', gap: 8, alignItems: 'center' },
  grow: { flex: 1 },
  small: { minHeight: 52, paddingHorizontal: 14, borderRadius: 12, backgroundColor: '#2a303b', alignItems: 'center', justifyContent: 'center' },
  smallText: { color: '#fff', fontWeight: '700', fontSize: 15 },
  error: { color: '#ff8a80', fontSize: 16, fontWeight: '600', marginTop: 8 },
  primary: { minHeight: 58, borderRadius: 14, backgroundColor: '#ffd400', alignItems: 'center', justifyContent: 'center', marginTop: 16 },
  primaryText: { color: '#111', fontSize: 19, fontWeight: '900' },
  secondary: { minHeight: 54, borderRadius: 14, backgroundColor: '#2a303b', alignItems: 'center', justifyContent: 'center', marginTop: 14 },
  secondaryText: { color: '#fff', fontSize: 17, fontWeight: '800' },
  disabled: { opacity: 0.5 },
  link: { minHeight: 48, alignItems: 'center', justifyContent: 'center' },
  linkText: { color: '#b9c0cb', fontSize: 16 },
  result: { borderWidth: 2, borderRadius: 12, padding: 12, gap: 4, marginTop: 10, backgroundColor: '#1a1e25' },
  resultLine: { color: '#e8edf5', fontSize: 15 },
});
