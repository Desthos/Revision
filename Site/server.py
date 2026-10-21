import base64
import binascii
import io
import logging
import os
import re
import time
import unicodedata
import zipfile
from xml.etree import ElementTree
from pathlib import Path
from typing import Any

import httpx
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
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash").removeprefix("models/").strip()
FAST_MODEL = os.getenv("GEMINI_FAST_MODEL", "gemini-3.5-flash-lite").removeprefix("models/").strip()
FAST_REQUEST_TIMEOUT_MS = 25_000
GENERATION_CONFIG = {
    "max_output_tokens": 65536,
    # Lower reasoning effort reduces latency for this app's concise study requests.
    "thinking_level": "low",
}
NO_EMOJI_INSTRUCTION = "N’utilise jamais d’emoji ni d’émoticône dans ta réponse, quelle que soit la demande."

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


class CourseFile(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=120)
    data: str = Field(min_length=1, max_length=1_800_000)


class ResourcesRequest(BaseModel):
    chapter_title: str = Field(min_length=1, max_length=200)
    course: str = Field(default="", max_length=100_000)
    query: str = Field(min_length=2, max_length=2000)
    files: list[CourseFile] = Field(default_factory=list, max_length=12)


class RevisionRequest(BaseModel):
    chapter_title: str = Field(min_length=1, max_length=200)
    course: str = Field(min_length=1, max_length=30000)


