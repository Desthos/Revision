# Site de révision

## Lancement local

1. Crée l’environnement et installe les dépendances :

   ```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
   ```

2. Crée un fichier `.env` à côté de `server.py` avec ta clé Gemini :

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

Vérifie `http://127.0.0.1:8000/api/health` : la réponse doit être JSON avec `"gemini_configured": true`. Si tu vois une page HTML ou un refus 403, le navigateur n’est pas connecté à ce serveur FastAPI.

L’onglet Ressources transmet à Gemini la demande saisie, tout le texte du cours du chapitre et les fichiers joints : PDF et images du cours sont envoyés comme pièces multimodales; TXT, RTF, DOCX, ODT, PPTX, XLSX et ODS sont convertis en texte avant l’appel. Les anciens formats binaires DOC, PPT et XLS ne sont pas pris en charge. Gemini répond sous forme de texte formaté à partir du cours et de la demande. Les réponses doivent être synthétiques, sans phrase d’introduction ni de conclusion, et sans émojis. Le niveau de raisonnement est `low` pour les demandes courantes. Les demandes de quiz utilisent automatiquement `GEMINI_FAST_MODEL` (par défaut `gemini-3.5-flash-lite`) avec le niveau `minimal`, conçu pour les réponses courtes à faible latence. Le délai réseau par tentative est limité à 25 secondes; en cas de surcharge ou de délai dépassé, le serveur bascule vers le modèle de secours. Les autres demandes utilisent `GEMINI_MODEL` (par défaut `gemini-3.8-flash`) et peuvent utiliser Google Search pour les demandes de ressources externes. Le navigateur envoie toujours le cours et les fichiers au serveur local; celui-ci les transmet à Gemini avec la clé API configurée.

Une erreur HTTP 503 indique généralement que Gemini est temporairement indisponible ou surchargé; les erreurs de quota ou de débit sont généralement signalées en HTTP 429. Les demandes courantes réessaient deux fois avec un délai progressif puis basculent vers `GEMINI_FALLBACK_MODEL` (par défaut `gemini-3.6-flash`). Les demandes de quiz passent directement au modèle de secours après une erreur 503 ou un délai dépassé. Le modèle de secours reçoit la même demande, le cours, les fichiers et, si nécessaire, la recherche Web. Les deux modèles restent hébergés par Google : si les deux sont indisponibles, il faudra réessayer plus tard. Tu peux remplacer `GEMINI_FAST_MODEL` ou `GEMINI_FALLBACK_MODEL` dans `.env`; redémarre le serveur après ces changements.

Chaque réponse générée est enregistrée avec son chapitre dans le stockage local du navigateur et incluse dans l’export JSON. L’import des anciennes sauvegardes sans ressources reste accepté.
