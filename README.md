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
   GEMINI_MODEL=models/gemini-3.8-flash
   ```

   `.env` est ignoré par Git; ne publie jamais cette clé.

3. Démarre le site et son API :

   ```sh
   source .venv/bin/activate
   python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
   ```

4. Ouvre `http://127.0.0.1:8000`.

L’onglet Ressources utilise Google Search via Gemini. L’onglet Révision génère des questions à partir du texte saisi dans le chapitre. Les photos et documents joints ne sont pas transmis au modèle pour l’instant.