def get_gemini_client(*, fast_mode: bool = False) -> genai.Client:
    api_key = get_gemini_api_key()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=f"GEMINI_API_KEY n’est pas configurée. Ajoute-la dans {BASE_DIR / '.env'}.",
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=FAST_REQUEST_TIMEOUT_MS if fast_mode else None,
            retry_options=types.HttpRetryOptions(
                # One API attempt per model; application-level fallback is explicit and bounded.
                attempts=1,
                initial_delay=1.0,
                max_delay=4.0,
                exp_base=2.0,
                jitter=0.2,
                http_status_codes=[503],
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


def _extract_provider_error_message(error: Exception | None) -> str:
    if error is None:
        return ""

    pending: list[Any] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        for attribute in ("message", "details"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested_error = value.get("error")
                nested_message = nested_error.get("message") if isinstance(nested_error, dict) else None
                for candidate in (value.get("message"), nested_message):
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()

        for nested in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(nested, Exception):
                pending.append(nested)

    return str(error)


def _format_retry_delay(value: str) -> str:
    units = {"d": "j", "h": "h", "m": "min", "s": "s"}
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", value, re.IGNORECASE)
    return " ".join(f"{amount} {units[unit.lower()]}" for amount, unit in parts)


def _rate_limit_error_detail(error: Exception | None) -> str:
    provider_message = _extract_provider_error_message(error)
    model_match = re.search(
        r"[\"']?model[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9._-]*)",
        provider_message,
        re.IGNORECASE,
    )
    model_context = f" du modèle Gemini « {model_match.group(1)} »" if model_match else " du modèle Gemini utilisé"
    limit_match = re.search(
        r"(?:limit:\s*)?([\d,]+)\s+(requests?|tokens?)\s+per\s+(day|minute|second)",
        provider_message,
        re.IGNORECASE,
    )
    retry_match = re.search(
        r"\bretry\s+in\s+((?:\d+(?:\.\d+)?\s*[dhms]\s*)+)",
        provider_message,
        re.IGNORECASE,
    )
    retry_delay = _format_retry_delay(retry_match.group(1)) if retry_match else ""

    if limit_match and limit_match.group(3).lower() == "day":
        amount, measure = limit_match.group(1), limit_match.group(2).lower()
        limit_label = "requêtes" if measure.startswith("request") else "jetons"
        tier = " sur le forfait gratuit" if "free tier" in provider_message.lower() else ""
        retry = f" Réessaie dans {retry_delay} ou après le renouvellement du quota." if retry_delay else " Réessaie après le renouvellement du quota."
        return (
            f"Le quota journalier{model_context} de ce projet est atteint : {amount} {limit_label} par jour{tier}."
            f"{retry} Les appels d’autres clés ou applications du même projet comptent aussi. Vérifie l’utilisation de ce modèle dans Google AI Studio."
        )

    if limit_match:
        amount, measure, period = limit_match.groups()
        limit_label = "requêtes" if measure.lower().startswith("request") else "jetons"
        period_label = {"minute": "minute", "second": "seconde"}[period.lower()]
        retry = f" Réessaie dans {retry_delay}." if retry_delay else " Attends un peu avant de réessayer."
        return f"La limite Gemini est atteinte : {amount} {limit_label} par {period_label}.{retry}"

    if retry_delay:
        return f"Gemini demande d’attendre {retry_delay} avant la prochaine requête. Il s’agit d’une limite de débit ou de quota, pas d’une erreur dans le cours."

    return "Gemini a atteint une limite de débit ou de quota. Consulte les limites et l’utilisation de ce projet dans Google AI Studio."


def _gemini_error_details(
    code: int,
    error: Exception | None = None,
    *,
    model: str = MODEL,
) -> tuple[int, str]:
    if code in (401, 403):
        return 502, "La clé Gemini est invalide ou n’a pas accès à ce modèle. Vérifie GEMINI_API_KEY."
    if code == 404:
        return 502, f"Le modèle Gemini « {model} » ou l’API demandée est introuvable. Vérifie le nom du modèle configuré."
    if code == 429:
        return 429, _rate_limit_error_detail(error)
    if code == 503:
        return 503, "Gemini est temporairement indisponible ou surchargé (HTTP 503). Ce code ne signifie généralement pas que ton quota est atteint; les limites de quota ou de débit sont signalées en HTTP 429. Réessaie dans quelques instants."
    if code == 400:
        return 502, "Gemini a refusé la demande (HTTP 400). Vérifie le modèle et les paramètres de l’API."
    return 502, f"Gemini n’a pas pu traiter la demande (HTTP {code}). Réessaie dans quelques instants."


def _extract_http_status(error: Exception) -> int | None:
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        for attribute in ("status_code", "code"):
            value = getattr(current, attribute, None)
            try:
                status = int(value)
            except (TypeError, ValueError):
                continue
            if 400 <= status <= 599:
                return status

        response = getattr(current, "response", None)
        response_status = getattr(response, "status_code", None)
        try:
            response_status = int(response_status)
        except (TypeError, ValueError):
            response_status = None
        if response_status is not None and 400 <= response_status <= 599:
            return response_status

        match = re.search(r"\b(?:Error code:\s*|HTTP\s+)([45]\d{2})\b", str(current), re.IGNORECASE)
        if match:
            return int(match.group(1))

        for nested in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(nested, Exception):
                pending.append(nested)
    return None


def _is_timeout_error(error: Exception) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.TimeoutException):
            return True
        for nested in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(nested, Exception):
                pending.append(nested)
    return False


def generate_with_gemini(
    prompt: str,
    *,
    use_search: bool,
    file_inputs: list[dict[str, str]] | None = None,
    fast_mode: bool = False,
) -> str:
    active_model = MODEL
    started_at = time.perf_counter()
    try:
        client = get_gemini_client(fast_mode=fast_mode)
        options: dict[str, Any] = {}
        if use_search:
            options["tools"] = [{"type": "google_search"}]

        input_items = [{"type": "text", "text": prompt}, *(file_inputs or [])]
        models_to_try = [FAST_MODEL, FALLBACK_MODEL] if fast_mode else [MODEL, FALLBACK_MODEL]
        models_to_try = list(dict.fromkeys(model for model in models_to_try if model))
        if not models_to_try:
            models_to_try = [MODEL]

        for model_index, active_model in enumerate(models_to_try):
            try:
                logger.info(
                    "Envoi Gemini (modèle=%s, tentative=%d/%d, mode_rapide=%s)",
                    active_model,
                    model_index + 1,
                    len(models_to_try),
                    fast_mode,
                )
                interaction = client.interactions.create(
                    model=active_model,
                    input=input_items,
                    generation_config=(
                        {**GENERATION_CONFIG, "thinking_level": "minimal"}
                        if fast_mode and active_model == FAST_MODEL
                        else GENERATION_CONFIG
                    ),
                    **options,
                )
                if model_index > 0:
                    logger.info("Gemini a répondu avec le modèle de secours %s", active_model)
                break
            except Exception as error:
                status = _extract_http_status(error)
                has_fallback = model_index + 1 < len(models_to_try)
                timed_out = fast_mode and _is_timeout_error(error)
                if (status != 503 and not timed_out) or not has_fallback:
                    raise
                logger.warning(
                    "Le modèle Gemini %s a échoué (%s); nouvel essai avec %s.",
                    active_model,
                    f"HTTP {status}" if status else "délai dépassé",
                    models_to_try[model_index + 1],
                )

        output_text = getattr(interaction, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            logger.info(
                "Réponse Gemini reçue (modèle=%s, mode_rapide=%s, durée=%.1fs, prompt=%d caractères)",
                active_model,
                fast_mode,
                time.perf_counter() - started_at,
                len(prompt),
            )
            return output_text.strip()

        steps = getattr(interaction, "steps", None) or []
        for step in reversed(steps):
            if getattr(step, "type", None) != "model_output":
                continue
            output = _extract_text(getattr(step, "content", None))
            if output.strip():
                logger.info(
                    "Réponse Gemini reçue (modèle=%s, mode_rapide=%s, durée=%.1fs, prompt=%d caractères)",
                    active_model,
                    fast_mode,
                    time.perf_counter() - started_at,
                    len(prompt),
                )
                return output.strip()
        raise RuntimeError("Gemini n’a renvoyé aucun texte de réponse.")
    except HTTPException:
        raise
    except genai.errors.APIError as error:
        status = _extract_http_status(error)
        logger.exception(
            "Gemini a échoué (modèle=%s, mode_rapide=%s, durée=%.1fs, HTTP=%s)",
            active_model,
            fast_mode,
            time.perf_counter() - started_at,
            status or "inconnue",
        )
        status_code, detail = _gemini_error_details(status, error, model=active_model) if status is not None else (
            502,
            "Gemini n’a pas pu traiter la demande. Réessaie dans quelques instants.",
        )
        raise HTTPException(status_code=status_code, detail=detail) from error
    except Exception as error:
        logger.exception("Échec de la requête Gemini")
        # Interactions SDK versions expose the HTTP status as status_code,
        # code, on response.status_code, or only in the wrapped error message.
        status = _extract_http_status(error)
        if fast_mode and _is_timeout_error(error):
            raise HTTPException(
                status_code=504,
                detail="Gemini n’a pas répondu assez vite avec le modèle de secours. Réessaie dans quelques instants.",
            ) from error
        if status is not None:
            status_code, detail = _gemini_error_details(status, error, model=active_model)
            raise HTTPException(status_code=status_code, detail=detail) from error
        raise HTTPException(
            status_code=502,
            detail="Gemini n’a pas pu traiter la demande. Réessaie dans quelques instants.",
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


def request_is_quiz(query: str) -> bool:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", query.casefold())
        if not unicodedata.combining(character)
    )
    return bool(re.search(r"\b(?:quiz|qcm|questionnaire|interrogation|questions?)\b", normalized))


def _xml_paragraphs(xml_bytes: bytes) -> list[str]:
    root = ElementTree.fromstring(xml_bytes)
    paragraphs = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] in ("p", "h"):
            text = "".join(element.itertext()).strip()
            if text:
                paragraphs.append(text)
    return paragraphs


def _extract_office_text(file_bytes: bytes, mime_type: str) -> str:
    xml_target_by_mime = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word/document.xml",
        "application/vnd.oasis.opendocument.text": "content.xml",
    }
    if mime_type in xml_target_by_mime:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            info = archive.getinfo(xml_target_by_mime[mime_type])
            if info.file_size > 5_000_000:
                raise HTTPException(status_code=413, detail="Le texte extrait de ce fichier est trop volumineux.")
            return "\n".join(_xml_paragraphs(archive.read(info)))

    if mime_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            slide_names = [
                name for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ]
            slide_names.sort(key=lambda name: int(re.search(r"slide(\d+)", name).group(1)))
            if sum(archive.getinfo(name).file_size for name in slide_names) > 5_000_000:
                raise HTTPException(status_code=413, detail="Le texte extrait de ce fichier est trop volumineux.")
            return "\n\n".join(
                "\n".join(_xml_paragraphs(archive.read(name))) for name in slide_names
            )

    if mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 10_000_000:
                raise HTTPException(status_code=413, detail="Le texte extrait de ce fichier est trop volumineux.")
            shared_strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared_strings = ["".join(item.itertext()) for item in shared_root]
            sheet_names = [
                name for name in archive.namelist()
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            ]
            sheet_names.sort(key=lambda name: int(re.search(r"sheet(\d+)", name).group(1)))
            rows = []
            for sheet_name in sheet_names:
                sheet = ElementTree.fromstring(archive.read(sheet_name))
                for row in (element for element in sheet.iter() if element.tag.rsplit("}", 1)[-1] == "row"):
                    values = []
                    for cell in (element for element in row if element.tag.rsplit("}", 1)[-1] == "c"):
                        cell_type = cell.attrib.get("t")
                        value_element = next((part for part in cell if part.tag.rsplit("}", 1)[-1] == "v"), None)
                        if cell_type == "inlineStr":
                            value = "".join(cell.itertext())
                        elif value_element is not None:
                            value = value_element.text or ""
                            if cell_type == "s":
                                try:
                                    value = shared_strings[int(value)]
                                except (ValueError, IndexError):
                                    pass
                        else:
                            value = ""
                        values.append(value.strip())
                    if any(values):
                        rows.append(" | ".join(values))
            return "\n".join(rows)

    if mime_type == "application/vnd.oasis.opendocument.spreadsheet":
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as archive:
            info = archive.getinfo("content.xml")
            if info.file_size > 10_000_000:
                raise HTTPException(status_code=413, detail="Le texte extrait de ce fichier est trop volumineux.")
            root = ElementTree.fromstring(archive.read(info))
            rows = []
            for row in (element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "table-row"):
                values = []
                for cell in row:
                    if cell.tag.rsplit("}", 1)[-1] not in ("table-cell", "covered-table-cell"):
                        continue
                    value = " ".join(text.strip() for text in _xml_paragraphs(ElementTree.tostring(cell)) if text.strip())
                    values.append(value)
                if any(values):
                    rows.append(" | ".join(values))
            return "\n".join(rows)

    raise HTTPException(
        status_code=400,
        detail=f"Le fichier « {mime_type} » n’est pas pris en charge pour l’IA. Utilise un PDF, une image, un fichier TXT/RTF, DOCX, ODT, PPTX, XLSX ou ODS.",
    )


