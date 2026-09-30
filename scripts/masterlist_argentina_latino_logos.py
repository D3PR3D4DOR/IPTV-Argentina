#!/usr/bin/env python3
"""
Masterlist Argentina + Premium Latinoamerica - Logo Manager

Objetivo:
- procesar TODOS los canales de una M3U;
- descargar una copia local PNG de cada logo;
- conservar canales, URLs, tvg-id y #EXTVLCOPT sin cambios;
- buscar logos en varias fuentes antes de marcar un canal como no encontrado;
- priorizar PNG adecuados para interfaces IPTV oscuras;
- generar una M3U de salida y un informe detallado.

Fuentes de logo, en este orden general:
1. tv-logo/tv-logos
2. Fourqui/tv
3. logo-tv/tv-logos
4. hmlendea/tv-logos
5. iptv-org
6. logo actual de la M3U
7. Wikimedia Commons
8. Bing Images
9. Google Images

El orden real puede cambiar por coincidencia y disponibilidad.

IMPORTANTE:
- El script NO cambia ninguna URL de stream.
- NO elimina canales.
- NO modifica #EXTVLCOPT.
- Los logos se guardan localmente como PNG.
- Con --logo-base-url la M3U queda preparada para apuntar a GitHub.
- Con --clean-logo-dir se elimina la carpeta de logos al comenzar,
  evitando duplicados de ejecuciones anteriores.

Dependencias:
    pip install requests Pillow

Uso recomendado:
    python3 scripts/masterlist_argentina_latino_logos.py \
      --input masterlist_argentina_latino.m3u \
      --output masterlist_argentina_latino_logos.m3u \
      --report masterlist_argentina_latino_logos_report.txt \
      --clean-logo-dir \
      --logo-base-url "https://raw.githubusercontent.com/D3PR3D4DOR/IPTV-Argentina/main/logos"
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import io
import json
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus, unquote, urljoin

import requests
from PIL import Image


# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

UA = "Masterlist-Argentina-LATAM-LogoManager/2.0"

IPTV_ORG_CHANNELS = "https://iptv-org.github.io/api/channels.json"
IPTV_ORG_LOGOS = "https://iptv-org.github.io/api/logos.json"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

GITHUB_TREE_SOURCES = [
    ("tv-logo/tv-logos", "main", "tv-logo"),
    ("Fourqui/tv", "main", "fourqui"),
    ("logo-tv/tv-logos", "main", "logo-tv"),
    ("hmlendea/tv-logos", "master", "hmlendea"),
]

BING_IMAGES_URL = "https://www.bing.com/images/search"
GOOGLE_IMAGES_URL = "https://www.google.com/search"

SOURCE_TIMEOUT = 20
SEARCH_TIMEOUT = 20
LOGO_THUMB_WIDTH = 800
LOGO_MAX_SIZE = 600
LOGO_PADDING = 18
MAX_IMAGE_BYTES = 12 * 1024 * 1024

# Fuentes locales tienen prioridad porque permiten almacenar nosotros mismos
# una copia PNG y evitan depender de un servidor externo.
SOURCE_PRIORITY = {
    "tv-logo": 100,
    "fourqui": 96,
    "logo-tv": 92,
    "hmlendea": 88,
    "iptv-org": 84,
    "m3u": 80,
    "Wikimedia Commons": 72,
    "Bing Images": 62,
    "Google Images": 58,
}

RASTER_FORMATS = {
    "PNG", "JPEG", "JPG", "WEBP", "GIF", "AVIF", "APNG"
}

REGION_MARKERS = (
    "argentina",
    "latin america",
    "latinamerica",
    "latinoamerica",
    "latinoamérica",
    "world latin america",
    "panregional",
    "andes",
    "south",
    "chile",
    "mexico",
    "central america",
    "america latina",
    "américa latina",
)

# ---------------------------------------------------------------------------
# MODELOS
# ---------------------------------------------------------------------------

@dataclass
class ChannelEntry:
    index: int
    extinf: str
    url: str
    tvg_id: str
    name: str
    group: str
    current_logo: str


@dataclass
class LogoCandidate:
    url: str
    source: str
    match: float
    reason: str = ""
    feed: str = ""


@dataclass
class LogoResult:
    path: Optional[Path] = None
    url: str = ""
    source: str = ""
    confidence: int = 0
    reason: str = ""
    white_background: bool = False


# ---------------------------------------------------------------------------
# NORMALIZACION
# ---------------------------------------------------------------------------

def canon(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokenize(value: str) -> set[str]:
    stop = {
        "tv", "channel", "canal", "latin", "america", "latinoamerica",
        "latino", "panregional", "south", "andes", "hd", "sd", "the",
        "argentina", "world", "international", "network",
    }
    return {
        token
        for token in canon(value).split()
        if len(token) >= 2 and token not in stop
    }


def name_similarity(a: str, b: str) -> float:
    aa = canon(a)
    bb = canon(b)

    if not aa or not bb:
        return 0.0

    score = SequenceMatcher(None, aa, bb).ratio()

    at = tokenize(a)
    bt = tokenize(b)
    if at and bt:
        overlap = len(at & bt) / max(len(at), len(bt))
        score = max(score, overlap)

    if aa == bb:
        score = 1.0

    return score


def region_score(value: str) -> float:
    n = canon(value)
    score = 0.0

    for marker in REGION_MARKERS:
        if canon(marker) in n:
            score += 0.03

    return min(score, 0.15)


def image_slug(name: str, tvg_id: str) -> str:
    base = canon(tvg_id) or canon(name) or "channel"
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    digest = SequenceMatcher(None, name, tvg_id).ratio()
    suffix = f"{int(digest * 1_000_000):06d}"
    return f"{base[:70]}-{suffix}.png"


def extract_attr(line: str, attr: str) -> str:
    match = re.search(
        rf'{re.escape(attr)}="([^"]*)"',
        line,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else extinf.strip()


def set_attr(line: str, attr: str, value: str) -> str:
    pattern = rf'{re.escape(attr)}="[^"]*"'

    if re.search(pattern, line, flags=re.IGNORECASE):
        return re.sub(
            pattern,
            f'{attr}="{value.replace(chr(34), "%22")}"',
            line,
            count=1,
            flags=re.IGNORECASE,
        )

    comma = line.find(",")
    if comma < 0:
        return line

    return line[:comma] + f' {attr}="{value}"' + line[comma:]


# ---------------------------------------------------------------------------
# M3U
# ---------------------------------------------------------------------------

def load_m3u(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8-sig")
    return raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def save_m3u(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"

    path.write_bytes(
        text.replace("\n", "\r\n").encode("utf-8")
    )


def parse_entries(lines: list[str]) -> list[ChannelEntry]:
    entries: list[ChannelEntry] = []
    pending_index: Optional[int] = None
    pending_extinf: Optional[str] = None

    for index, line in enumerate(lines):
        if line.startswith("#EXTINF:"):
            pending_index = index
            pending_extinf = line
            continue

        if (
            pending_extinf is not None
            and line.startswith(("http://", "https://"))
        ):
            tvg_id = extract_attr(pending_extinf, "tvg-id")
            group = extract_attr(pending_extinf, "group-title")
            name = visible_name(pending_extinf)
            current_logo = extract_attr(pending_extinf, "tvg-logo")

            entries.append(
                ChannelEntry(
                    index=pending_index if pending_index is not None else index,
                    extinf=pending_extinf,
                    url=line,
                    tvg_id=tvg_id,
                    name=name,
                    group=group,
                    current_logo=current_logo,
                )
            )

            pending_index = None
            pending_extinf = None
            continue

        if line.startswith("#"):
            continue

        pending_index = None
        pending_extinf = None

    return entries


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def session_headers(accept: str = "*/*") -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": accept,
        "Cache-Control": "no-cache",
    }


def get_json(session: requests.Session, url: str) -> Any:
    response = session.get(
        url,
        timeout=SOURCE_TIMEOUT,
        headers=session_headers("application/json"),
    )
    response.raise_for_status()
    return response.json()


def get_bytes(session: requests.Session, url: str) -> bytes:
    response = session.get(
        url,
        timeout=SOURCE_TIMEOUT,
        headers=session_headers(
            "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        ),
        allow_redirects=True,
    )
    response.raise_for_status()

    if len(response.content) > MAX_IMAGE_BYTES:
        raise RuntimeError("imagen demasiado grande")

    return response.content


# ---------------------------------------------------------------------------
# IPTVP-ORG
# ---------------------------------------------------------------------------

def build_iptv_org_indexes(
    session: requests.Session,
) -> tuple[dict[str, list[dict]], list[dict]]:
    print("[+] Descargando channels.json de iptv-org...")
    channels = get_json(session, IPTV_ORG_CHANNELS)

    print("[+] Descargando logos.json de iptv-org...")
    logos = get_json(session, IPTV_ORG_LOGOS)

    if not isinstance(channels, list):
        raise RuntimeError("channels.json no devolvio una lista")
    if not isinstance(logos, list):
        raise RuntimeError("logos.json no devolvio una lista")

    index: dict[str, list[dict]] = {}

    for item in logos:
        if not isinstance(item, dict):
            continue

        channel = str(item.get("channel", "")).strip()
        if channel:
            index.setdefault(channel, []).append(item)

    return index, [
        item for item in channels
        if isinstance(item, dict)
    ]


def logo_rank(item: dict, preferred_feed: str = "") -> tuple:
    fmt = str(item.get("format", "")).upper()
    raster = 1 if fmt in RASTER_FORMATS else 0

    tags = {
        str(tag).strip().lower()
        for tag in (item.get("tags") or [])
        if tag
    }

    visible = 0
    if "white" in tags or "light" in tags:
        visible += 30
    if "horizontal" in tags:
        visible += 8
    if "black" in tags or "dark" in tags:
        visible -= 25

    feed = str(item.get("feed") or "").strip().lower()
    feed_match = 1 if preferred_feed and feed == preferred_feed.lower() else 0
    in_use = 1 if item.get("in_use") is True else 0

    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)

    return (
        raster,
        visible,
        feed_match,
        in_use,
        width,
        height,
    )


def choose_iptv_logo(
    tvg_id: str,
    channel_index: dict[str, list[dict]],
) -> list[LogoCandidate]:
    if not tvg_id:
        return []

    base = tvg_id.split("@", 1)[0].strip()
    feed = tvg_id.split("@", 1)[1].strip() if "@" in tvg_id else ""

    candidates: list[LogoCandidate] = []

    for channel_id in (tvg_id, base):
        items = channel_index.get(channel_id, [])
        items = sorted(
            items,
            key=lambda item: logo_rank(item, feed),
            reverse=True,
        )

        for item in items[:5]:
            url = str(item.get("url", "")).strip()
            if not url.startswith("https://"):
                continue

            fmt = str(item.get("format", "")).upper()
            score = 1.0 if fmt in RASTER_FORMATS else 0.97

            tags = " ".join(
                str(x) for x in (item.get("tags") or [])
            )
            score += region_score(f"{channel_id} {feed} {tags}")

            candidates.append(
                LogoCandidate(
                    url=url,
                    source="iptv-org",
                    match=min(1.0, score),
                    reason=f"match exacto tvg-id={channel_id}",
                    feed=feed,
                )
            )

    return candidates


# ---------------------------------------------------------------------------
# GITHUB LOGO REPOSITORIES
# ---------------------------------------------------------------------------

def build_github_logo_index(
    session: requests.Session,
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}

    for repo, branch, source in GITHUB_TREE_SOURCES:
        url = (
            f"https://api.github.com/repos/{repo}"
            f"/git/trees/{branch}?recursive=1"
        )

        try:
            data = get_json(session, url)
        except Exception as exc:
            print(
                f"[!] No se pudo indexar {repo}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue

        tree = data.get("tree", [])
        paths: list[str] = []

        if isinstance(tree, list):
            for item in tree:
                if not isinstance(item, dict):
                    continue

                if item.get("type") != "blob":
                    continue

                path = str(item.get("path", ""))
                if path.lower().endswith((
                    ".png", ".jpg", ".jpeg", ".webp"
                )):
                    paths.append(path)

        result[source] = paths
        print(f"[+] {source}: {len(paths)} logos raster indexados")

    return result


def github_path_score(
    name: str,
    tvg_id: str,
    path: str,
) -> float:
    # Solo comparamos con el nombre del archivo y una parte de la ruta.
    filename = Path(path).stem
    score = name_similarity(name, filename)

    text = canon(path)
    score = min(
        1.0,
        score + region_score(text),
    )

    query_tokens = tokenize(name)
    path_tokens = tokenize(path)

    if query_tokens and path_tokens:
        overlap = len(query_tokens & path_tokens) / max(
            len(query_tokens),
            len(path_tokens),
        )
        score = max(score, overlap)

    # Ayuda a separar variantes argentinas de variantes internacionales.
    if ".ar" in canon(tvg_id):
        if "argentina" in text:
            score = min(1.0, score + 0.08)

    return score


def github_logo_candidates(
    name: str,
    tvg_id: str,
    indexes: dict[str, list[str]],
) -> list[LogoCandidate]:
    result: list[LogoCandidate] = []

    source_priority = {
        "tv-logo": 100,
        "fourqui": 96,
        "logo-tv": 92,
        "hmlendea": 88,
    }

    raw_base = {
        "tv-logo": "https://raw.githubusercontent.com/tv-logo/tv-logos/main/",
        "fourqui": "https://raw.githubusercontent.com/Fourqui/tv/main/",
        "logo-tv": "https://raw.githubusercontent.com/logo-tv/tv-logos/main/",
        "hmlendea": "https://raw.githubusercontent.com/hmlendea/tv-logos/master/",
    }

    for source, paths in indexes.items():
        ranked = sorted(
            (
                (
                    github_path_score(name, tvg_id, path),
                    path,
                )
                for path in paths
            ),
            reverse=True,
        )

        for score, path in ranked[:8]:
            if score < 0.48:
                continue

            result.append(
                LogoCandidate(
                    url=raw_base[source] + quote_path(path),
                    source=source,
                    match=min(1.0, score),
                    reason=f"coincidencia en {source}: {path}",
                )
            )

    result.sort(
        key=lambda item: (
            item.match,
            source_priority.get(item.source, 0),
        ),
        reverse=True,
    )

    return result[:12]


def quote_path(path: str) -> str:
    return "/".join(
        quote_plus(part).replace("+", "%20")
        for part in path.split("/")
    )


# ---------------------------------------------------------------------------
# WIKIMEDIA
# ---------------------------------------------------------------------------

def wikimedia_search(
    session: requests.Session,
    name: str,
    premium: bool,
) -> list[LogoCandidate]:
    extra = " Latin America" if premium else " Argentina"
    query = f'"{name}" television channel logo{extra}'

    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": "12",
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": LOGO_THUMB_WIDTH,
        "format": "json",
        "formatversion": "2",
    }

    try:
        response = session.get(
            COMMONS_API,
            params=params,
            timeout=SEARCH_TIMEOUT,
            headers=session_headers("application/json"),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    pages = data.get("query", {}).get("pages", [])
    if not isinstance(pages, list):
        return []

    result: list[LogoCandidate] = []

    for page in pages:
        title = str(page.get("title", ""))
        info = page.get("imageinfo") or []

        if not info:
            continue

        image = info[0]
        url = str(
            image.get("thumburl")
            or image.get("url")
            or ""
        )

        if not url.startswith("https://"):
            continue

        title_norm = canon(title)
        score = name_similarity(name, title)

        if "logo" in title_norm or "wordmark" in title_norm:
            score += 0.08

        result.append(
            LogoCandidate(
                url=url,
                source="Wikimedia Commons",
                match=min(1.0, score),
                reason=f"Wikimedia: {title}",
            )
        )

    result.sort(key=lambda item: item.match, reverse=True)
    return result[:8]


# ---------------------------------------------------------------------------
# GOOGLE / BING IMAGE SEARCH
# ---------------------------------------------------------------------------

def decode_escaped_url(value: str) -> str:
    value = html.unescape(value)
    value = value.replace("\\/", "/")
    value = value.replace('\\"', '"')

    try:
        value = bytes(value, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        pass

    return value


def bing_image_search(
    session: requests.Session,
    name: str,
    premium: bool,
) -> list[LogoCandidate]:
    region = "Latin America" if premium else "Argentina"
    query = f"{name} TV channel logo {region}"

    try:
        response = session.get(
            BING_IMAGES_URL,
            params={
                "q": query,
                "form": "HDRSC2",
                "first": "1",
            },
            timeout=SEARCH_TIMEOUT,
            headers=session_headers("text/html,application/xhtml+xml"),
        )
        response.raise_for_status()
        source = html.unescape(response.text)
    except requests.RequestException:
        return []

    urls: list[str] = []

    # Bing coloca la metadata de cada resultado en atributos "m".
    for match in re.finditer(
        r'class=["\'][^"\']*iusc[^"\']*["\'][^>]*\bm=["\']([^"\']+)',
        source,
        flags=re.IGNORECASE,
    ):
        raw = match.group(1)

        try:
            metadata = json.loads(
                html.unescape(raw.replace("&quot;", '"'))
            )
        except (json.JSONDecodeError, TypeError):
            continue

        murl = str(metadata.get("murl", "")).strip()
        if murl.startswith(("http://", "https://")):
            urls.append(murl)

    # Fallback para cambios de HTML.
    if not urls:
        for match in re.findall(
            r'"murl":"(https?://[^"]+)"',
            source,
            flags=re.IGNORECASE,
        ):
            urls.append(decode_escaped_url(match))

    result: list[LogoCandidate] = []

    for url in unique(urls)[:10]:
        path_text = unquote(url)
        score = name_similarity(name, path_text)
        result.append(
            LogoCandidate(
                url=url,
                source="Bing Images",
                match=min(1.0, score + 0.45),
                reason=f"Bing Images: {query}",
            )
        )

    return result[:8]


def google_image_search(
    session: requests.Session,
    name: str,
    premium: bool,
) -> list[LogoCandidate]:
    region = "Latin America" if premium else "Argentina"
    query = f"{name} TV channel logo {region}"

    try:
        response = session.get(
            GOOGLE_IMAGES_URL,
            params={
                "tbm": "isch",
                "q": query,
            },
            timeout=SEARCH_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/131 Safari/537.36"
                ),
                "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
            },
        )
        response.raise_for_status()
        source = response.text
    except requests.RequestException:
        return []

    urls: list[str] = []

    # Google Images suele incluir la URL original como "ou".
    for match in re.findall(
        r'"ou":"(https?://[^"]+)"',
        source,
        flags=re.IGNORECASE,
    ):
        urls.append(decode_escaped_url(match))

    # Algunos diseños usan "original".
    for match in re.findall(
        r'"original":"(https?://[^"]+)"',
        source,
        flags=re.IGNORECASE,
    ):
        urls.append(decode_escaped_url(match))

    result: list[LogoCandidate] = []

    for url in unique(urls)[:12]:
        path_text = unquote(url)
        score = name_similarity(name, path_text)

        result.append(
            LogoCandidate(
                url=url,
                source="Google Images",
                match=min(1.0, score + 0.43),
                reason=f"Google Images: {query}",
            )
        )

    return result[:8]


# ---------------------------------------------------------------------------
# IMAGEN / PNG
# ---------------------------------------------------------------------------

def rasterize_image(
    data: bytes,
    output_path: Path,
) -> bool:
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
            raise RuntimeError("imagen demasiado pequeña para ser un logo")

        # Añadimos margen transparente.
        padded = Image.new(
            "RGBA",
            (
                image.width + LOGO_PADDING * 2,
                image.height + LOGO_PADDING * 2,
            ),
            (0, 0, 0, 0),
        )
        padded.alpha_composite(
            image,
            (LOGO_PADDING, LOGO_PADDING),
        )
        image = padded

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Siempre PNG real.
        image.save(
            output_path,
            format="PNG",
            optimize=True,
        )

    return True


def detect_dark_logo_on_transparent(path: Path) -> bool:
    """
    Detecta logos predominantemente oscuros sobre transparencia.
    Estos pueden resultar invisibles en clientes IPTV con fondo oscuro.

    La correccion se aplica solo cuando:
    - hay suficiente contenido visible;
    - la mayoria de los pixels visibles son oscuros.
    """
    with Image.open(path) as source:
        image = source.convert("RGBA")
        alpha = image.getchannel("A")
        visible = []

        for y in range(0, image.height, 4):
            for x in range(0, image.width, 4):
                a = alpha.getpixel((x, y))
                if a <= 32:
                    continue

                r, g, b, _ = image.getpixel((x, y))
                luminance = (
                    0.2126 * r
                    + 0.7152 * g
                    + 0.0722 * b
                )
                visible.append(luminance)

        if len(visible) < 20:
            return False

        mean = sum(visible) / len(visible)
        dark = sum(1 for value in visible if value < 85) / len(visible)

        return mean < 115 and dark >= 0.45


def apply_white_background(path: Path) -> None:
    with Image.open(path) as source:
        image = source.convert("RGBA")

        background = Image.new(
            "RGBA",
            image.size,
            (255, 255, 255, 255),
        )
        background.alpha_composite(image)

        background.save(
            path,
            format="PNG",
            optimize=True,
        )


def validate_png(path: Path) -> tuple[bool, int, int]:
    try:
        with Image.open(path) as image:
            image.verify()

        with Image.open(path) as image:
            return True, image.width, image.height
    except Exception:
        return False, 0, 0


# ---------------------------------------------------------------------------
# CANDIDATOS
# ---------------------------------------------------------------------------

def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for value in items:
        if value and value not in seen:
            seen.add(value)
            result.append(value)

    return result


def current_logo_candidate(entry: ChannelEntry) -> list[LogoCandidate]:
    if not entry.current_logo:
        return []

    return [
        LogoCandidate(
            url=entry.current_logo,
            source="m3u",
            match=0.85,
            reason="logo existente en la M3U",
        )
    ]


def candidate_score(
    candidate: LogoCandidate,
    image_width: int,
    image_height: int,
) -> float:
    source_base = SOURCE_PRIORITY.get(candidate.source, 50) / 100.0

    size_bonus = 0.0
    largest = max(image_width, image_height)
    if largest >= 300:
        size_bonus = 0.05
    elif largest >= 150:
        size_bonus = 0.02

    ratio = image_width / max(image_height, 1)
    ratio_bonus = 0.02 if 1.1 <= ratio <= 5.5 else 0.0

    return (
        candidate.match * 0.60
        + source_base * 0.32
        + size_bonus
        + ratio_bonus
    )


def find_best_local_logo(
    entry: ChannelEntry,
    channel_index: dict[str, list[dict]],
    github_indexes: dict[str, list[str]],
    session: requests.Session,
    allow_web_search: bool,
    candidate_limit: int,
) -> LogoCandidate | None:
    premium = canon(entry.group) == "premium latinoamerica"

    candidates: list[LogoCandidate] = []

    candidates.extend(
        choose_iptv_logo(
            entry.tvg_id,
            channel_index,
        )
    )
    candidates.extend(
        github_logo_candidates(
            entry.name,
            entry.tvg_id,
            github_indexes,
        )
    )
    candidates.extend(
        current_logo_candidate(entry)
    )

    # Ordenamos y probamos primero las fuentes estructuradas.
    candidates.sort(
        key=lambda item: (
            SOURCE_PRIORITY.get(item.source, 50),
            item.match,
        ),
        reverse=True,
    )

    if allow_web_search and not candidates:
        candidates.extend(
            wikimedia_search(session, entry.name, premium)
        )
        candidates.extend(
            bing_image_search(session, entry.name, premium)
        )
        candidates.extend(
            google_image_search(session, entry.name, premium)
        )

    # Aunque haya candidatos estructurados, si todos fallan al descargar,
    # el llamador volvera a intentar las busquedas web.
    return candidates[:candidate_limit][0] if candidates else None


def all_candidates_for_entry(
    entry: ChannelEntry,
    channel_index: dict[str, list[dict]],
    github_indexes: dict[str, list[str]],
    session: requests.Session,
    web_search: bool,
) -> list[LogoCandidate]:
    premium = canon(entry.group) == "premium latinoamerica"

    candidates: list[LogoCandidate] = []
    candidates.extend(choose_iptv_logo(entry.tvg_id, channel_index))
    candidates.extend(
        github_logo_candidates(
            entry.name,
            entry.tvg_id,
            github_indexes,
        )
    )
    candidates.extend(current_logo_candidate(entry))

    candidates.sort(
        key=lambda item: (
            item.match,
            SOURCE_PRIORITY.get(item.source, 50),
        ),
        reverse=True,
    )

    if web_search:
        # Las búsquedas web se hacen después de las fuentes estructuradas.
        candidates.extend(
            wikimedia_search(session, entry.name, premium)
        )
        candidates.extend(
            bing_image_search(session, entry.name, premium)
        )
        candidates.extend(
            google_image_search(session, entry.name, premium)
        )

    # Deduplicar por URL.
    output: list[LogoCandidate] = []
    seen: set[str] = set()

    for candidate in candidates:
        if candidate.url in seen:
            continue

        seen.add(candidate.url)
        output.append(candidate)

    return output[:30]


def save_candidate_image(
    session: requests.Session,
    candidate: LogoCandidate,
    target: Path,
) -> tuple[bool, int, int, bool]:
    data = get_bytes(session, candidate.url)

    # Primero se intenta con Pillow.
    rasterize_image(data, target)

    white_background = False

    if detect_dark_logo_on_transparent(target):
        apply_white_background(target)
        white_background = True

    ok, width, height = validate_png(target)

    if not ok:
        raise RuntimeError("PNG generado no valido")

    return True, width, height, white_background


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Descarga logos PNG de todos los canales de una M3U."
    )

    parser.add_argument(
        "--input",
        default="masterlist_argentina_latino.m3u",
    )
    parser.add_argument(
        "--output",
        default="masterlist_argentina_latino_logos.m3u",
    )
    parser.add_argument(
        "--report",
        default="masterlist_argentina_latino_logos_report.txt",
    )
    parser.add_argument(
        "--logo-dir",
        default="logos",
    )
    parser.add_argument(
        "--logo-base-url",
        default="",
        help="URL base publica para los PNG locales",
    )
    parser.add_argument(
        "--clean-logo-dir",
        action="store_true",
        help="elimina la carpeta logos antes de comenzar",
    )
    parser.add_argument(
        "--no-web-search",
        action="store_true",
        help="no consultar Wikimedia/Bing/Google Images",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="descargas simultaneas de logos (default: 4)",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=12,
        help="maximo de candidatos estructurados por canal",
    )

    args = parser.parse_args()

    if not (1 <= args.workers <= 12):
        parser.error("--workers debe estar entre 1 y 12")

    if not (2 <= args.candidate_limit <= 30):
        parser.error("--candidate-limit debe estar entre 2 y 30")

    input_path = Path(args.input)
    output_path = Path(args.output)
    report_path = Path(args.report)
    logo_dir = Path(args.logo_dir)

    if not input_path.exists():
        print(
            f"[!] No existe la M3U: {input_path}",
            file=sys.stderr,
        )
        return 1

    if args.clean_logo_dir and logo_dir.exists():
        print(f"[+] Limpiando carpeta {logo_dir}...")
        shutil.rmtree(logo_dir)

    lines = load_m3u(input_path)
    entries = parse_entries(lines)

    if not entries:
        print("[!] No se encontraron entradas EXTINF.", file=sys.stderr)
        return 1

    print()
    print(f"[+] Entradas EXTINF: {len(entries)}")

    session = requests.Session()

    try:
        channel_index, channels = build_iptv_org_indexes(session)
    except Exception as exc:
        print(
            f"[!] No se pudo cargar iptv-org: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        session.close()
        return 1

    github_indexes = build_github_logo_index(session)

    # -----------------------------------------------------------------------
    # Primero armamos el conjunto de candidatos. Las busquedas web se hacen
    # por canal en paralelo despues de tener las fuentes estructuradas.
    # -----------------------------------------------------------------------
    web_search = not args.no_web_search

    candidate_map: dict[int, list[LogoCandidate]] = {}

    def build_for_entry(
        entry: ChannelEntry,
    ) -> tuple[int, list[LogoCandidate]]:
        local_session = requests.Session()
        try:
            candidates = all_candidates_for_entry(
                entry,
                channel_index,
                github_indexes,
                local_session,
                web_search,
            )
            return entry.index, candidates
        finally:
            local_session.close()

    print("[+] Buscando candidatos para los canales...")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as executor:
        futures = [
            executor.submit(build_for_entry, entry)
            for entry in entries
        ]

        for future in concurrent.futures.as_completed(futures):
            index, candidates = future.result()
            candidate_map[index] = candidates

    # -----------------------------------------------------------------------
    # Descargar y validar. Se hace con pocas conexiones simultaneas para no
    # saturar hosts de logos.
    # -----------------------------------------------------------------------
    results: dict[int, LogoResult] = {}
    counts: dict[str, int] = {}
    failures: dict[int, list[str]] = {}

    def process_entry(entry: ChannelEntry) -> tuple[int, LogoResult]:
        candidates = candidate_map.get(entry.index, [])
        target = logo_dir / image_slug(
            entry.name,
            entry.tvg_id,
        )

        local_session = requests.Session()

        try:
            # Prioridad dinámica:
            # match -> fuente -> tamaño.
            ordered = sorted(
                candidates,
                key=lambda item: (
                    item.match,
                    SOURCE_PRIORITY.get(item.source, 50),
                ),
                reverse=True,
            )

            # Primera pasada: candidatos estructurados y logo actual.
            tried: list[str] = []

            for candidate in ordered[:args.candidate_limit]:
                try:
                    (
                        ok,
                        width,
                        height,
                        white_background,
                    ) = save_candidate_image(
                        local_session,
                        candidate,
                        target,
                    )

                    if not ok:
                        continue

                    score = candidate_score(
                        candidate,
                        width,
                        height,
                    )

                    result = LogoResult(
                        path=target,
                        source=candidate.source,
                        confidence=min(
                            99,
                            max(
                                1,
                                int(score * 100),
                            ),
                        ),
                        reason=(
                            f"{candidate.reason}; "
                            f"{width}x{height}px"
                        ),
                        white_background=white_background,
                    )

                    return entry.index, result

                except Exception as exc:
                    tried.append(
                        f"{candidate.source}: "
                        f"{type(exc).__name__}"
                    )

            return entry.index, LogoResult(
                source="NO ENCONTRADO",
                confidence=0,
                reason=(
                    "Ninguna fuente produjo una imagen valida. "
                    + "; ".join(tried[:8])
                ),
            )

        finally:
            local_session.close()

    print("[+] Descargando y normalizando logos...")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as executor:
        futures = [
            executor.submit(process_entry, entry)
            for entry in entries
        ]

        for done, future in enumerate(
            concurrent.futures.as_completed(futures),
            start=1,
        ):
            index, result = future.result()
            results[index] = result

            state = "OK" if result.path else "FAIL"
            print(
                f"[{done:>3}/{len(entries)}] "
                f"{state:<4} "
                f"{entries[[x.index for x in entries].index(index)].name}"
            )

    # -----------------------------------------------------------------------
    # Aplicar referencias locales / URL publicas y generar informe.
    # -----------------------------------------------------------------------
    total = len(entries)
    success = 0
    unresolved = 0
    backgrounds = 0

    report_lines = [
        "Masterlist Argentina + Premium Latinoamerica - Logo Report",
        "=" * 72,
        f"M3U entrada: {input_path}",
        f"M3U salida: {output_path}",
        f"Directorio logos: {logo_dir}",
        "",
    ]

    public_base = args.logo_base_url.rstrip("/")

    entry_by_index = {entry.index: entry for entry in entries}

    for entry in entries:
        result = results.get(
            entry.index,
            LogoResult(
                source="NO ENCONTRADO",
                reason="Sin resultado.",
            ),
        )

        if not result.path:
            unresolved += 1

            report_lines.append(
                f"❌ SIN LOGO | {entry.name} | tvg-id={entry.tvg_id}"
            )
            report_lines.append(
                f"   detalle={result.reason}"
            )
            continue

        success += 1

        if result.white_background:
            backgrounds += 1

        filename = result.path.name

        if public_base:
            logo_ref = f"{public_base}/{filename}"
        else:
            logo_ref = f"{logo_dir.as_posix()}/{filename}"

        lines[entry.index] = set_attr(
            lines[entry.index],
            "tvg-logo",
            logo_ref,
        )

        report_lines.append(
            f"✅ LOGO | {entry.name} | tvg-id={entry.tvg_id}"
        )
        report_lines.append(
            f"   fuente={result.source} | confianza={result.confidence}%"
        )
        report_lines.append(
            f"   PNG={result.path.as_posix()}"
        )
        report_lines.append(
            f"   tvg-logo={logo_ref}"
        )
        report_lines.append(
            f"   fondo_blanco={'SI' if result.white_background else 'NO'}"
        )
        report_lines.append(
            f"   detalle={result.reason}"
        )

        counts[result.source] = counts.get(result.source, 0) + 1

    # -----------------------------------------------------------------------
    # Integridad
    # -----------------------------------------------------------------------
    before_extinf = len(entries)
    after_extinf = sum(
        1 for line in lines
        if line.startswith("#EXTINF:")
    )

    if before_extinf != after_extinf:
        print(
            f"[!] ERROR DE INTEGRIDAD: "
            f"antes={before_extinf} despues={after_extinf}",
            file=sys.stderr,
        )
        session.close()
        return 2

    save_m3u(output_path, lines)

    report_lines.extend([
        "",
        "RESUMEN",
        "-" * 72,
        f"Entradas EXTINF: {total}",
        f"Logos PNG generados: {success}",
        f"Sin logo: {unresolved}",
        f"PNG con fondo blanco por visibilidad: {backgrounds}",
        "",
        "FUENTES UTILIZADAS",
    ])

    for source, count in sorted(
        counts.items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        report_lines.append(
            f"- {source}: {count}"
        )

    report_lines.extend([
        "",
        "INTEGRIDAD:",
        f"- Cantidad de EXTINF preservada: {'SI' if before_extinf == after_extinf else 'NO'}",
        "- URLs de streams: no modificadas.",
        "- Entradas #EXTVLCOPT: no modificadas.",
        "- Canales: no eliminados.",
        "- Logos almacenados localmente como PNG.",
    ])

    report_path.write_text(
        "\n".join(report_lines) + "\n",
        encoding="utf-8",
    )

    print()
    print("[+] Proceso terminado.")
    print(f"[+] Logos PNG generados: {success}/{total}")
    print(f"[+] Sin logo: {unresolved}")
    print(f"[+] Fondo blanco aplicado: {backgrounds}")
    print(f"[+] M3U: {output_path}")
    print(f"[+] Informe: {report_path}")
    print(f"[+] Logos: {logo_dir}")

    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
