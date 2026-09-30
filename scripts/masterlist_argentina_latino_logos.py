#!/usr/bin/env python3
"""
Masterlist Argentina + Premium Latinoamerica - SAFE Logo Manager

OBJETIVO
--------
Generar una copia PNG de los logos de la M3U SIN INVENTAR ASIGNACIONES.

REGLAS
------
1. Los logos que YA existen en la M3U se consideran la fuente de verdad.
   NO se reemplazan por coincidencias aproximadas.
2. Solo se buscan logos automaticamente para entradas SIN tvg-logo.
3. Para entradas sin logo, primero se usa iptv-org mediante coincidencia
   EXACTA de tvg-id/base id.
4. Wikimedia puede actuar como respaldo si el titulo del archivo coincide
   con suficiente precision.
5. Google Images y Bing Images se consultan solo como respaldo y sus
   resultados NO se aceptan automaticamente salvo que cumplan una
   comprobacion estricta del nombre.
6. Si no se puede demostrar una coincidencia suficientemente buena,
   el canal queda SIN LOGO y aparece en el informe para revision manual.
   Es preferible quedar sin logo antes que colocar uno incorrecto.
7. No se modifican URLs de streams.
8. No se modifican #EXTVLCOPT.
9. No se eliminan canales.
10. Los logos descargados se convierten a PNG y se pueden publicar en GitHub.

En la M3U actual del proyecto hay 17 entradas Premium sin tvg-logo:
AMC, AXN, AXN South, Cinecanal, Comedy Central, Disney Channel,
Disney Jr., FX, History 2, History, Lifetime, National Geographic,
Sony Channel, Star Channel, Studio Universal, TNT Novelas y Universal TV.

Dependencias:
    python -m pip install requests Pillow

Uso recomendado:
    python scripts/masterlist_argentina_latino_logos.py \
      --input masterlist_argentina_latino.m3u \
      --output masterlist_argentina_latino_logos.m3u \
      --report masterlist_argentina_latino_logos_report.txt \
      --clean-logo-dir \
      --logo-base-url "https://raw.githubusercontent.com/D3PR3D4DOR/IPTV-Argentina/main/logos"
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import io
import json
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote

import requests
from PIL import Image


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

UA = "IPTV-Argentina-SafeLogoManager/1.0"

IPTV_ORG_CHANNELS = "https://iptv-org.github.io/api/channels.json"
IPTV_ORG_LOGOS = "https://iptv-org.github.io/api/logos.json"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
BING_IMAGES_URL = "https://www.bing.com/images/search"
GOOGLE_IMAGES_URL = "https://www.google.com/search"

GITHUB_TREE_SOURCES = [
    ("tv-logo/tv-logos", "main", "tv-logo"),
    ("logo-tv/tv-logos", "main", "logo-tv"),
    ("hmlendea/tv-logos", "master", "hmlendea"),
]

GITHUB_RAW_BASES = {
    "tv-logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/",
    "logo-tv": "https://raw.githubusercontent.com/logo-tv/tv-logos/main/",
    "hmlendea": "https://raw.githubusercontent.com/hmlendea/tv-logos/master/",
}

SOURCE_PRIORITY = {
    "iptv-org": 100,
    "M3U existente": 98,
    "Wikimedia Commons": 85,
    "tv-logo": 80,
    "logo-tv": 76,
    "hmlendea": 72,
    "Bing Images": 60,
    "Google Images": 58,
}

SOURCE_TIMEOUT = 20
MAX_IMAGE_BYTES = 12 * 1024 * 1024
LOGO_MAX_SIZE = 600
LOGO_PADDING = 18

# Para estos nombres no aceptamos una variante que omita el diferenciador.
DISTINCTIVE_TOKENS = {
    "2", "south", "junior", "jr", "kids", "news", "music",
    "plus", "max", "international", "cartoon", "accion", "terror",
    "classic", "clasico", "novelas",
}


# ---------------------------------------------------------------------------
# MODELOS
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    index: int
    extinf: str
    name: str
    tvg_id: str
    current_logo: str
    stream_url: str
    group: str


@dataclass
class Candidate:
    url: str
    source: str
    score: float
    reason: str


@dataclass
class Result:
    path: Optional[Path] = None
    source: str = ""
    confidence: int = 0
    reason: str = ""
    replaced_existing: bool = False
    white_background: bool = False


# ---------------------------------------------------------------------------
# NORMALIZATION
# ---------------------------------------------------------------------------

def canon(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = value.replace("&", " and ")
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokens(value: str) -> set[str]:
    return set(canon(value).split())


def meaningful_tokens(value: str) -> set[str]:
    stop = {
        "tv", "channel", "canal", "latin", "america", "latinoamerica",
        "latino", "panregional", "argentina", "world", "international",
        "network", "the", "hd", "sd",
    }
    return {x for x in tokens(value) if len(x) >= 2 and x not in stop}


def exact_variant_ok(requested: str, found: str) -> bool:
    req = tokens(requested)
    got = tokens(found)

    if not req or not got:
        return False

    # Los tokens distintivos pedidos deben estar presentes.
    for token in req & DISTINCTIVE_TOKENS:
        if token not in got:
            return False

    # Si el resultado agrega un diferenciador fuerte que no esta pedido,
    # tambien lo rechazamos.
    for token in got & DISTINCTIVE_TOKENS:
        if token not in req:
            return False

    return True


def name_similarity(a: str, b: str) -> float:
    aa = canon(a)
    bb = canon(b)

    if aa == bb and aa:
        return 1.0

    if not aa or not bb:
        return 0.0

    # No usamos fuzzy scoring para decidir por si solo; aqui sirve solamente
    # como una medida secundaria.
    from difflib import SequenceMatcher
    return SequenceMatcher(None, aa, bb).ratio()


def image_filename(name: str, tvg_id: str) -> str:
    base = canon(tvg_id) or canon(name) or "channel"
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:70]
    digest = hashlib.sha1(
        f"{tvg_id}|{name}".encode("utf-8")
    ).hexdigest()[:10]
    return f"{base}-{digest}.png"


# ---------------------------------------------------------------------------
# M3U
# ---------------------------------------------------------------------------

def extract_attr(line: str, attr: str) -> str:
    m = re.search(
        rf'{re.escape(attr)}="([^"]*)"',
        line,
        flags=re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def set_attr(line: str, attr: str, value: str) -> str:
    pattern = rf'{re.escape(attr)}="[^"]*"'
    if re.search(pattern, line, flags=re.IGNORECASE):
        return re.sub(
            pattern,
            f'{attr}="{value}"',
            line,
            count=1,
            flags=re.IGNORECASE,
        )

    comma = line.find(",")
    if comma < 0:
        return line

    return line[:comma] + f' {attr}="{value}"' + line[comma:]


def visible_name(line: str) -> str:
    return line.rsplit(",", 1)[-1].strip() if "," in line else line.strip()


def load_lines(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8-sig")
    return raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def save_lines(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


def parse_entries(lines: list[str]) -> list[Entry]:
    entries: list[Entry] = []

    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF:"):
            continue

        stream_url = ""
        j = i + 1

        while j < len(lines):
            if lines[j].startswith(("http://", "https://")):
                stream_url = lines[j]
                break
            if lines[j].startswith("#EXTINF:"):
                break
            j += 1

        if not stream_url:
            continue

        entries.append(
            Entry(
                index=i,
                extinf=line,
                name=visible_name(line),
                tvg_id=extract_attr(line, "tvg-id"),
                current_logo=extract_attr(line, "tvg-logo"),
                stream_url=stream_url,
                group=extract_attr(line, "group-title"),
            )
        )

    return entries


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def headers(accept: str = "*/*") -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": accept,
        "Cache-Control": "no-cache",
    }


def get_json(session: requests.Session, url: str) -> Any:
    r = session.get(url, timeout=SOURCE_TIMEOUT, headers=headers("application/json"))
    r.raise_for_status()
    return r.json()


def get_bytes(session: requests.Session, url: str) -> bytes:
    r = session.get(
        url,
        timeout=SOURCE_TIMEOUT,
        headers=headers("image/avif,image/webp,image/apng,image/*,*/*;q=0.8"),
        allow_redirects=True,
    )
    r.raise_for_status()

    if len(r.content) > MAX_IMAGE_BYTES:
        raise RuntimeError("imagen demasiado grande")

    return r.content


# ---------------------------------------------------------------------------
# IPT-V ORG EXACTO
# ---------------------------------------------------------------------------

def build_iptv_org_logo_index(
    session: requests.Session,
) -> dict[str, list[dict]]:
    logos = get_json(session, IPTV_ORG_LOGOS)

    if not isinstance(logos, list):
        raise RuntimeError("logos.json no devolvio una lista")

    index: dict[str, list[dict]] = {}

    for item in logos:
        if not isinstance(item, dict):
            continue

        channel = str(item.get("channel", "")).strip()
        url = str(item.get("url", "")).strip()

        if channel and url.startswith("https://"):
            index.setdefault(channel, []).append(item)

    return index


def iptv_org_exact_candidates(
    entry: Entry,
    index: dict[str, list[dict]],
) -> list[Candidate]:
    if not entry.tvg_id:
        return []

    full_id = entry.tvg_id.strip()
    base_id = full_id.split("@", 1)[0].strip()
    feed = full_id.split("@", 1)[1].strip().lower() if "@" in full_id else ""

    candidates: list[Candidate] = []

    for channel_id in dict.fromkeys([full_id, base_id]):
        for item in index.get(channel_id, []):
            url = str(item.get("url", "")).strip()
            if not url.startswith("https://"):
                continue

            item_feed = str(item.get("feed", "")).strip().lower()

            # Exacto por ID. La coincidencia de feed ayuda, pero nunca
            # convierte un ID distinto en una coincidencia valida.
            score = 1.0
            if feed and item_feed == feed:
                score += 0.03

            fmt = str(item.get("format", "")).upper()
            if fmt in {"PNG", "JPEG", "JPG", "WEBP", "GIF", "APNG"}:
                score += 0.02

            candidates.append(
                Candidate(
                    url=url,
                    source="iptv-org",
                    score=min(1.0, score),
                    reason=f"ID exacto: {channel_id}"
                )
            )

    # Quitar duplicados.
    out: list[Candidate] = []
    seen: set[str] = set()

    for c in sorted(candidates, key=lambda x: x.score, reverse=True):
        if c.url in seen:
            continue
        seen.add(c.url)
        out.append(c)

    return out[:8]


# ---------------------------------------------------------------------------
# M3U EXISTENTE
# ---------------------------------------------------------------------------

def existing_logo_candidate(entry: Entry) -> list[Candidate]:
    if not entry.current_logo:
        return []

    return [
        Candidate(
            url=entry.current_logo,
            source="M3U existente",
            score=0.99,
            reason="logo ya presente en la M3U; no se sustituye por fuzzy match",
        )
    ]


# ---------------------------------------------------------------------------
# GITHUB LOGO REPOS - SOLO COINCIDENCIA EXACTA
# ---------------------------------------------------------------------------

def build_github_indexes(
    session: requests.Session,
) -> dict[str, list[str]]:
    indexes: dict[str, list[str]] = {}

    for repo, branch, source in GITHUB_TREE_SOURCES:
        url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"

        try:
            data = get_json(session, url)
        except Exception as exc:
            print(
                f"[!] No se pudo indexar {repo}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue

        paths: list[str] = []
        for item in data.get("tree", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") != "blob":
                continue

            p = str(item.get("path", ""))
            if p.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                paths.append(p)

        indexes[source] = paths
        print(f"[+] {source}: {len(paths)} logos indexados")

    return indexes


def github_exact_candidates(
    entry: Entry,
    indexes: dict[str, list[str]],
) -> list[Candidate]:
    if not entry.tvg_id and not entry.name:
        return []

    wanted = {
        canon(entry.tvg_id),
        canon(entry.tvg_id.split("@", 1)[0]) if entry.tvg_id else "",
        canon(entry.name),
    }
    wanted.discard("")

    result: list[Candidate] = []

    for source, paths in indexes.items():
        base = GITHUB_RAW_BASES[source]

        for path in paths:
            stem = canon(Path(path).stem)
            if stem not in wanted:
                continue

            if not exact_variant_ok(entry.name, Path(path).stem):
                continue

            result.append(
                Candidate(
                    url=base + "/".join(quote(part) for part in path.split("/")),
                    source=source,
                    score=0.96,
                    reason=f"nombre exacto en {source}: {path}",
                )
            )

    result.sort(
        key=lambda c: (
            c.score,
            SOURCE_PRIORITY.get(c.source, 0),
        ),
        reverse=True,
    )

    return result[:8]


# ---------------------------------------------------------------------------
# WEB FALLBACK - ESTRICTO
# ---------------------------------------------------------------------------

CURATED_SEARCHES = {
    "history 2": [
        "History 2 Latin America logo",
        "History2 Latin America logo",
    ],
    "axn south": [
        "AXN South Latin America logo",
        "AXN Latin America South logo",
    ],
    "disney jr": [
        "Disney Junior Latin America logo",
        "Disney Jr Latin America logo",
    ],
    "national geographic": [
        "National Geographic Latin America logo",
    ],
    "sony channel": [
        "Sony Channel Latin America logo",
    ],
    "studio universal": [
        "Studio Universal Latin America logo",
    ],
    "universal tv": [
        "Universal TV Latin America logo",
    ],
    "star channel": [
        "Star Channel Latin America logo",
    ],
    "tnt novelas": [
        "TNT Novelas Latin America logo",
    ],
    "disney channel": [
        "Disney Channel Latin America logo",
    ],
    "comedy central": [
        "Comedy Central Latin America logo",
    ],
    "cinecanal": [
        "Cinecanal Latin America logo",
    ],
    "fx": [
        "FX Latin America logo",
    ],
    "lifetime": [
        "Lifetime Latin America logo",
    ],
    "amc": [
        "AMC Latin America TV channel logo",
    ],
    "history": [
        "History Latin America TV channel logo",
    ],
    "axn": [
        "AXN Latin America TV channel logo",
    ],
}


def web_query(entry: Entry) -> list[str]:
    key = canon(entry.name)
    curated = CURATED_SEARCHES.get(key, [])
    if curated:
        return curated[:2]

    region = "Latin America" if canon(entry.group) == "premium latinoamerica" else "Argentina"
    return [f"{entry.name} TV channel logo {region}"]


def web_result_name_ok(requested: str, found_text: str) -> bool:
    # Para web exigimos coincidencia exacta o tokens distintivos completos.
    req = canon(requested)
    found = canon(found_text)

    if req == found:
        return True

    return exact_variant_ok(requested, found_text)


def wikimedia_search(
    session: requests.Session,
    entry: Entry,
) -> list[Candidate]:
    result: list[Candidate] = []

    for query in web_query(entry):
        try:
            r = session.get(
                COMMONS_API,
                params={
                    "action": "query",
                    "generator": "search",
                    "gsrsearch": query,
                    "gsrnamespace": "6",
                    "gsrlimit": "10",
                    "prop": "imageinfo",
                    "iiprop": "url|mime|size",
                    "iiurlwidth": 800,
                    "format": "json",
                    "formatversion": "2",
                },
                timeout=SOURCE_TIMEOUT,
                headers=headers("application/json"),
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            continue

        for page in data.get("query", {}).get("pages", []):
            title = str(page.get("title", ""))
            title_clean = re.sub(r"^File:\s*", "", title)

            if not web_result_name_ok(entry.name, title_clean):
                continue

            info = page.get("imageinfo") or []
            if not info:
                continue

            url = str(
                info[0].get("thumburl")
                or info[0].get("url")
                or ""
            )

            if not url.startswith("https://"):
                continue

            result.append(
                Candidate(
                    url=url,
                    source="Wikimedia Commons",
                    score=0.94,
                    reason=f"Wikimedia exacto: {title_clean}",
                )
            )

    return dedupe_candidates(result)[:8]


def parse_bing_candidates(
    source_html: str,
    entry: Entry,
) -> list[Candidate]:
    result: list[Candidate] = []

    for m in re.finditer(
        r'class=["\'][^"\']*iusc[^"\']*["\'][^>]*\bm=["\']([^"\']+)',
        source_html,
        flags=re.IGNORECASE,
    ):
        raw = html.unescape(m.group(1)).replace("&quot;", '"')

        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        title = str(
            meta.get("t")
            or meta.get("title")
            or meta.get("purl")
            or ""
        )
        murl = str(meta.get("murl", "")).strip()

        if not murl.startswith(("http://", "https://")):
            continue

        if not web_result_name_ok(entry.name, title):
            continue

        result.append(
            Candidate(
                url=murl,
                source="Bing Images",
                score=0.82,
                reason=f"Bing exacto: {title}",
            )
        )

    return dedupe_candidates(result)[:6]


def bing_search(
    session: requests.Session,
    entry: Entry,
) -> list[Candidate]:
    result: list[Candidate] = []

    for query in web_query(entry):
        try:
            r = session.get(
                BING_IMAGES_URL,
                params={
                    "q": query,
                    "form": "HDRSC2",
                    "first": "1",
                },
                timeout=SOURCE_TIMEOUT,
                headers=headers("text/html,application/xhtml+xml"),
            )
            r.raise_for_status()
        except requests.RequestException:
            continue

        result.extend(parse_bing_candidates(r.text, entry))

    return dedupe_candidates(result)[:8]


def google_search(
    session: requests.Session,
    entry: Entry,
) -> list[Candidate]:
    result: list[Candidate] = []

    for query in web_query(entry):
        try:
            r = session.get(
                GOOGLE_IMAGES_URL,
                params={
                    "tbm": "isch",
                    "q": query,
                },
                timeout=SOURCE_TIMEOUT,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/131 Safari/537.36"
                    ),
                    "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
                },
            )
            r.raise_for_status()
        except requests.RequestException:
            continue

        # Google incluye distintas piezas de metadata. Intentamos recuperar
        # pares de URL + texto cercano. Si el texto no permite validar el
        # nombre, el candidato se descarta.
        src = r.text

        blocks = re.findall(
            r'\{[^{}]{0,2500}(?:"ou"|"original").{0,2500}\}',
            src,
            flags=re.IGNORECASE,
        )

        for block in blocks:
            urls = re.findall(
                r'"(?:ou|original)":"(https?://[^"]+)"',
                block,
                flags=re.IGNORECASE,
            )
            if not urls:
                continue

            text_blob = re.sub(r"[^A-Za-z0-9]+", " ", block)
            if not web_result_name_ok(entry.name, text_blob):
                continue

            for url in urls[:3]:
                result.append(
                    Candidate(
                        url=url,
                        source="Google Images",
                        score=0.80,
                        reason=f"Google exacto: {query}",
                    )
                )

    return dedupe_candidates(result)[:8]


# ---------------------------------------------------------------------------
# IMAGEN
# ---------------------------------------------------------------------------

def rasterize(data: bytes, output: Path) -> None:
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGBA")

        alpha = image.getchannel("A")
        bbox = alpha.getbbox()
        if bbox:
            image = image.crop(bbox)

        image.thumbnail(
            (LOGO_MAX_SIZE, LOGO_MAX_SIZE),
            Image.Resampling.LANCZOS,
        )

        if image.width < 40 or image.height < 20:
            raise RuntimeError("imagen demasiado pequeña")

        padded = Image.new(
            "RGBA",
            (image.width + 36, image.height + 36),
            (0, 0, 0, 0),
        )
        padded.alpha_composite(image, (18, 18))

        output.parent.mkdir(parents=True, exist_ok=True)
        padded.save(output, format="PNG", optimize=True)


def validate_png(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.verify()

    with Image.open(path) as image:
        if image.width < 40 or image.height < 20:
            raise RuntimeError("PNG demasiado pequeño")
        return image.width, image.height


def dark_transparent(path: Path) -> bool:
    with Image.open(path) as source:
        image = source.convert("RGBA")
        values: list[float] = []

        for y in range(0, image.height, 4):
            for x in range(0, image.width, 4):
                r, g, b, a = image.getpixel((x, y))
                if a <= 32:
                    continue
                lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
                values.append(lum)

        if len(values) < 20:
            return False

        mean = sum(values) / len(values)
        dark = sum(1 for v in values if v < 85) / len(values)

        return mean < 115 and dark >= 0.45


def white_background(path: Path) -> None:
    with Image.open(path) as source:
        image = source.convert("RGBA")
        bg = Image.new("RGBA", image.size, (255, 255, 255, 255))
        bg.alpha_composite(image)
        bg.save(path, format="PNG", optimize=True)


# ---------------------------------------------------------------------------
# CANDIDATES
# ---------------------------------------------------------------------------

def dedupe_candidates(items: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []

    for item in sorted(
        items,
        key=lambda x: (
            x.score,
            SOURCE_PRIORITY.get(x.source, 0),
        ),
        reverse=True,
    ):
        if item.url in seen:
            continue
        seen.add(item.url)
        out.append(item)

    return out


def candidate_pools(
    entry: Entry,
    iptv_index: dict[str, list[dict]],
    github_indexes: dict[str, list[str]],
) -> tuple[list[Candidate], list[Candidate]]:
    # IMPORTANT:
    # - existing logo is authoritative
    # - missing logo gets exact structured candidates
    existing = existing_logo_candidate(entry)

    structured: list[Candidate] = []
    structured.extend(iptv_org_exact_candidates(entry, iptv_index))
    structured.extend(github_exact_candidates(entry, github_indexes))

    return dedupe_candidates(existing + structured), dedupe_candidates(structured)


def download_candidate(
    session: requests.Session,
    candidate: Candidate,
    target: Path,
) -> tuple[int, int, bool]:
    data = get_bytes(session, candidate.url)
    rasterize(data, target)

    white = False
    if dark_transparent(target):
        white_background(target)
        white = True

    width, height = validate_png(target)
    return width, height, white


def try_candidates(
    session: requests.Session,
    candidates: list[Candidate],
    target: Path,
    minimum_confidence: int,
) -> Optional[Result]:
    for candidate in candidates:
        try:
            width, height, white = download_candidate(
                session,
                candidate,
                target,
            )

            # La confianza aqui refleja la fuente + exactitud declarada por
            # la funcion que produjo el candidato, no una "adivinanza" visual.
            confidence = int(candidate.score * 100)

            if confidence < minimum_confidence:
                target.unlink(missing_ok=True)
                continue

            return Result(
                path=target,
                source=candidate.source,
                confidence=confidence,
                reason=f"{candidate.reason}; {width}x{height}px",
                replaced_existing=(candidate.source != "M3U existente"),
                white_background=white,
            )

        except Exception:
            target.unlink(missing_ok=True)

    return None


def process_entry(
    entry: Entry,
    iptv_index: dict[str, list[dict]],
    github_indexes: dict[str, list[str]],
    logo_dir: Path,
    web_search: bool,
) -> tuple[int, Result]:
    target = logo_dir / image_filename(entry.name, entry.tvg_id)

    structured_with_existing, structured_missing = candidate_pools(
        entry,
        iptv_index,
        github_indexes,
    )

    session = requests.Session()

    try:
        # EXISTENTE: se conserva. Solo descargamos una copia local.
        if entry.current_logo:
            result = try_candidates(
                session,
                structured_with_existing[:4],
                target,
                minimum_confidence=98,
            )

            if result and result.source == "M3U existente":
                return entry.index, result

            # Si el logo existente no puede descargarse, NO lo sustituimos.
            return entry.index, Result(
                source="M3U existente (no descargable)",
                confidence=0,
                reason=(
                    "El logo existente se conserva conceptualmente, "
                    "pero no se pudo descargar una copia PNG local."
                ),
            )

        # FALTA LOGO: solo coincidencias EXACTAS estructuradas.
        result = try_candidates(
            session,
            structured_missing,
            target,
            minimum_confidence=94,
        )

        if result:
            return entry.index, result

        # RESPALDO WEB: nunca fuzzy.
        if web_search:
            web_candidates: list[Candidate] = []
            web_candidates.extend(wikimedia_search(session, entry))
            web_candidates.extend(bing_search(session, entry))
            web_candidates.extend(google_search(session, entry))

            result = try_candidates(
                session,
                dedupe_candidates(web_candidates),
                target,
                minimum_confidence=78,
            )

            if result:
                return entry.index, result

        return entry.index, Result(
            source="SIN LOGO",
            confidence=0,
            reason=(
                "No hubo una coincidencia exacta suficientemente confiable. "
                "Se deja sin logo para evitar una asignacion incorrecta."
            ),
        )

    finally:
        session.close()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gestor seguro de logos para la masterlist."
    )

    parser.add_argument("--input", default="masterlist_argentina_latino.m3u")
    parser.add_argument(
        "--output",
        default="masterlist_argentina_latino_logos.m3u",
    )
    parser.add_argument(
        "--report",
        default="masterlist_argentina_latino_logos_report.txt",
    )
    parser.add_argument("--logo-dir", default="logos")
    parser.add_argument("--logo-base-url", default="")
    parser.add_argument("--clean-logo-dir", action="store_true")
    parser.add_argument("--no-web-search", action="store_true")
    parser.add_argument("--workers", type=int, default=2)

    args = parser.parse_args()

    if not (1 <= args.workers <= 8):
        parser.error("--workers debe estar entre 1 y 8")

    input_path = Path(args.input)
    output_path = Path(args.output)
    report_path = Path(args.report)
    logo_dir = Path(args.logo_dir)

    if not input_path.exists():
        print(f"[!] No existe la M3U: {input_path}", file=sys.stderr)
        return 1

    if args.clean_logo_dir and logo_dir.exists():
        print(f"[+] Limpiando {logo_dir}...")
        shutil.rmtree(logo_dir)

    lines = load_lines(input_path)
    entries = parse_entries(lines)

    if not entries:
        print("[!] No se encontraron entradas EXTINF.", file=sys.stderr)
        return 1

    total_extinf = sum(
        1 for line in lines if line.startswith("#EXTINF:")
    )

    print()
    print(f"[+] Entradas EXTINF: {total_extinf}")
    print(f"[+] Entradas procesables: {len(entries)}")

    missing = [e for e in entries if not e.current_logo]
    existing = [e for e in entries if e.current_logo]

    print(f"[+] Logos existentes que se conservaran: {len(existing)}")
    print(f"[+] Logos faltantes que se buscaran: {len(missing)}")

    if missing:
        print("[+] Faltantes:")
        for entry in missing:
            print(f"    - {entry.name}")

    session = requests.Session()

    try:
        print("[+] Descargando logos.json de iptv-org...")
        iptv_index = build_iptv_org_logo_index(session)

        print("[+] Indexando repositorios de logos por nombre EXACTO...")
        github_indexes = build_github_indexes(session)
    except Exception as exc:
        print(
            f"[!] No se pudo preparar las fuentes: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        session.close()
        return 1
    finally:
        session.close()

    print("[+] Modo SAFE activo:")
    print("    * existentes: se conservan")
    print("    * faltantes: coincidencia exacta primero")
    print("    * web: solo como respaldo estricto")
    print("    * fuzzy matching: DESACTIVADO")

    results: dict[int, Result] = {}

    def worker(entry: Entry) -> tuple[int, Result]:
        return process_entry(
            entry,
            iptv_index,
            github_indexes,
            logo_dir,
            not args.no_web_search,
        )

    print("[+] Procesando logos...")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as executor:
        futures = [
            executor.submit(worker, entry)
            for entry in entries
        ]

        for done, future in enumerate(
            concurrent.futures.as_completed(futures),
            start=1,
        ):
            index, result = future.result()
            results[index] = result
            entry = next(x for x in entries if x.index == index)

            status = "OK" if result.path else "MANUAL"
            print(
                f"[{done:>3}/{len(entries)}] "
                f"{status:<6} "
                f"{entry.name}"
                + (
                    f" | {result.source} | {result.confidence}%"
                    if result.source
                    else ""
                )
            )

    public_base = args.logo_base_url.rstrip("/")
    report: list[str] = [
        "Masterlist Argentina + Premium Latinoamerica - SAFE Logo Report",
        "=" * 78,
        f"M3U entrada: {input_path}",
        f"M3U salida: {output_path}",
        f"Directorio logos: {logo_dir}",
        "",
        "REGLA: los tvg-logo existentes NO se reemplazan automaticamente.",
        "",
    ]

    ok = 0
    manual = 0
    existing_kept = 0
    backgrounds = 0

    for entry in entries:
        result = results.get(entry.index)

        if result is None:
            manual += 1
            report.append(
                f"❌ MANUAL | {entry.name} | tvg-id={entry.tvg_id}"
            )
            report.append("   sin resultado")
            continue

        if not result.path:
            manual += 1
            report.append(
                f"❌ MANUAL | {entry.name} | tvg-id={entry.tvg_id}"
            )
            report.append(f"   fuente={result.source}")
            report.append(f"   detalle={result.reason}")

            # Si existia un logo, NO tocamos la linea EXTINF.
            continue

        ok += 1

        if entry.current_logo and result.source == "M3U existente":
            existing_kept += 1

        if result.white_background:
            backgrounds += 1

        logo_url = (
            f"{public_base}/{result.path.name}"
            if public_base
            else result.path.as_posix()
        )

        # Solo cambiamos tvg-logo cuando:
        # - faltaba; o
        # - el usuario tenga una futura version que lo solicite explicitamente.
        #
        # En este SAFE manager una entrada existente nunca se sustituye.
        if not entry.current_logo:
            lines[entry.index] = set_attr(
                lines[entry.index],
                "tvg-logo",
                logo_url,
            )

        report.append(
            f"✅ LOGO | {entry.name} | tvg-id={entry.tvg_id}"
        )
        report.append(
            f"   fuente={result.source} | confianza={result.confidence}%"
        )
        report.append(f"   PNG={result.path.as_posix()}")
        report.append(f"   tvg-logo={logo_url}")
        report.append(
            f"   fondo_blanco={'SI' if result.white_background else 'NO'}"
        )
        report.append(f"   detalle={result.reason}")

    after_extinf = sum(
        1 for line in lines if line.startswith("#EXTINF:")
    )

    if total_extinf != after_extinf:
        print(
            f"[!] INTEGRIDAD FALLIDA: antes={total_extinf}, despues={after_extinf}",
            file=sys.stderr,
        )
        return 2

    save_lines(output_path, lines)

    report.extend([
        "",
        "RESUMEN",
        "-" * 78,
        f"EXTINF entrada: {total_extinf}",
        f"EXTINF salida: {after_extinf}",
        f"Resultados PNG: {ok}",
        f"Pendientes de revision manual: {manual}",
        f"Logos existentes conservados: {existing_kept}",
        f"Fondo blanco aplicado: {backgrounds}",
        "",
        "INTEGRIDAD",
        "- URLs de streams: no modificadas.",
        "- #EXTVLCOPT: no modificados.",
        "- Canales: no eliminados.",
        "- Fuzzy matching: DESACTIVADO.",
        "- Logos existentes: no reemplazados.",
    ])

    report_path.write_text(
        "\n".join(report) + "\n",
        encoding="utf-8",
    )

    print()
    print("[+] Proceso terminado.")
    print(f"[+] PNG locales generados: {ok}/{len(entries)}")
    print(f"[+] Pendientes de revision: {manual}")
    print(f"[+] Logos existentes conservados: {existing_kept}")
    print(f"[+] M3U salida: {output_path}")
    print(f"[+] Informe: {report_path}")
    print(f"[+] Logos: {logo_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
