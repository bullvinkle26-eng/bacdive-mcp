"""
BacDive Remote MCP Server
=========================
FastMCP-сервер для работы с открытым REST API DSMZ BacDive (API v2).
Предоставляет асинхронные инструменты для поиска бактериальных и архейных штаммов
по таксономии, коллекционным номерам и номерам сиквенсов (16S рРНК и геномов),
а также получение детальной информации по штаммам с выборочной фильтрацией разделов.

Готов для деплоя на Render.com как Web Service со Streamable HTTP (или SSE).
"""

import os
import sys
import logging
import urllib.parse
from typing import Any, Dict, List, Optional, Union

import httpx
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# --- Логирование ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("bacdive_server")

# --- Конфигурация ---
BACDIVE_API_BASE = "https://api.bacdive.dsmz.de/v2"
HTTP_TIMEOUT = float(os.environ.get("BACDIVE_TIMEOUT", "30.0"))
DEFAULT_PORT = int(os.environ.get("PORT", 8000))
SERVER_HOST = os.environ.get("HOST", "0.0.0.0")
DEFAULT_TRANSPORT = os.environ.get("MCP_TRANSPORT", "http").strip().lower()
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()


# --- Middleware аутентификации ---
class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware для защиты эндпоинтов MCP сервера.
    Разрешает доступ к эндпоинтам проверки здоровья (/health и /) без авторизации,
    но требует заголовок 'Authorization: Bearer <MCP_API_KEY>' или 'X-API-Key: <MCP_API_KEY>'
    для всех остальных маршрутов (включая /mcp и /sse), если MCP_API_KEY задан.
    """
    def __init__(self, app, api_key: str):
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next):
        # Health check и корневая справка всегда открыты для мониторинга и Render
        if request.url.path in ["/health", "/", "/health/"]:
            return await call_next(request)

        # Если задан секретный ключ, проверяем заголовок Authorization, X-API-Key или query-параметр (?key= или ?token=)
        if self.api_key:
            auth_header = request.headers.get("authorization", "").strip()
            custom_key = request.headers.get("x-api-key", "").strip()
            query_key = (
                request.query_params.get("key")
                or request.query_params.get("token")
                or request.query_params.get("api_key")
                or ""
            ).strip()
            expected_bearer = f"Bearer {self.api_key}"

            is_authorized = (
                auth_header == expected_bearer
                or custom_key == self.api_key
                or query_key == self.api_key
            )

            if not is_authorized:
                logger.warning(
                    "Unauthorized request to '%s' from %s",
                    request.url.path,
                    request.client.host if request.client else "unknown",
                )
                return JSONResponse(
                    {
                        "error": "Unauthorized: Invalid or missing API key. Provide 'Authorization: Bearer <API_KEY>', 'X-API-Key: <API_KEY>', or '?key=<API_KEY>' in the URL."
                    },
                    status_code=401,
                )

        return await call_next(request)

# Карта разделов BacDive для удобной нечувствительной к регистру и частичной фильтрации
KNOWN_SECTIONS = [
    "General",
    "Name and taxonomic classification",
    "Morphology",
    "Culture and growth conditions",
    "Physiology and metabolism",
    "Isolation, sampling and environmental information",
    "Interaction and safety",
    "Sequence information",
    "Genome-based predictions",
    "Literature",
    "Reference",
]

# Создаем экземпляр FastMCP
mcp = FastMCP(
    name="BacDive MCP Server",
    instructions=(
        "You are connected to the DSMZ BacDive MCP server, providing access to the "
        "world's largest database for standardized bacterial and archaeal metadata. "
        "Use search tools to find BacDive IDs by taxon, culture collection numbers, or "
        "sequence accessions, and use get_strain_details to retrieve comprehensive or "
        "section-specific phenotypic, morphological, metabolic, and genomic data."
    ),
)


# --- HTTP Client Helper ---
async def _make_api_request(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Выполняет асинхронный GET-запрос к BacDive REST API v2 с обработкой ошибок.
    """
    url = f"{BACDIVE_API_BASE}/{endpoint.lstrip('/')}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "BacDive-MCP-Server/2.0 (FastMCP; Render.com)",
    }

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            logger.info("Requesting BacDive API: %s with params %s", url, params)
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP error %s from BacDive API for %s", exc.response.status_code, url)
            return {
                "error": True,
                "status_code": exc.response.status_code,
                "message": f"BacDive API returned HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                "results": [],
                "count": 0,
            }
        except httpx.TimeoutException:
            logger.error("Timeout accessing BacDive API: %s", url)
            return {
                "error": True,
                "status_code": 504,
                "message": f"Connection timed out while contacting BacDive API ({HTTP_TIMEOUT}s limit)",
                "results": [],
                "count": 0,
            }
        except Exception as exc:
            logger.exception("Unexpected error while querying BacDive API: %s", exc)
            return {
                "error": True,
                "status_code": 500,
                "message": f"Internal error contacting BacDive API: {str(exc)}",
                "results": [],
                "count": 0,
            }


