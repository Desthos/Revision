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
   ```

   `.env` est ignoré par Git; ne publie jamais cette clé.

3. Démarre le site et son API :

   ```sh
source .venv/bin/activate
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
   ```

4. Ouvre `http://127.0.0.1:8000`.

Vérifie `http://127.0.0.1:8000/api/health` : la réponse doit être JSON avec `"gemini_configured": true`. Si tu vois une page HTML ou un refus 403, le navigateur n’est pas connecté à ce serveur FastAPI.

L’onglet Ressources transmet à Gemini la demande saisie, tout le texte du cours du chapitre et les fichiers joints : PDF et images du cours sont envoyés comme pièces multimodales; TXT, RTF, DOCX, ODT, PPTX, XLSX et ODS sont convertis en texte avant l’appel. Les anciens formats binaires DOC, PPT et XLS ne sont pas pris en charge. Gemini répond sous forme de texte formaté à partir du cours et de la demande. Les réponses doivent être synthétiques, sans phrase d’introduction ni de conclusion, et sans émojis. L’appel utilise `GEMINI_MODEL` (par défaut `gemini-3.8-flash`) et peut utiliser Google Search pour les demandes de ressources externes. Le navigateur envoie le cours et les fichiers au serveur local; celui-ci les transmet à Gemini avec la clé API configurée.
