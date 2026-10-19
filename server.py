import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").removeprefix("models/")
GENERATION_CONFIG = {
    "max_output_tokens": 65536,
    "thinking_level": "medium",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("revision-site")
app = FastAPI(title="Révision Gemini API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class ResourcesRequest(BaseModel):
    chapter_title: str = Field(min_length=1, max_length=200)
    course: str = Field(default="", max_length=100_000)
    query: str = Field(min_length=2, max_length=2000)


class RevisionRequest(BaseModel):
    chapter_title: str = Field(min_length=1, max_length=200)
    course: str = Field(min_length=1, max_length=30000)


def get_gemini_client() -> genai.Client:
    api_key = get_gemini_api_key()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=f"GEMINI_API_KEY n’est pas configurée. Ajoute-la dans {BASE_DIR / '.env'}.",
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(
                attempts=3,
                http_status_codes=[408, 500, 502, 503, 504],
            )
        ),
    )


def get_gemini_api_key() -> str | None:
    # Reload on each request so adding .env while Uvicorn is running takes effect.
    load_dotenv(BASE_DIR / ".env", override=False)
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


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
    try:
        client = get_gemini_client()
        options: dict[str, Any] = {}
        if use_search:
            options["tools"] = [{"type": "google_search"}]

        interaction = client.interactions.create(
            model=MODEL,
            input=prompt,
            generation_config=GENERATION_CONFIG,
            **options,
        )
        output_text = getattr(interaction, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        steps = getattr(interaction, "steps", None) or []
        for step in reversed(steps):
            if getattr(step, "type", None) != "model_output":
                continue
            output = _extract_text(getattr(step, "content", None))
            if output.strip():
                return output.strip()
        raise RuntimeError("Gemini n’a renvoyé aucun texte de réponse.")
    except HTTPException:
        raise
    except genai.errors.APIError as error:
        logger.exception("Gemini a renvoyé une erreur HTTP %s", error.code)
        if error.code in (401, 403):
            detail = "La clé Gemini est invalide ou n’a pas accès à ce modèle. Vérifie GEMINI_API_KEY."
        elif error.code == 404:
            detail = f"Le modèle Gemini « {MODEL} » ou l’API demandée est introuvable. Vérifie GEMINI_MODEL."
        elif error.code == 429:
            detail = "Le quota ou la limite de requêtes Gemini est atteint. Réessaie plus tard."
        elif error.code == 400:
            detail = "Gemini a refusé la demande (HTTP 400). Vérifie le modèle et les paramètres de l’API."
        else:
            detail = f"Gemini a renvoyé une erreur HTTP {error.code}. Consulte le terminal du serveur pour le détail."
        raise HTTPException(status_code=502, detail=detail) from error
    except Exception as error:
        logger.exception("Échec de la requête Gemini")
        raise HTTPException(
            status_code=502,
            detail=f"Erreur technique Gemini ({type(error).__name__}). Consulte le terminal du serveur pour le détail.",
        ) from error


def request_needs_search(query: str) -> bool:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", query.casefold())
        if not unicodedata.combining(character)
    )
    external_resource = re.search(
        r"\b(?:liens?|videos?|sites?|articles?|sources?|podcasts?|internet|web|en ligne)\b",
        normalized,
    )
    generic_resource_search = re.search(r"\bressources?\b", normalized) and re.search(
        r"\b(?:cherche|recherche|trouve|recommande|propose)\b", normalized
    )
    return bool(external_resource or generic_resource_search)


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True, "gemini_configured": bool(get_gemini_api_key())}


@app.post("/api/resources")
def create_resources(request: ResourcesRequest) -> dict[str, str]:
    course = request.course.strip() or "Aucun texte de cours n’a été saisi pour ce chapitre."
    use_search = request_needs_search(request.query)
    search_instruction = (
        "Si la demande porte sur des ressources externes, utilise Google Search et fournis des liens complets vers des ressources pertinentes."
        if use_search
        else "Ne fais pas de recherche sur le Web : crée le contenu demandé à partir du cours fourni."
    )
    prompt = f"""Tu es un assistant pédagogique qui répond directement à la demande d’un élève.
Chapitre : {request.chapter_title}
Cours complet saisi pour ce chapitre :
{course}

Demande de l’élève : {request.query}

Réponds en français et réalise précisément ce que demande l’élève en t’appuyant sur le cours ci-dessus.
{search_instruction}
Si elle demande de créer une fiche, des exercices ou une explication, produis-les à partir du cours et signale clairement les informations absentes.
N’invente aucune source ni aucun lien."""
    return {"response": generate_with_gemini(prompt, use_search=use_search)}


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