# --- Маршруты проверки здоровья для Render.com ---
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Эндпоинт проверки жизнеспособности сервиса для оркестраторов (Render, Docker, K8s)."""
    return JSONResponse({
        "status": "healthy",
        "service": "bacdive-mcp-server",
        "api_target": BACDIVE_API_BASE,
        "transport": DEFAULT_TRANSPORT,
    })


@mcp.custom_route("/", methods=["GET"])
async def root_info(request: Request) -> JSONResponse:
    """Корневой эндпоинт с информацией о сервере и доступных маршрутах."""
    return JSONResponse({
        "name": "BacDive MCP Server",
        "status": "running",
        "description": "Model Context Protocol (MCP) server for DSMZ BacDive REST API v2",
        "endpoints": {
            "health": "/health",
            "mcp": "/mcp",
        },
        "documentation": "https://api.bacdive.dsmz.de/v2/",
    })


# --- MCP Инструменты ---

@mcp.tool()
async def search_by_taxon(
    genus: str,
    species: Optional[str] = None,
    subspecies: Optional[str] = None,
    page: int = 1,
) -> Dict[str, Any]:
    """
    Search BacDive strains by taxonomic hierarchy (genus, species, and optional subspecies).

    Args:
        genus: Genus name (e.g. 'Bacillus', 'Escherichia', 'Pseudomonas').
        species: Optional species epithet (e.g. 'subtilis', 'coli').
        subspecies: Optional subspecies epithet (e.g. 'subtilis').
        page: Page number for pagination (starts from 1, each page returns up to 100 strains).

    Returns:
        Dictionary containing total match count, list of BacDive strain IDs, and pagination info.
    """
    clean_genus = genus.strip()
    if not clean_genus:
        return {"error": True, "message": "Genus name must not be empty"}

    # Формируем путь для таксона
    parts = [urllib.parse.quote(clean_genus)]
    if species and species.strip():
        parts.append(urllib.parse.quote(species.strip()))
        if subspecies and subspecies.strip():
            parts.append(urllib.parse.quote(subspecies.strip()))

    endpoint = f"taxon/{'/'.join(parts)}"

    # BacDive API v2 использует 0-индексированную пагинацию (page=0, 1, 2...)
    # Для пользователя транслируем page=1 -> 0, page=2 -> 1
    api_page = max(0, page - 1)
    params = {"page": api_page} if api_page > 0 else None

    data = await _make_api_request(endpoint, params=params)

    if data.get("error"):
        return data

    total_count = data.get("count", 0)
    strain_ids = data.get("results", [])
    has_next = data.get("next") is not None
    has_prev = data.get("previous") is not None

    return {
        "query": {
            "genus": clean_genus,
            "species": species.strip() if species else None,
            "subspecies": subspecies.strip() if subspecies else None,
            "page": page,
        },
        "total_count": total_count,
        "page": page,
        "returned_strains": len(strain_ids),
        "has_next_page": has_next,
        "has_previous_page": has_prev,
        "bacdive_ids": strain_ids,
        "hint": (
            f"Found {total_count} matching strains. Call get_strain_details(strain_id) "
            "with any of the returned IDs to fetch complete morphological, physiological, "
            "or genomic information."
        ),
    }


@mcp.tool()
async def search_by_culture_collection(
    culture_collection_no: str,
    page: int = 1,
) -> Dict[str, Any]:
    """
    Search BacDive strains by culture collection deposit number.

    Args:
        culture_collection_no: Collection number, e.g. 'DSM 30083', 'ATCC 6051', 'JCM 1465', 'NBRC 101211'.
        page: Page number for pagination (starts from 1, each page returns up to 100 strains).

    Returns:
        Dictionary with count and list of corresponding BacDive IDs.
    """
    clean_no = culture_collection_no.strip()
    if not clean_no:
        return {"error": True, "message": "Culture collection number must not be empty"}

    endpoint = f"culturecollectionno/{urllib.parse.quote(clean_no)}"

    api_page = max(0, page - 1)
    params = {"page": api_page} if api_page > 0 else None

    data = await _make_api_request(endpoint, params=params)

    if data.get("error"):
        return data

    total_count = data.get("count", 0)
    strain_ids = data.get("results", [])

    return {
        "query": clean_no,
        "total_count": total_count,
        "page": page,
        "returned_strains": len(strain_ids),
        "has_next_page": data.get("next") is not None,
        "bacdive_ids": strain_ids,
        "hint": (
            "Use get_strain_details(strain_id) with the retrieved ID to inspect full "
            "strain characteristics and metadata."
        ),
    }


@mcp.tool()
async def search_by_sequence_accession(
    accession: str,
    sequence_type: str = "auto",
) -> Dict[str, Any]:
    """
    Search BacDive strains by sequence accession number (16S rRNA gene or genome sequence).

    Args:
        accession: Accession code, e.g. 'AF000162' (16S rRNA), 'GCA_000243275' (NCBI genome assembly).
        sequence_type: Sequence category: 'auto' (automatic detection), '16s' (16S rRNA gene), or 'genome' (whole genome sequence).

    Returns:
        Dictionary with matching BacDive IDs and resolved sequence classification.
    """
    clean_acc = accession.strip()
    if not clean_acc:
        return {"error": True, "message": "Accession number must not be empty"}

    clean_type = sequence_type.strip().lower()

    # Вспомогательная функция для запроса конкретного типа
    async def _query_type(stype: str) -> Dict[str, Any]:
        ep = f"sequence_{stype}/{urllib.parse.quote(clean_acc)}"
        return await _make_api_request(ep)

    resolved_type = clean_type

    if clean_type == "auto":
        # Эвристика: геномные сборки часто начинаются с GCA_, GCF_, ASM
        is_genome_pattern = clean_acc.upper().startswith(("GCA_", "GCF_", "ASM"))
        first_attempt = "genome" if is_genome_pattern else "16s"
        second_attempt = "16s" if is_genome_pattern else "genome"

        res = await _query_type(first_attempt)
        if res.get("count", 0) > 0:
            data = res
            resolved_type = first_attempt
        else:
            # Пробуем второй тип, если первый не дал результатов
            logger.info("Auto-detection: no match with %s, trying %s", first_attempt, second_attempt)
            res2 = await _query_type(second_attempt)
            if res2.get("count", 0) > 0:
                data = res2
                resolved_type = second_attempt
            else:
                data = res
                resolved_type = first_attempt
    elif clean_type in ("16s", "genome"):
        data = await _query_type(clean_type)
    else:
        return {
            "error": True,
            "message": f"Unsupported sequence_type '{sequence_type}'. Choose 'auto', '16s', or 'genome'.",
        }

    if data.get("error"):
        return data

    total_count = data.get("count", 0)
    strain_ids = data.get("results", [])

    return {
        "accession": clean_acc,
        "resolved_sequence_type": resolved_type,
        "total_count": total_count,
        "bacdive_ids": strain_ids,
        "hint": "Fetch strain details using get_strain_details(strain_id)." if strain_ids else "No strains found for this accession number.",
    }


def _match_section_name(requested_section: str, available_sections: List[str]) -> Optional[str]:
    """
    Нечувствительное к регистру и гибкое сопоставление названия раздела.
    Позволяет передавать как точные названия, так и короткие ключевые слова
    (например, 'growth', 'morphology', 'taxonomy', 'metabolism', 'safety').
    """
    req = requested_section.strip().lower()
    
    # 1. Точное совпадение без учета регистра
    for sec in available_sections:
        if sec.lower() == req:
            return sec

    # 2. Сопоставление по известным синонимам и сокращениям
    aliases = {
        "taxonomy": "Name and taxonomic classification",
        "classification": "Name and taxonomic classification",
        "growth": "Culture and growth conditions",
        "culture": "Culture and growth conditions",
        "conditions": "Culture and growth conditions",
        "metabolism": "Physiology and metabolism",
        "physiology": "Physiology and metabolism",
        "isolation": "Isolation, sampling and environmental information",
        "environment": "Isolation, sampling and environmental information",
        "sampling": "Isolation, sampling and environmental information",
        "safety": "Interaction and safety",
        "pathogenicity": "Interaction and safety",
        "sequence": "Sequence information",
        "sequences": "Sequence information",
        "16s": "Sequence information",
        "genome": "Genome-based predictions",
        "predictions": "Genome-based predictions",
    }
    if req in aliases:
        target = aliases[req]
        if target in available_sections:
            return target

    # 3. Вхождение подстроки
    for sec in available_sections:
        if req in sec.lower():
            return sec

    return None


@mcp.tool()
async def get_strain_details(
    strain_id: Union[int, str],
    section: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieve comprehensive phenotypic and taxonomic details for a specific BacDive strain ID.
    Supports section filtering to drastically reduce token consumption for LLMs.

    Available sections in BacDive records:
      - 'General': General strain description, species epithet, designation
      - 'Name and taxonomic classification': Full taxonomy (domain down to subspecies), LPSN links
      - 'Morphology': Cell shape, size, motility, gram stain, colony morphology
      - 'Culture and growth conditions': Temperatures, medium recipes, pH, oxygen tolerance
      - 'Physiology and metabolism': Enzymes, fermentation, carbon sources, antibiotics
      - 'Isolation, sampling and environmental information': Geographic location, country, habitat, isolation source
      - 'Interaction and safety': Biosafety level (risk group), host associations, pathogenicity
      - 'Sequence information': 16S rRNA gene accessions, genome assembly links, GC-content
      - 'Genome-based predictions': Predicted traits from genome data
      - 'Literature': PubMed citations and bibliographies
      - 'Reference': Data source references within DSMZ

    Args:
        strain_id: The BacDive ID of the strain (e.g. 15259 for Bacillus subtilis, 4907).
        section: Optional section name to return only that part (e.g. 'Morphology', 'Culture and growth conditions', 'taxonomy', 'metabolism').
                 If omitted, returns the entire strain record.

    Returns:
        Structured JSON dictionary with the requested strain data and metadata.
    """
    clean_id = str(strain_id).strip()
    if not clean_id:
        return {"error": True, "message": "strain_id must not be empty"}

    endpoint = f"fetch/{urllib.parse.quote(clean_id)}"
    data = await _make_api_request(endpoint)

    if data.get("error"):
        return data

    results = data.get("results")
    if not results or clean_id not in results:
        # Если results список или пуст
        if isinstance(results, dict) and results:
            # Возьмем первый доступный ключ, если ID числовой или строковый формат разошелся
            actual_id = list(results.keys())[0]
            strain_data = results[actual_id]
        else:
            return {
                "error": True,
                "message": f"Strain with BacDive ID '{clean_id}' was not found in the database.",
                "strain_id": clean_id,
            }
    else:
        strain_data = results[clean_id]

    available_sections = list(strain_data.keys())

    # Если запрошена фильтрация по разделу
    if section and section.strip():
        matched_section = _match_section_name(section, available_sections)
        if not matched_section:
            return {
                "error": True,
                "message": f"Section '{section}' was not found for strain {clean_id}.",
                "requested_section": section,
                "available_sections": available_sections,
                "hint": "Please select one of the available_sections listed above.",
            }

        return {
            "strain_id": clean_id,
            "section": matched_section,
            "data": strain_data[matched_section],
            "available_sections": available_sections,
            "hint": f"Showing section '{matched_section}'. Other sections can be queried via get_strain_details.",
        }

    # Возврат полной карточки
    return {
        "strain_id": clean_id,
        "section": "all",
        "data": strain_data,
        "available_sections": available_sections,
        "hint": "Full record returned. To save context tokens in future calls, specify the 'section' argument.",
    }