def _extract_rtf_text(file_bytes: bytes) -> str:
    text = file_bytes.decode("cp1252", errors="replace")
    text = re.sub(
        r"\\'([0-9a-fA-F]{2})",
        lambda match: bytes([int(match.group(1), 16)]).decode("cp1252", errors="replace"),
        text,
    )
    text = re.sub(
        r"\\u(-?\d+)\??",
        lambda match: chr(int(match.group(1)) % 65536),
        text,
    )
    text = re.sub(r"\\(?:par|line)\b\s?", "\n", text)
    text = re.sub(r"\\tab\b\s?", "\t", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", text)
    text = re.sub(r"\\([{}\\])", r"\1", text)
    return re.sub(r"[{}]", " ", text)


def _course_file_inputs(files: list[CourseFile]) -> tuple[str, list[dict[str, str]]]:
    text_sections = []
    gemini_inputs = []
    allowed_image_types = {"image/jpeg", "image/png", "image/webp", "image/bmp"}
    allowed_office_types = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/rtf",
        "text/rtf",
        "text/plain",
    }
    total_data_length = sum(len(course_file.data) for course_file in files)
    if total_data_length > 8_000_000:
        raise HTTPException(status_code=413, detail="Les fichiers du cours dépassent la taille totale autorisée de 8 Mo. Envoie-en moins à la fois.")

    for course_file in files:
        try:
            file_bytes = base64.b64decode(course_file.data, validate=True)
        except (ValueError, binascii.Error) as error:
            raise HTTPException(status_code=400, detail=f"Le fichier « {course_file.name} » est invalide.") from error

        if course_file.mime_type == "application/pdf":
            gemini_inputs.append({
                "type": "document",
                "data": base64.b64encode(file_bytes).decode("ascii"),
                "mime_type": course_file.mime_type,
            })
        elif course_file.mime_type in allowed_image_types:
            gemini_inputs.append({
                "type": "image",
                "data": base64.b64encode(file_bytes).decode("ascii"),
                "mime_type": course_file.mime_type,
            })
        elif course_file.mime_type in allowed_office_types:
            if course_file.mime_type in ("text/plain", "text/rtf", "application/rtf"):
                extracted = file_bytes.decode("utf-8", errors="replace")
                if course_file.mime_type != "text/plain":
                    extracted = _extract_rtf_text(file_bytes)
            else:
                try:
                    extracted = _extract_office_text(file_bytes, course_file.mime_type)
                except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError) as error:
                    raise HTTPException(status_code=400, detail=f"Le fichier « {course_file.name} » ne peut pas être lu.") from error
            if extracted.strip():
                if len(extracted) > 50_000:
                    raise HTTPException(status_code=413, detail=f"Le texte extrait de « {course_file.name} » dépasse la taille autorisée.")
                text_sections.append(f"Contenu du fichier {course_file.name} :\n{extracted.strip()}")
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Le fichier « {course_file.name} » n’est pas pris en charge. Utilise un PDF, une image, un fichier TXT/RTF, DOCX, ODT, PPTX, XLSX ou ODS.",
            )
    return "\n\n".join(text_sections), gemini_inputs


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True, "gemini_configured": bool(get_gemini_api_key())}


