#!/usr/bin/env python3
"""
Masterlist Argentina + Premium Latinoamerica - Logo Manager
Google Images via SerpApi

OBJETIVO
--------
Automatizar futuros usos sin asignar logos por similitud aproximada.

REGLAS
------
1. Los logos que ya existen en la M3U principal se consideran la referencia:
   el script descarga exactamente esa URL y NO busca reemplazos.
2. Las M3U adicionales indicadas con --source-m3u se usan como fuentes de
   logos ya existentes mediante coincidencia EXACTA de tvg-id/nombre.
3. Los canales que siguen sin logo se buscan SOLO en Google Images mediante
   la API de SerpApi.
4. La consulta se hace con el nombre real del canal:
      History 2 -> "History 2" logo
      AXN South -> "AXN South" logo
      Disney Jr. -> "Disney Junior" logo
5. Los resultados de Google se validan con el titulo/fuente/enlace del resultado.
   No usamos fuzzy matching para decidir el canal.
6. Si no se puede verificar suficientemente un logo, queda pendiente para
   revision manual. Es preferible no tener logo a tener uno incorrecto.
7. No se modifican streams, #EXTVLCOPT ni se eliminan canales.
8. Los logos se convierten a PNG local.
9. La API key NUNCA se guarda en el repositorio.

Dependencias:
    python -m pip install requests Pillow

Variable de entorno:
    SERPAPI_KEY o SERPAPI_API_KEY

Ejemplo:
    $env:SERPAPI_KEY="TU_CLAVE"
    python scripts/masterlist_argentina_latino_logos_serpapi.py \
      --input masterlist_argentina_latino.m3u \
      --source-m3u ".\\otra-masterlist.m3u" \
      --output masterlist_argentina_latino_logos.m3u \
      --report masterlist_argentina_latino_logos_report.txt \
      --logo-dir logos \
      --logo-base-url "https://raw.githubusercontent.com/D3PR3D4DOR/IPTV-Argentina/main/logos" \
      --clean-logo-dir
"""

from __future__ import annotations

import argparse
import json
import hashlib
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image


SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_URL = "https://serpapi.com/account.json"

MAX_IMAGE_BYTES = 12 * 1024 * 1024
HTTP_TIMEOUT = 30
MAX_RESULTS = 20
MAX_LOGO_SIZE = 600
PADDING = 18

# Nombres de busqueda reales. Se pueden ampliar en el futuro.
SEARCH_OVERRIDES = {
    "AMC": "AMC",
    "AXN": "AXN",
    "AXN South": "AXN South",
    "Cinecanal": "Cinecanal",
    "Comedy Central": "Comedy Central",
    "Disney Channel": "Disney Channel",
    "Disney Jr.": "Disney Junior",
    "FX": "FX",
    "History 2": "History 2",
    "History": "History",
    "Lifetime": "Lifetime",
    "National Geographic": "National Geographic",
    "Sony Channel": "Sony Channel",
    "Star Channel": "Star Channel",
    "Studio Universal": "Studio Universal",
    "TNT Novelas": "TNT Novelas",
    "Universal TV": "Universal TV",
}

# Diferenciadores que no se pueden perder en un resultado.
DISTINCTIVE = {
    "2",
    "south",
    "junior",
    "jr",
    "novelas",
    "national",
    "geographic",
    "studio",
    "universal",
}

ALIASES = {
    "tv": {"tv", "television"},
    "television": {"tv", "television"},
    "jr": {"jr", "junior"},
    "junior": {"jr", "junior"},
}


@dataclass
class Entry:
    index: int
    extinf: str
    name: str
    tvg_id: str
    logo_url: str
    stream_url: str
    group: str


@dataclass
class Result:
    path: Path | None = None
    source: str = ""
    reason: str = ""