# Подготавливаем middleware безопасности
middleware_list = []
if MCP_API_KEY:
    logger.info("Security: API Key protection ENABLED.")
    middleware_list.append(Middleware(AuthMiddleware, api_key=MCP_API_KEY))
else:
    logger.info("Security: MCP_API_KEY is not set. Server is running in PUBLIC mode.")

# Экспортируем Starlette ASGI app для возможности запуска через `uvicorn bacdive_server:app`
app = mcp.http_app(middleware=middleware_list if middleware_list else None)


# --- Точка входа ---
if __name__ == "__main__":
    port = DEFAULT_PORT
    transport = DEFAULT_TRANSPORT

    logger.info("Starting BacDive MCP Server on %s:%d using transport '%s'", SERVER_HOST, port, transport)
    print(f"[*] BacDive MCP Server listening on http://{SERVER_HOST}:{port}")
    print(f"[*] MCP Endpoint: http://{SERVER_HOST}:{port}/mcp (transport={transport})")
    print(f"[*] Health check: http://{SERVER_HOST}:{port}/health")
    if MCP_API_KEY:
        print("[*] Security: Authentication REQUIRED (Bearer token or X-API-Key)")
    else:
        print("[*] Security: Public access (no MCP_API_KEY set)")

    # FastMCP run() поддерживает транспорты 'http', 'sse', 'stdio'
    mcp.run(
        transport=transport,
        host=SERVER_HOST,
        port=port,
        middleware=middleware_list if middleware_list else None,
    )