@app.post("/api/resources")
def create_resources(request: ResourcesRequest) -> dict[str, str]:
    attached_text, file_inputs = _course_file_inputs(request.files)
    course_sections = [part for part in (request.course.strip(), attached_text) if part]
    course = "\n\n".join(course_sections)
    if len(course) > 100_000:
        raise HTTPException(status_code=413, detail="Le cours complet dépasse 100 000 caractères. Réduis le texte ou le nombre de fichiers.")
    if not course and not file_inputs:
        course = "Aucun texte ni fichier de cours n’a été ajouté pour ce chapitre."
    use_search = request_needs_search(request.query)
    search_instruction = (
        "Si la demande nécessite des informations externes récentes ou des liens, utilise Google Search pour vérifier les éléments utiles et cite les sources avec leurs liens."
        if use_search
        else "N’effectue pas de recherche sur le Web : base ta réponse sur le cours fourni et la demande."
    )
    attached_names = ", ".join(course_file.name for course_file in request.files) or "aucun"
    prompt = f"""Tu aides un élève à réviser et à trouver des ressources utiles.
Chapitre : {request.chapter_title}
Cours complet saisi pour ce chapitre :
{course}
Fichiers joints du cours : {attached_names}

Demande de l’élève : {request.query}

Réponds directement à la demande en français, sous forme de texte clair et bien formaté en Markdown.
Utilise le cours complet et les fichiers joints comme source principale. N’invente pas de faits qui contredisent le cours; si une information nécessaire manque, signale-le brièvement.
Sois le plus synthétique possible. Commence directement par le contenu, sans phrase d’introduction, et ne termine pas par une phrase de conclusion.
{search_instruction}
{NO_EMOJI_INSTRUCTION}
"""
    return {
        "response": generate_with_gemini(
            prompt,
            use_search=use_search,
            file_inputs=file_inputs,
            fast_mode=request_is_quiz(request.query),
        )
    }


@app.post("/api/revision")
def create_revision(request: RevisionRequest) -> dict[str, str]:
    prompt = f"""Tu es un professeur qui aide un élève à réviser.
Chapitre : {request.chapter_title}

Crée une séance de révision en français à partir du cours ci-dessous uniquement :
- 5 questions progressives, avec un mélange de rappel et de compréhension;
- puis un corrigé séparé, clairement titré;
- n’ajoute pas de notions absentes du cours et signale toute ambiguïté.
Sois synthétique, commence directement par les questions sans phrase d’introduction et ne termine pas par une conclusion.
{NO_EMOJI_INSTRUCTION}

Cours :
{request.course}"""
    return {"response": generate_with_gemini(prompt, use_search=False, fast_mode=True)}


app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="site")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