def canon(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()

    # Separamos letras y numeros para que "history2" tambien sea "history 2".
    value = re.sub(r"([a-z])([0-9])", r"\1 \2", value)
    value = re.sub(r"([0-9])([a-z])", r"\1 \2", value)

    value = value.replace("&", " and ")
    value = re.sub(r"\[[^]]*\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)

    return re.sub(r"\s+", " ", value).strip()


def tokens(value: str) -> set[str]:
    return set(canon(value).split())


def token_matches(wanted: str, found: str) -> bool:
    if wanted == found:
        return True

    return found in ALIASES.get(wanted, {wanted})


def evidence_matches(query_name: str, evidence: str) -> bool:
    wanted = tokens(query_name)
    found = tokens(evidence)

    if not wanted or not found:
        return False

    # Todos los tokens importantes tienen que aparecer.
    for token in wanted:
        aliases = ALIASES.get(token, {token})
        if not (aliases & found):
            return False

    # Rechazar variantes claramente distintas:
    # History 2 no acepta History, AXN no acepta AXN South, etc.
    wanted_distinctive = wanted & DISTINCTIVE
    found_distinctive = found & DISTINCTIVE

    if found_distinctive - wanted_distinctive:
        return False

    return True


def search_name(entry: Entry) -> str:
    if entry.name in SEARCH_OVERRIDES:
        return SEARCH_OVERRIDES[entry.name]

    return re.sub(
        r"\s*\((?:144|240|360|480|540|576|720|1080|1440|2160)p\)\s*$",
        "",
        entry.name,
        flags=re.IGNORECASE,
    ).strip()


def google_query(entry: Entry) -> str:
    return f'"{search_name(entry)}" logo'


def safe_filename(entry: Entry) -> str:
    base = canon(entry.tvg_id or entry.name)
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:70]

    digest = hashlib.sha1(
        f"{entry.tvg_id}|{entry.name}".encode("utf-8")
    ).hexdigest()[:10]

    return f"{base}-{digest}.png"


def extract_attr(line: str, attr: str) -> str:
    match = re.search(
        rf'{re.escape(attr)}="([^"]*)"',
        line,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


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


def load_m3u(path: Path) -> tuple[list[str], list[Entry]]:
    raw = path.read_text(encoding="utf-8-sig")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    entries: list[Entry] = []

    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF:"):
            continue

        stream = ""
        for j in range(i + 1, len(lines)):
            if lines[j].startswith(("http://", "https://")):
                stream = lines[j]
                break
            if lines[j].startswith("#EXTINF:"):
                break

        if not stream:
            continue

        entries.append(
            Entry(
                index=i,
                extinf=line,
                name=visible_name(line),
                tvg_id=extract_attr(line, "tvg-id"),
                logo_url=extract_attr(line, "tvg-logo"),
                stream_url=stream,
                group=extract_attr(line, "group-title"),
            )
        )

    return lines, entries


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "IPTV-Argentina-LogoManager/3.0 "
            "(Google Images via SerpApi)"
        ),
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
    })
    return session


