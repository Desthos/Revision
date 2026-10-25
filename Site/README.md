# Site de révision

## Lancement local

Depuis la racine du dépôt (`Revision`), entre d’abord dans le dossier `Site` :

1. Crée l’environnement et installe les dépendances :

   ```sh
cd Site
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
   ```

2. Crée un fichier `.env` à côté de `server.py` avec ta clé API :

   ```env
   GEMINI_API_KEY=ta_cle_api
   GEMINI_MODEL=gemini-3.8-flash
   GEMINI_FALLBACK_MODEL=gemini-3.6-flash
   ```

   `.env` est ignoré par Git; ne publie jamais cette clé.

3. Démarre le site et son API :

   ```sh
source .venv/bin/activate
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
   ```

4. Ouvre `http://127.0.0.1:8000`.

Vérifie `http://127.0.0.1:8000/api/health` : la réponse doit être JSON avec `"generation_configured": true`. Si tu vois une page HTML ou un refus 403, le navigateur n’est pas connecté à ce serveur FastAPI.

L’onglet Ressources transmet au service IA la demande saisie, tout le texte du cours du chapitre et les fichiers joints : PDF et images du cours sont envoyés comme pièces multimodales; TXT, RTF, DOCX, ODT, PPTX, XLSX et ODS sont convertis en texte avant l’appel. Les anciens formats binaires DOC, PPT et XLS ne sont pas pris en charge. Le service génère une réponse sous forme de texte formaté à partir du cours et de la demande. Les réponses doivent être synthétiques, sans phrase d’introduction ni de conclusion, et sans émojis. Le niveau de raisonnement est `low` pour les demandes courantes. Les demandes de quiz utilisent automatiquement `GEMINI_FAST_MODEL` (par défaut `gemini-3.5-flash-lite`) avec le niveau `minimal`, conçu pour les réponses courtes à faible latence. Le délai réseau par tentative est limité à 25 secondes; en cas de surcharge ou de délai dépassé, le serveur bascule vers le modèle de secours. Les autres demandes utilisent `GEMINI_MODEL` (par défaut `gemini-3.8-flash`) et peuvent utiliser Google Search pour les demandes de ressources externes. Le navigateur envoie toujours le cours et les fichiers au serveur local; celui-ci les transmet au service IA avec la clé API configurée.

Une erreur HTTP 503 indique généralement que le service IA est temporairement indisponible ou surchargé; les erreurs de quota ou de débit sont généralement signalées en HTTP 429. Le SDK ne répète plus les appels automatiquement. Après un 503 — ou un délai dépassé pour un quiz — l’application effectue au plus un appel supplémentaire au modèle de secours (`GEMINI_FALLBACK_MODEL`, par défaut `gemini-3.6-flash`); après un 429, elle s’arrête sans autre appel. Un basculement est donc un second appel API et peut utiliser le quota propre au modèle de secours. Les limites s’appliquent au projet Google et varient selon le modèle; les appels d’autres clés ou applications du même projet comptent aussi. Vérifie l’utilisation en temps réel dans Google AI Studio. Tu peux remplacer `GEMINI_FAST_MODEL` ou `GEMINI_FALLBACK_MODEL` dans `.env`; redémarre le serveur après ces changements.

Chaque réponse générée est enregistrée avec son chapitre dans le stockage local du navigateur et incluse dans l’export JSON. L’import des anciennes sauvegardes sans ressources reste accepté.
