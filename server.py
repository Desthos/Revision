import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from google import genai
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MODEL = os.getenv("GEMINI_MODEL", "models/gemini-3.8-flash")
GENERATION_CONFIG = {
    "max_output_tokens": 65536,
    "thinking_level": "medium",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("revision-site")
app = FastAPI(title="Révision Gemini API")


class ResourcesRequest(BaseModel):
    chapter_title: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=2, max_length=2000)


class RevisionRequest(BaseModel):
    chapter_title: str = Field(min_length=1, max_length=200)
    course: str = Field(min_length=1, max_length=30000)


def get_gemini_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="GEMINI_API_KEY n’est pas configurée. Ajoute-la dans le fichier .env local.",
        )
    return genai.Client(api_key=api_key)


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_extract_text(part) for part in value)))
    if isinstance(value, dict):
        for key in ("text", "content", "output"):
            if key in value:
                result = _extract_text(value[key])
                if result:
                    return result
        return ""

    for attribute in ("text", "content", "output"):
        candidate = getattr(value, attribute, None)
        if candidate is not None:
            result = _extract_text(candidate)
            if result:
                return result
    return ""


def generate_with_gemini(prompt: str, *, use_search: bool) -> str:
    client = get_gemini_client()
    options: dict[str, Any] = {}
    if use_search:
        options["tools"] = [{"type": "google_search"}]

    try:
        interaction = client.interactions.create(
            model=MODEL,
            input=prompt,
            generation_config=GENERATION_CONFIG,
            **options,
        )
        steps = getattr(interaction, "steps", None)
        if not steps:
            raise RuntimeError("Gemini n’a renvoyé aucun résultat.")
        output = _extract_text(steps[-1]) or str(steps[-1])
        return output.strip()
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Échec de la requête Gemini")
        raise HTTPException(
            status_code=502,
            detail="Gemini n’a pas pu répondre. Vérifie la configuration et réessaie.",
        ) from error


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True, "gemini_configured": bool(os.environ.get("GEMINI_API_KEY"))}


@app.post("/api/resources")
def create_resources(request: ResourcesRequest) -> dict[str, str]:
    prompt = f"""Tu aides un élève à trouver des ressources pédagogiques fiables.
Chapitre : {request.chapter_title}
Demande : {request.query}

Utilise la recherche Google pour trouver des ressources pertinentes et accessibles.
Réponds en français, en distinguant les types de ressources, avec leurs URL complètes.
N’invente aucune source; indique clairement si tu n’en trouves pas."""
    return {"response": generate_with_gemini(prompt, use_search=True)}


@app.post("/api/revision")
def create_revision(request: RevisionRequest) -> dict[str, str]:
    prompt = f"""Tu es un professeur qui aide un élève à réviser.
Chapitre : {request.chapter_title}

Crée une séance de révision en français à partir du cours ci-dessous uniquement :
- 5 questions progressives, avec un mélange de rappel et de compréhension;
- puis un corrigé séparé, clairement titré;
- n’ajoute pas de notions absentes du cours et signale toute ambiguïté.

Cours :
{request.course}"""
    return {"response": generate_with_gemini(prompt, use_search=False)}


app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="site")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