def download_image(
    session: requests.Session,
    url: str,
    target: Path,
) -> tuple[int, int]:
    response = session.get(
        url,
        timeout=HTTP_TIMEOUT,
        allow_redirects=True,
        headers={
            "User-Agent": session.headers["User-Agent"],
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    response.raise_for_status()

    if len(response.content) > MAX_IMAGE_BYTES:
        raise RuntimeError("imagen demasiado grande")

    with Image.open(__import__("io").BytesIO(response.content)) as source:
        image = source.convert("RGBA")

        alpha = image.getchannel("A")
        bbox = alpha.getbbox()
        if bbox:
            image = image.crop(bbox)

        image.thumbnail(
            (MAX_LOGO_SIZE, MAX_LOGO_SIZE),
            Image.Resampling.LANCZOS,
        )

        if image.width < 40 or image.height < 20:
            raise RuntimeError("imagen demasiado pequeña")

        padded = Image.new(
            "RGBA",
            (
                image.width + PADDING * 2,
                image.height + PADDING * 2,
            ),
            (0, 0, 0, 0),
        )
        padded.alpha_composite(
            image,
            (PADDING, PADDING),
        )

        target.parent.mkdir(parents=True, exist_ok=True)
        padded.save(target, format="PNG", optimize=True)

        with Image.open(target) as check:
            check.verify()

        return padded.width, padded.height


def download_existing(entry: Entry, logo_dir: Path) -> Result:
    target = logo_dir / safe_filename(entry)
    session = make_session()

    try:
        width, height = download_image(
            session,
            entry.logo_url,
            target,
        )

        return Result(
            path=target,
            source="M3U",
            reason=f"URL original; {width}x{height}px",
        )
    except Exception as exc:
        target.unlink(missing_ok=True)
        return Result(
            source="M3U - ERROR",
            reason=f"{type(exc).__name__}: {exc} | {entry.logo_url}",
        )
    finally:
        session.close()


def load_sources(
    paths: list[str],
) -> list[Entry]:
    result: list[Entry] = []

    for raw_path in paths:
        path = Path(raw_path)

        if not path.exists():
            print(f"[!] M3U fuente no existe: {path}")
            continue

        _, entries = load_m3u(path)
        result.extend(entries)
        print(
            f"[+] Fuente adicional: {path} | "
            f"{len(entries)} entradas"
        )

    return result


def exact_logo_from_sources(
    entry: Entry,
    sources: list[Entry],
) -> str:
    wanted_ids = {
        canon(entry.tvg_id),
        canon(entry.tvg_id.split("@", 1)[0]) if entry.tvg_id else "",
    }
    wanted_ids.discard("")

    wanted_name = canon(search_name(entry))

    # Primero ID exacto.
    for source in sources:
        if not source.logo_url:
            continue

        source_id = canon(source.tvg_id)
        if source_id in wanted_ids:
            return source.logo_url

    # Después nombre exacto.
    for source in sources:
        if not source.logo_url:
            continue

        if canon(search_name(source)) == wanted_name:
            return source.logo_url

    return ""


def serpapi_key() -> str:
    return (
        __import__("os").environ.get("SERPAPI_KEY", "").strip()
        or __import__("os").environ.get("SERPAPI_API_KEY", "").strip()
    )


def check_account(
    api_key: str,
) -> tuple[bool, str]:
    session = make_session()

    try:
        response = session.get(
            SERPAPI_ACCOUNT_URL,
            params={"api_key": api_key},
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        left = data.get("total_searches_left")
        plan = data.get("plan_name", "desconocido")

        return (
            True,
            f"plan={plan} | busquedas_restantes={left}",
        )
    except Exception as exc:
        return (
            False,
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        session.close()


def serpapi_search(
    api_key: str,
    entry: Entry,
) -> list[dict]:
    query = google_query(entry)

    session = make_session()

    try:
        response = session.get(
            SERPAPI_SEARCH_URL,
            params={
                "engine": "google_images",
                "q": query,
                "api_key": api_key,
                "gl": "ar",
                "hl": "es",
                "ijn": "0",
            },
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()

        if data.get("error"):
            raise RuntimeError(str(data["error"]))

        results = data.get("images_results", [])

        return [
            item for item in results
            if isinstance(item, dict)
        ][:MAX_RESULTS]

    finally:
        session.close()


def cache_key(query: str) -> str:
    return hashlib.sha1(
        query.encode("utf-8")
    ).hexdigest()


def load_cache(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(path: Path, cache: dict) -> None:
    path.write_text(
        json.dumps(
            cache,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def result_evidence(item: dict) -> str:
    return " ".join(
        str(item.get(key, ""))
        for key in (
            "title",
            "source",
            "link",
        )
        if item.get(key)
    )


def google_candidate_ok(
    entry: Entry,
    item: dict,
) -> bool:
    title = str(item.get("title", ""))
    source = str(item.get("source", ""))
    link = str(item.get("link", ""))

    evidence = " ".join(
        (title, source, link)
    )

    query_name = search_name(entry)

    return evidence_matches(
        query_name,
        evidence,
    )


def choose_google_result(
    entry: Entry,
    results: list[dict],
    logo_dir: Path,
) -> Result:
    target = logo_dir / safe_filename(entry)
    session = make_session()

    try:
        for item in results:
            if not google_candidate_ok(entry, item):
                continue

            original = str(
                item.get("original")
                or ""
            ).strip()

            if not original.startswith(("http://", "https://")):
                continue

            try:
                width, height = download_image(
                    session,
                    original,
                    target,
                )

                return Result(
                    path=target,
                    source="Google Images / SerpApi",
                    reason=(
                        f'query={google_query(entry)} | '
                        f'position={item.get("position")} | '
                        f'title={item.get("title", "")} | '
                        f'source={item.get("source", "")} | '
                        f'{width}x{height}px | '
                        f'original={original}'
                    ),
                )
            except Exception:
                target.unlink(missing_ok=True)
                continue

        return Result(
            source="Google Images - SIN LOGO VERIFICABLE",
            reason=(
                f"Se obtuvieron {len(results)} resultados, "
                "pero ninguno cumplio la validacion o fue descargable."
            ),
        )

    finally:
        session.close()


def google_missing(
    api_key: str,
    entry: Entry,
    logo_dir: Path,
    cache: dict,
    cache_path: Path,
    use_cache: bool,
) -> Result:
    query = google_query(entry)
    key = cache_key(query)

    results = None

    if use_cache and key in cache:
        cached = cache[key]
        if isinstance(cached, list):
            results = cached

    if results is None:
        results = serpapi_search(
            api_key,
            entry,
        )

        cache[key] = results
        save_cache(cache_path, cache)

    return choose_google_result(
        entry,
        results,
        logo_dir,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Descarga logos existentes de M3U y busca faltantes "
            "solo en Google Images mediante SerpApi."
        )
    )

    parser.add_argument(
        "--input",
        default="masterlist_argentina_latino.m3u",
    )
    parser.add_argument(
        "--source-m3u",
        action="append",
        default=[],
        help=(
            "M3U adicional con logos; se puede repetir."
        ),
    )
    parser.add_argument(
        "--output",
        default="masterlist_argentina_latino_logos_serpapi.m3u",
    )
    parser.add_argument(
        "--report",
        default="masterlist_argentina_latino_logos_serpapi_report.txt",
    )
    parser.add_argument(
        "--logo-dir",
        default="logos",
    )
    parser.add_argument(
        "--logo-base-url",
        default="",
    )
    parser.add_argument(
        "--cache",
        default="logo_search_cache.json",
    )
    parser.add_argument(
        "--clean-logo-dir",
        action="store_true",
    )
    parser.add_argument(
        "--no-google",
        action="store_true",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="ignora el cache de resultados de Google",
    )
    parser.add_argument(
        "--check-account",
        action="store_true",
        help="muestra el saldo de SerpApi y termina",
    )

    args = parser.parse_args()

    input_path = Path(args.input)

    if not input_path.exists():
        print(
            f"[!] No existe la M3U principal: {input_path}",
            file=sys.stderr,
        )
        return 1

    api_key = serpapi_key()

    if args.check_account:
        if not api_key:
            print(
                "[!] Falta SERPAPI_KEY o SERPAPI_API_KEY.",
                file=sys.stderr,
            )
            return 1

        ok, message = check_account(api_key)
        print(
            ("[+] " if ok else "[!] ") + message
        )
        return 0 if ok else 1

    if not args.no_google and not api_key:
        print(
            "[!] Falta SERPAPI_KEY o SERPAPI_API_KEY.",
            file=sys.stderr,
        )
        print(
            "    Define la variable antes de ejecutar el script.",
            file=sys.stderr,
        )
        return 1

    if args.clean_logo_dir and Path(args.logo_dir).exists():
        print(
            f"[+] Limpiando carpeta: {args.logo_dir}"
        )
        shutil.rmtree(args.logo_dir)

    lines, main_entries = load_m3u(input_path)

    additional = load_sources(args.source_m3u)

    main_existing = [
        entry for entry in main_entries
        if entry.logo_url
    ]

    extra_existing = [
        entry for entry in additional
        if entry.logo_url
    ]

    # Todos los logos disponibles se descargan, pero los logos de la M3U
    # principal mantienen prioridad cuando ese canal ya tiene tvg-logo.
    source_entries = main_existing + extra_existing

    unique_existing: dict[tuple[str, str, str], Entry] = {}

    for entry in source_entries:
        key = (
            canon(entry.tvg_id),
            canon(entry.name),
            entry.logo_url,
        )
        unique_existing.setdefault(key, entry)

    existing = list(unique_existing.values())
    missing = [
        entry
        for entry in main_entries
        if not entry.logo_url
    ]

    print()
    print(
        f"[+] EXTINF principal: {len(main_entries)}"
    )
    print(
        f"[+] Logos disponibles en las M3U: {len(existing)}"
    )
    print(
        f"[+] Logos faltantes en principal: {len(missing)}"
    )

    if missing:
        print()
        print("[+] Canales faltantes:")

        for entry in missing:
            alternative = exact_logo_from_sources(
                entry,
                additional,
            )

            if alternative:
                print(
                    f"    - {entry.name} -> "
                    f"logo encontrado en M3U adicional"
                )
            else:
                print(
                    f"    - {entry.name} -> "
                    f'Google: {google_query(entry)}'
                )

    logo_dir = Path(args.logo_dir)
    logo_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------
    # 1) Descargar logos que ya existen.
    # ---------------------------------------------------------------
    main_existing_keys = {
        (canon(entry.tvg_id), canon(entry.name))
        for entry in main_existing
    }

    existing_results: dict[
        tuple[str, str],
        Result,
    ] = {}

    print()
    print(
        "[+] Descargando todos los logos disponibles "
        "en las M3U..."
    )

    for position, entry in enumerate(
        existing,
        start=1,
    ):
        key = (
            canon(entry.tvg_id),
            canon(entry.name),
        )

        result = download_existing(
            entry,
            logo_dir,
        )

        # La M3U principal es la referencia de verdad:
        # una M3U adicional nunca puede sobrescribir su logo.
        if key not in existing_results:
            existing_results[key] = result
        elif key in main_existing_keys and result.path:
            existing_results[key] = result
        elif not existing_results[key].path and result.path:
            existing_results[key] = result

        print(
            f"[M3U {position:>3}/{len(existing)}] "
            f"{'OK' if result.path else 'FAIL':<4} "
            f"{entry.name}"
        )

    # ---------------------------------------------------------------
    # 2) Resolver faltantes.
    # ---------------------------------------------------------------
    google_results: dict[
        tuple[str, str],
        Result,
    ] = {}

    cache_path = Path(args.cache)
    cache = load_cache(cache_path)

    if missing and not args.no_google:
        print()
        print(
            "[+] Resolviendo logos faltantes..."
        )

        remaining = 0

        # Solo una consulta API cuando realmente no hay un logo exacto
        # disponible en las M3U adicionales.
        for position, entry in enumerate(
            missing,
            start=1,
        ):
            key = (
                canon(entry.tvg_id),
                canon(entry.name),
            )

            alternative = exact_logo_from_sources(
                entry,
                additional,
            )

            if alternative:
                print(
                    f"[M3U EXTRA {position:>2}/{len(missing)}] "
                    f"{entry.name} -> usando logo exacto de otra M3U"
                )

                alt_entry = Entry(
                    index=entry.index,
                    extinf=entry.extinf,
                    name=entry.name,
                    tvg_id=entry.tvg_id,
                    logo_url=alternative,
                    stream_url=entry.stream_url,
                    group=entry.group,
                )

                result = download_existing(
                    alt_entry,
                    logo_dir,
                )

                if result.path:
                    google_results[key] = result
                    continue

                print(
                    "    -> la URL alternativa no fue descargable; "
                    "se pasa a Google."
                )

            print(
                f'[GOOGLE {position:>2}/{len(missing)}] '
                f'{entry.name} -> {google_query(entry)}'
            )

            try:
                result = google_missing(
                    api_key,
                    entry,
                    logo_dir,
                    cache,
                    cache_path,
                    not args.no_cache,
                )

                google_results[key] = result

                if result.path:
                    print(
                        f"    -> OK | {result.path.name}"
                    )
                else:
                    print(
                        f"    -> PENDIENTE | {result.source}"
                    )

            except Exception as exc:
                result = Result(
                    source="SerpApi - ERROR",
                    reason=(
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
                google_results[key] = result
                print(
                    f"    -> ERROR | {result.reason}"
                )

            remaining += 1

            # No hacemos paralelismo con Google.
            if remaining != len(missing):
                time.sleep(0.75)

    # ---------------------------------------------------------------
    # 3) Crear M3U final.
    # ---------------------------------------------------------------
    public_base = args.logo_base_url.rstrip("/")

    report: list[str] = [
        "Masterlist Argentina + Premium Latinoamerica",
        "Logo Manager - Google Images via SerpApi",
        "=" * 78,
        f"M3U principal: {input_path}",
        f"EXTINF principal: {len(main_entries)}",
        "",
        "ESTRATEGIA:",
        "- Logos existentes: se descarga exactamente su URL original.",
        "- M3U adicionales: solo coincidencia exacta de ID/nombre.",
        "- Logos faltantes: Google Images via SerpApi.",
        "- Fuzzy matching: DESACTIVADO.",
        "- No se sustituyen logos existentes por busquedas.",
        "",
    ]

    ok_existing = 0
    fail_existing = 0
    ok_google = 0
    pending = 0

    for entry in main_entries:
        key = (
            canon(entry.tvg_id),
            canon(entry.name),
        )

        if entry.logo_url:
            result = existing_results.get(key)

            if result and result.path:
                ok_existing += 1

                logo_url = (
                    f"{public_base}/{result.path.name}"
                    if public_base
                    else result.path.as_posix()
                )

                lines[entry.index] = set_attr(
                    lines[entry.index],
                    "tvg-logo",
                    logo_url,
                )

                report.append(
                    f"OK EXISTENTE | {entry.name} | "
                    f"{result.reason}"
                )
            else:
                fail_existing += 1

                report.append(
                    f"FAIL EXISTENTE | {entry.name} | "
                    f"{result.reason if result else 'sin resultado'}"
                )

        else:
            result = google_results.get(key)

            if result and result.path:
                ok_google += 1

                logo_url = (
                    f"{public_base}/{result.path.name}"
                    if public_base
                    else result.path.as_posix()
                )

                lines[entry.index] = set_attr(
                    lines[entry.index],
                    "tvg-logo",
                    logo_url,
                )

                report.append(
                    f"OK FALTANTE | {entry.name} | "
                    f"{result.reason}"
                )
            else:
                pending += 1

                report.append(
                    f"PENDIENTE | {entry.name} | "
                    f"{result.reason if result else 'sin resultado'}"
                )

    before = len(main_entries)
    after = sum(
        1 for line in lines
        if line.startswith("#EXTINF:")
    )

    if before != after:
        print(
            f"[!] ERROR DE INTEGRIDAD: {before} -> {after}",
            file=sys.stderr,
        )
        return 2

    output_path = Path(args.output)
    save_m3u(output_path, lines)

    report.extend([
        "",
        "RESUMEN",
        "-" * 78,
        f"Logos existentes descargados: {ok_existing}",
        f"Logos existentes con error: {fail_existing}",
        f"Logos faltantes resueltos: {ok_google}",
        f"Pendientes: {pending}",
        f"EXTINF: {after}/{before}",
        "",
        "INTEGRIDAD:",
        "- Streams: no modificados.",
        "- #EXTVLCOPT: no modificados.",
        "- Canales: no eliminados.",
        "- Logos existentes: no reemplazados.",
        "- Fuzzy matching: desactivado.",
        "- Busqueda externa: solo Google Images via SerpApi.",
    ])

    Path(args.report).write_text(
        "\n".join(report) + "\n",
        encoding="utf-8",
    )

    print()
    print("[+] Proceso terminado.")
    print(
        f"[+] Logos existentes descargados: {ok_existing}"
    )
    print(
        f"[+] Logos existentes con error: {fail_existing}"
    )
    print(
        f"[+] Logos faltantes resueltos: {ok_google}"
    )
    print(f"[+] Pendientes: {pending}")
    print(f"[+] M3U: {output_path}")
    print(f"[+] Informe: {args.report}")
    print(f"[+] Logos: {logo_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
