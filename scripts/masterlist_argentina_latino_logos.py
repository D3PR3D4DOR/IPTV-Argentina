#!/usr/bin/env python3
"""
Masterlist Argentina + Premium Latinoamerica - Logo Manager

Objetivo:
- leer una M3U existente sin cambiar canales ni URLs;
- conservar todas las entradas y opciones #EXTVLCOPT intactas;
- completar los tvg-logo que falten;
- buscar primero en la API publica de iptv-org;
- usar coincidencia por tvg-id y, si no alcanza, por nombre/alias;
- como ultimo recurso, buscar un logo en Wikimedia Commons;
- generar un informe de lo encontrado y de los canales que quedaron sin logo.

IMPORTANTE:
- Por defecto NO reemplaza logos que ya existan.
- Solo modifica la linea #EXTINF agregando/reemplazando tvg-logo.
- Nunca cambia la URL del stream.
- Nunca elimina canales.
- Nunca reemplaza el stream por otro.
- Si no encuentra un logo con suficiente confianza, lo deja sin logo y lo informa.

Dependencia:
    pip install requests

Uso:
    python3 scripts/masterlist_argentina_latino_v10.py

Ejemplo:
    python3 scripts/masterlist_argentina_latino_v10.py \
        --input masterlist_argentina_latino.m3u \
        --output masterlist_argentina_latino_logos.m3u \
        --report masterlist_argentina_latino_logos_report.txt

Para volver a buscar y actualizar tambien los logos existentes:
    python3 scripts/masterlist_argentina_latino_v10.py --refresh-existing
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests


# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

IPTV_ORG_CHANNELS = "https://iptv-org.github.io/api/channels.json"
IPTV_ORG_LOGOS = "https://iptv-org.github.io/api/logos.json"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

UA = "Masterlist-Argentina-LATAM/1.1"
SOURCE_TIMEOUT = 25
COMMONS_TIMEOUT = 15
LOGO_THUMB_WIDTH = 600

RASTER_FORMATS = {
    "PNG", "JPEG", "JPG", "WEBP", "GIF", "AVIF", "APNG"
}

REGION_MARKERS = (
    "latin america",
    "latinamerica",
    "latinoamerica",
    "latinoamérica",
    "panregional",
    "andes",
    "south",
    "mexico",
    "chile",
    "argentina",
    "central america",
    "america latina",
    "américa latina",
)


# ---------------------------------------------------------------------------
# MODELOS
# ---------------------------------------------------------------------------

@dataclass
class LogoResult:
    url: str = ""
    source: str = ""
    confidence: int = 0
    reason: str = ""


# ---------------------------------------------------------------------------
# TEXTO / NORMALIZACION
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
    }
    return {
        x for x in canon(value).split()
        if len(x) >= 2 and x not in stop
    }


def extract_attr(line: str, attr: str) -> str:
    m = re.search(
        rf'{re.escape(attr)}="([^"]*)"',
        line,
        flags=re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def visible_name(extinf: str) -> str:
    return extinf.rsplit(",", 1)[-1].strip() if "," in extinf else extinf.strip()


def set_attr(line: str, attr: str, value: str) -> str:
    escaped = value.replace('"', "%22")

    pattern = rf'{re.escape(attr)}="[^"]*"'
    if re.search(pattern, line, flags=re.IGNORECASE):
        return re.sub(
            pattern,
            f'{attr}="{escaped}"',
            line,
            count=1,
            flags=re.IGNORECASE,
        )

    comma = line.find(",")
    if comma < 0:
        return line

    return line[:comma] + f' {attr}="{escaped}"' + line[comma:]


def tvg_id_base(tvg_id: str) -> str:
    return tvg_id.split("@", 1)[0].strip()


def region_score(text: str) -> int:
    n = canon(text)
    score = 0
    for marker in REGION_MARKERS:
        if canon(marker) in n:
            score += 10
    return score


def name_similarity(query: str, candidate: str) -> float:
    q = canon(query)
    c = canon(candidate)

    if not q or not c:
        return 0.0

    score = SequenceMatcher(None, q, c).ratio()

    qt = tokenize(query)
    ct = tokenize(candidate)

    if qt and ct:
        overlap = len(qt & ct) / max(len(qt), len(ct))
        score = max(score, overlap)

    if q == c:
        score = 1.0

    return score


# ---------------------------------------------------------------------------
# M3U
# ---------------------------------------------------------------------------

def load_m3u(path: Path) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"No se pudo leer {path}: {exc}") from exc

    return raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def save_m3u(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines)

    if not text.endswith("\n"):
        text += "\n"

    # Las playlists M3U publicas de iptv-org usan CRLF.
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# IPTVP-ORG
# ---------------------------------------------------------------------------

def download_json(session: requests.Session, url: str) -> Any:
    r = session.get(
        url,
        timeout=SOURCE_TIMEOUT,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    r.raise_for_status()
    return r.json()


def build_iptv_org_indexes(
    session: requests.Session,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """
    Descarga:
      - channels.json
      - logos.json

    Devuelve:
      channel_index: channel id -> lista de logos
      channels: lista completa de canales para fuzzy matching
    """
    print("[+] Descargando channels.json de iptv-org...")
    channels = download_json(session, IPTV_ORG_CHANNELS)

    print("[+] Descargando logos.json de iptv-org...")
    logos = download_json(session, IPTV_ORG_LOGOS)

    if not isinstance(channels, list):
        raise RuntimeError("channels.json no devolvio una lista.")

    if not isinstance(logos, list):
        raise RuntimeError("logos.json no devolvio una lista.")

    channel_index: dict[str, list[dict]] = {}

    for item in logos:
        if not isinstance(item, dict):
            continue

        channel = str(item.get("channel", "")).strip()
        if not channel:
            continue

        channel_index.setdefault(channel, []).append(item)

    return channel_index, [x for x in channels if isinstance(x, dict)]


def logo_format_rank(item: dict) -> int:
    fmt = str(item.get("format", "")).upper()
    return 3 if fmt in RASTER_FORMATS else 0


def logo_visibility_score(item: dict) -> int:
    """
    Prioriza variantes que suelen verse mejor sobre interfaces oscuras.
    iptv-org publica tags como "white", "horizontal", etc.
    """
    tags = {
        str(x).strip().lower()
        for x in (item.get("tags") or [])
        if x
    }

    score = 0

    if "white" in tags or "light" in tags:
        score += 35

    if "horizontal" in tags:
        score += 8

    if "black" in tags or "dark" in tags:
        score -= 25

    if "transparent" in tags:
        score -= 3

    return score


def choose_logo(
    items: list[dict],
    preferred_feed: str = "",
) -> Optional[dict]:
    if not items:
        return None

    usable = [
        x for x in items
        if isinstance(x, dict)
        and str(x.get("url", "")).startswith("https://")
    ]

    if not usable:
        return None

    def rank(item: dict) -> tuple[int, int, int, int, int]:
        fmt_rank = logo_format_rank(item)
        visibility = logo_visibility_score(item)

        feed_match = 1 if (
            preferred_feed
            and str(item.get("feed") or "").strip().lower() == preferred_feed.lower()
        ) else 0

        in_use = 1 if item.get("in_use") is True else 0

        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)

        # Preferimos tamaños razonables para clientes IPTV. Evitamos logos
        # gigantes si existe una variante equivalente más pequeña.
        size_score = 1 if 150 <= max(width, height) <= 1600 else 0

        return (
            fmt_rank,
            visibility,
            feed_match * 30 + in_use * 20 + size_score * 5,
            width,
            height,
        )

    # IMPORTANTE:
    # - primero raster (PNG/JPEG/WebP/...)
    # - después visibilidad
    # - después feed/in_use
    # - SVG queda como última opción y será rasterizado si es de Wikimedia.
    usable.sort(key=rank, reverse=True)

    for item in usable:
        if logo_format_rank(item) > 0:
            return item

    # Si no existe una versión raster, devolvemos la mejor vectorial.
    # El llamador intentará obtener un thumbnail PNG/JPEG de Wikimedia.
    return usable[0]


def wikimedia_raster_url(
    session: requests.Session,
    url: str,
) -> Optional[str]:
    """
    Para un logo SVG alojado en Wikimedia Commons obtiene un thumbnail
    rasterizado mediante la API de Wikimedia. Así evitamos entregar SVG
    directamente a reproductores IPTV con soporte limitado.
    """
    if "upload.wikimedia.org/" not in url:
        return None

    try:
        filename = url.split("/")[-1].split("?", 1)[0]
        if not filename:
            return None

        params = {
            "action": "query",
            "titles": f"File:{filename}",
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "iiurlwidth": LOGO_THUMB_WIDTH,
            "format": "json",
            "formatversion": "2",
        }

        r = session.get(
            COMMONS_API,
            params=params,
            timeout=COMMONS_TIMEOUT,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
            },
        )
        r.raise_for_status()

        data = r.json()
        pages = data.get("query", {}).get("pages", [])
        if not isinstance(pages, list) or not pages:
            return None

        info = pages[0].get("imageinfo") or []
        if not info:
            return None

        image = info[0]
        thumb = str(image.get("thumburl") or "")
        if thumb.startswith("https://"):
            return thumb

    except (requests.RequestException, ValueError):
        return None

    return None


def make_raster_result(
    session: requests.Session,
    item: dict,
    source: str,
    confidence: int,
    reason: str,
) -> Optional[LogoResult]:
    url = str(item.get("url", "")).strip()
    fmt = str(item.get("format", "")).upper()

    if not url.startswith("https://"):
        return None

    # Los formatos raster se usan directamente.
    if fmt in RASTER_FORMATS:
        return LogoResult(
            url=url,
            source=source,
            confidence=confidence,
            reason=reason,
        )

    # SVG: solo lo aceptamos si Wikimedia puede entregarnos un thumbnail
    # rasterizado.
    if fmt == "SVG" or url.lower().split("?", 1)[0].endswith(".svg"):
        raster = wikimedia_raster_url(session, url)
        if raster:
            return LogoResult(
                url=raster,
                source=f"{source}:rasterizado",
                confidence=confidence,
                reason=f"{reason}; thumbnail PNG/JPEG generado por Wikimedia",
            )
        return None

    return None



def exact_iptv_org_logo(
    tvg_id: str,
    channel_index: dict[str, list[dict]],
    session: requests.Session,
) -> Optional[LogoResult]:
    if not tvg_id:
        return None

    base = tvg_id_base(tvg_id)
    feed = tvg_id.split("@", 1)[1].strip() if "@" in tvg_id else ""

    for channel_id in (tvg_id, base):
        item = choose_logo(channel_index.get(channel_id, []), feed)
        if item:
            result = make_raster_result(
                session,
                item,
                "iptv-org:exact",
                100,
                f"match exacto tvg-id={channel_id}",
            )
            if result:
                return result

    return None



def fuzzy_iptv_org_logo(
    name: str,
    tvg_id: str,
    channels: list[dict],
    channel_index: dict[str, list[dict]],
    premium: bool,
    session: requests.Session,
) -> Optional[LogoResult]:
    query_names = [name]

    base = tvg_id_base(tvg_id)
    if base:
        query_names.append(base)

    best: tuple[float, Optional[dict]] = (0.0, None)

    for channel in channels:
        channel_id = str(channel.get("id", "")).strip()
        if not channel_id:
            continue

        cand_name = str(channel.get("name", "")).strip()
        alt_names = channel.get("alt_names") or []

        candidates = [cand_name]
        if isinstance(alt_names, list):
            candidates.extend(str(x) for x in alt_names if x)

        similarity = max(
            name_similarity(q, c)
            for q in query_names
            for c in candidates
        )

        regional_bonus = 0.0
        if premium:
            region_text = " ".join(
                [channel_id, cand_name, *[str(x) for x in alt_names]]
                if isinstance(alt_names, list)
                else [channel_id, cand_name]
            )
            if region_score(region_text):
                regional_bonus = 0.08

        total = min(1.0, similarity + regional_bonus)

        if total > best[0]:
            best = (total, channel)

    confidence = int(best[0] * 100)
    channel = best[1]

    if not channel or confidence < (88 if premium else 84):
        return None

    channel_id = str(channel.get("id", "")).strip()
    logo = choose_logo(channel_index.get(channel_id, []))
    if not logo:
        return None

    return make_raster_result(
        session,
        logo,
        "iptv-org:name",
        confidence,
        f"match por nombre -> {channel_id}",
    )




# ---------------------------------------------------------------------------
# WIKIMEDIA COMMONS - FALLBACK WEB
# ---------------------------------------------------------------------------

def commons_logo_search(
    session: requests.Session,
    name: str,
    premium: bool,
) -> Optional[LogoResult]:
    extra = " Latin America" if premium else " Argentina"
    query = f'"{name}" television channel logo{extra}'

    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": "10",
        "prop": "imageinfo",
        "iiprop": "url|mime|size",
        "iiurlwidth": LOGO_THUMB_WIDTH,
        "format": "json",
        "formatversion": "2",
    }

    try:
        r = session.get(
            COMMONS_API,
            params=params,
            timeout=COMMONS_TIMEOUT,
            headers={
                "User-Agent": UA,
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        return None

    pages = data.get("query", {}).get("pages", [])
    if not isinstance(pages, list):
        return None

    best: tuple[float, Optional[dict]] = (0.0, None)

    for page in pages:
        title = str(page.get("title", ""))
        info = page.get("imageinfo") or []
        if not info:
            continue

        image = info[0]
        url = str(image.get("thumburl") or image.get("url") or "")
        mime = str(image.get("thumbmime") or image.get("mime") or "").lower()

        if not url.startswith("https://"):
            continue

        # Wikimedia entrega thumburl rasterizado incluso cuando el original
        # es SVG. Evitamos URL SVG directas en el resultado final.
        if url.lower().split("?", 1)[0].endswith(".svg"):
            continue

        if mime.startswith("image/") and mime not in {"image/svg+xml"}:
            pass
        elif not mime:
            # Si no llega thumbmime, la extensión del thumburl debe ser raster.
            if not re.search(r"\.(?:png|jpe?g|webp|gif|avif|apng)(?:$|[?])", url, re.I):
                continue
        else:
            continue

        title_norm = canon(title)

        has_logo_word = "logo" in title_norm or "wordmark" in title_norm
        if not has_logo_word:
            continue

        similarity = name_similarity(name, title)
        if name_norm_contains(name, title):
            similarity = max(similarity, 0.90)

        # Preferimos thumbnails de imagen reales; no penalizamos un logo
        # correcto por ser un thumbnail raster.
        if "white" in title_norm:
            similarity = min(1.0, similarity + 0.03)

        if similarity > best[0]:
            best = (similarity, {"url": url, "title": title})

    if not best[1] or best[0] < 0.80:
        return None

    return LogoResult(
        url=best[1]["url"],
        source="Wikimedia Commons:raster",
        confidence=int(best[0] * 100),
        reason=f"busqueda web -> {best[1]['title']}",
    )




# ---------------------------------------------------------------------------
# RESOLUCION POR CANAL
# ---------------------------------------------------------------------------

def is_premium_line(extinf: str) -> bool:
    return canon(extract_attr(extinf, "group-title")) == "premium latinoamerica"


def resolve_logo(
    extinf: str,
    channel_index: dict[str, list[dict]],
    channels: list[dict],
    session: requests.Session,
    web_fallback: bool,
) -> LogoResult:
    tvg_id = extract_attr(extinf, "tvg-id")
    name = visible_name(extinf)
    premium = is_premium_line(extinf)

    exact = exact_iptv_org_logo(
        tvg_id,
        channel_index,
        session,
    )
    if exact:
        return exact

    fuzzy = fuzzy_iptv_org_logo(
        name,
        tvg_id,
        channels,
        channel_index,
        premium,
        session,
    )
    if fuzzy:
        return fuzzy

    if web_fallback:
        web = commons_logo_search(session, name, premium)
        if web:
            return web

    return LogoResult(
        source="NO ENCONTRADO",
        confidence=0,
        reason="No se encontro un logo raster compatible con suficiente confianza.",
    )




# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Completa tvg-logo de una M3U sin tocar canales ni URLs; prioriza imagenes raster compatibles (PNG/JPEG/WebP)."
    )

    parser.add_argument(
        "--input",
        default="masterlist_argentina_latino.m3u",
        help="M3U de entrada",
    )
    parser.add_argument(
        "--output",
        default="masterlist_argentina_latino_logos.m3u",
        help="M3U de salida",
    )
    parser.add_argument(
        "--report",
        default="masterlist_argentina_latino_logos_report.txt",
        help="informe de logos",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="busquedas web simultaneas para fallbacks (default: 8)",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="tambien permite reemplazar logos que ya existan",
    )
    parser.add_argument(
        "--no-web-fallback",
        action="store_true",
        help="no usar Wikimedia Commons como busqueda web de respaldo",
    )

    args = parser.parse_args()

    if args.workers < 1 or args.workers > 20:
        parser.error("--workers debe estar entre 1 y 20")

    input_path = Path(args.input)
    output_path = Path(args.output)
    report_path = Path(args.report)

    if not input_path.exists():
        print(f"[!] No existe la M3U de entrada: {input_path}", file=sys.stderr)
        return 1

    try:
        lines = load_m3u(input_path)
    except RuntimeError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    session = requests.Session()

    try:
        channel_index, channels = build_iptv_org_indexes(session)
    except Exception as exc:
        print(f"[!] No se pudo obtener la informacion de logos: {exc}", file=sys.stderr)
        session.close()
        return 1

    # -----------------------------------------------------------------------
    # Identificar entradas EXTINF y decidir cuales necesitan resolucion.
    # -----------------------------------------------------------------------
    entries: list[tuple[int, str]] = []

    for i, line in enumerate(lines):
        if line.startswith("#EXTINF:"):
            entries.append((i, line))

    total = len(entries)
    already = 0
    pending: list[tuple[int, str]] = []

    for index, extinf in entries:
        current_logo = extract_attr(extinf, "tvg-logo")
        if current_logo and not args.refresh_existing:
            already += 1
            continue

        pending.append((index, extinf))

    print()
    print(f"[+] Entradas EXTINF: {total}")
    print(f"[+] Con logo existente (preservado): {already}")
    print(f"[+] A resolver: {len(pending)}")
    print()

    # -----------------------------------------------------------------------
    # Primero intentamos resolver desde iptv-org localmente.
    # El fallback web solo se ejecuta para los casos que no encuentran logo.
    # -----------------------------------------------------------------------
    results: dict[int, LogoResult] = {}

    unresolved_after_iptv: list[tuple[int, str]] = []

    for index, extinf in pending:
        exact = exact_iptv_org_logo(
            extract_attr(extinf, "tvg-id"),
            channel_index,
            session,
        )

        if exact:
            results[index] = exact
            continue

        fuzzy = fuzzy_iptv_org_logo(
            visible_name(extinf),
            extract_attr(extinf, "tvg-id"),
            channels,
            channel_index,
            is_premium_line(extinf),
            session,
        )

        if fuzzy:
            results[index] = fuzzy
            continue

        unresolved_after_iptv.append((index, extinf))

    print(f"[+] Resueltos con iptv-org: {len(results)}")
    print(f"[+] Sin coincidencia en iptv-org: {len(unresolved_after_iptv)}")

    # -----------------------------------------------------------------------
    # Fallback web con Commons.
    # -----------------------------------------------------------------------
    if unresolved_after_iptv and not args.no_web_fallback:
        def web_job(item: tuple[int, str]) -> tuple[int, LogoResult]:
            index, extinf = item
            local_session = requests.Session()
            try:
                result = commons_logo_search(
                    local_session,
                    visible_name(extinf),
                    is_premium_line(extinf),
                )
                if result:
                    return index, result
                return index, LogoResult(
                    source="NO ENCONTRADO",
                    confidence=0,
                    reason="No hubo resultado valido en Wikimedia Commons.",
                )
            finally:
                local_session.close()

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers
        ) as executor:
            future_map = [
                executor.submit(web_job, item)
                for item in unresolved_after_iptv
            ]

            for future in concurrent.futures.as_completed(future_map):
                index, result = future.result()
                results[index] = result

    # -----------------------------------------------------------------------
    # Aplicar exclusivamente tvg-logo.
    # -----------------------------------------------------------------------
    added = 0
    unresolved = 0
    refreshed = 0

    report_lines = [
        "Masterlist Argentina + Premium Latinoamerica Logo Report",
        "=" * 72,
        f"M3U entrada: {input_path}",
        f"M3U salida: {output_path}",
        "",
    ]

    for index, extinf in pending:
        result = results.get(
            index,
            LogoResult(
                source="NO ENCONTRADO",
                confidence=0,
                reason="No se obtuvo resultado raster compatible.",
            ),
        )

        name = visible_name(extinf)
        tvg_id = extract_attr(extinf, "tvg-id")
        old_logo = extract_attr(extinf, "tvg-logo")

        if result.url:
            lines[index] = set_attr(lines[index], "tvg-logo", result.url)

            if old_logo:
                refreshed += 1
                action = "ACTUALIZADO"
            else:
                added += 1
                action = "AGREGADO"

            report_lines.append(
                f"✅ {action} | {name} | tvg-id={tvg_id}"
            )
            report_lines.append(
                f"   fuente={result.source} | confianza={result.confidence}%"
            )
            report_lines.append(f"   logo={result.url}")
            report_lines.append(f"   detalle={result.reason}")
        else:
            unresolved += 1
            report_lines.append(
                f"❌ SIN LOGO | {name} | tvg-id={tvg_id}"
            )
            report_lines.append(f"   detalle={result.reason}")

    # Verificacion: nunca debe cambiar la cantidad de EXTINF.
    final_count = sum(1 for line in lines if line.startswith("#EXTINF:"))
    if final_count != total:
        print(
            f"[!] ERROR DE INTEGRIDAD: antes={total}, despues={final_count}",
            file=sys.stderr,
        )
        return 2

    try:
        save_m3u(output_path, lines)
    except Exception as exc:
        print(f"[!] No se pudo escribir {output_path}: {exc}", file=sys.stderr)
        return 1

    report_lines.extend([
        "",
        "RESUMEN",
        "-" * 72,
        f"Entradas EXTINF: {total}",
        f"Ya tenian logo y fueron preservadas: {already}",
        f"Logos nuevos agregados: {added}",
        f"Logos existentes actualizados: {refreshed}",
        f"Sin logo: {unresolved}",
        "",
        "INTEGRIDAD:",
        f"- Cantidad de EXTINF preservada: {'SI' if final_count == total else 'NO'}",
        "- URLs de streams: no modificadas por este script.",
        "- Entradas #EXTVLCOPT: no modificadas por este script.",
    ])

    try:
        report_path.write_text(
            "\n".join(report_lines) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[!] No se pudo escribir el informe: {exc}", file=sys.stderr)
        return 1

    print()
    print("[+] Proceso terminado.")
    print(f"[+] Logos nuevos: {added}")
    print(f"[+] Logos actualizados: {refreshed}")
    print(f"[+] Sin logo: {unresolved}")
    print(f"[+] M3U: {output_path}")
    print(f"[+] Informe: {report_path}")

    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
