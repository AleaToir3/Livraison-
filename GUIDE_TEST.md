# Guide du premier test (Android)

Chaque étape dit **qui** agit : 👤 vous, 🤖 moi.

## A. Le serveur (une fois, ~10 min)

1. 👤 Créez un compte sur **render.com** (connexion avec GitHub).
2. 👤 Render → **New** → **Blueprint** → choisissez le dépôt `Livraison-` et la branche `claude/uk-delivery-route-optimizer-ginh31`.
   Render lit le fichier `render.yaml` et crée le serveur tout seul. Il demande une carte bancaire (plan *Starter*, environ 7 $/mois, résiliable).
3. 👤 Attendez que le service passe à **Live** (5 à 10 min la première fois).
4. 👤 Ouvrez `https://VOTRE-SERVEUR.onrender.com/healthz` (l'adresse est en haut de la page du service).
   Vous devez voir `"status":"ok"`.
5. 👤 Dans Render : le service → **Environment** → copiez la valeur de `API_KEY` (générée automatiquement).

Aucune autre clé n'est nécessaire pour ce premier test :
- sans clé d'adresses, les colis sont placés au **centre de leur code postal** (assez pour tester le scan, la lecture et l'ordre de la tournée) ;
- sans clé Gemini, seules les étiquettes très abîmées ne sont pas lues.

Plus tard : ajoutez `GEMINI_API_KEY` et une clé d'adresses précises (`IDEAL_POSTCODES_API_KEY` ou `OS_PLACES_API_KEY`) dans **Environment**.

## B. L'app sur le téléphone (Android)

1. 👤 Sur le téléphone, ouvrez : **https://github.com/AleaToir3/Livraison-/releases/tag/android-latest**
2. 👤 Téléchargez `uk-last-mile.apk`, ouvrez-le, autorisez « installer des apps inconnues » pour votre navigateur.
   Si Play Protect avertit : **Installer quand même** (l'APK de test n'est pas publié sur le Play Store).
3. 👤 Lancez l'app, acceptez la caméra. Elle ouvre **Réglages** :
   - **Adresse du serveur** : l'adresse Render ;
   - **Clé API** : la valeur copiée à l'étape A5 ;
   - **Dépôt** : latitude et longitude de votre dépôt ;
   - **Tester la connexion** : tout doit être vert, puis **Enregistrer**.

## C. Le test

1. 👤 Visez l'adresse d'un colis. Zoomez (boutons **2×** / **3×**) pour que l'adresse remplisse le **cadre jaune**.
   L'app ne lit que ce qui est dans le cadre.
2. 👤 Un bip de vibration confirme le scan ; la ligne passe de `…` à `✓` (ou `≈` si la position est approximative).
3. 👤 Scannez 5 à 10 colis, puis **Tournée**.
   En haut de l'écran Tournée, deux boutons changent le calcul :
   - **Départ : dépôt / ma position** : « ma position » utilise le GPS du téléphone (le téléphone demande l'autorisation).
     C'est le bon choix si vous n'êtes pas au dépôt, par exemple chez vous pour tester ;
   - **Retour au départ : oui / non** : « non » arrête la tournée au dernier arrêt, sans retour.

   Le gros bouton jaune **Lancer les N arrêts** ouvre Google Maps avec tous les arrêts dans l'ordre calculé.
4. 👤 Envoyez-moi : une capture d'écran de ce qui ne va pas, et l'étiquette (photo) si la lecture est fausse.

## Si ça ne marche pas

| Symptôme | Cause probable |
|---|---|
| « Serveur injoignable » dans Réglages | Adresse mal copiée, ou le service Render n'est pas encore **Live** |
| « Clé API REFUSÉE » | Clé copiée avec un espace, ou clé différente de celle de Render |
| `≈` à côté des colis | Normal sans clé d'adresses précises (position = centre du code postal) |
| Rien ne se scanne | Mauvaise lumière : bouton **Lampe**. Adresse hors du cadre jaune |
| « Étiquette abîmée » ne fait rien | Pas de clé Gemini sur le serveur |
