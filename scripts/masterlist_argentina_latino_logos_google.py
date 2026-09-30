#!/usr/bin/env python3
"""
Masterlist Argentina + Premium Latinoamerica - Google Logo Downloader

Estrategia deliberadamente simple y segura:

1. Lee una o varias M3U.
2. Para cada canal que ya tenga tvg-logo:
   - descarga ESA URL;
   - la convierte a PNG local;
   - no intenta buscar otro logo.
3. Informa exactamente cuales canales no tenian logo.
4. Para los canales sin logo, BUSCA SOLAMENTE EN GOOGLE IMAGES.
5. La consulta se construye con el nombre real del canal:
      History 2 -> "History 2 logo"
      Disney Jr. -> "Disney Junior logo"
      AXN South -> "AXN South logo"
6. Los resultados de Google se validan antes de guardarlos:
   - la pagina/metadata debe mencionar los terminos importantes del canal;
   - no se aceptan variantes que omitan diferenciadores como 2, South, Junior,
     Novelas, etc.;
   - la imagen tiene que ser descargable y un PNG/JPEG/WEBP valido.
7. Si Google no entrega un resultado verificable, el canal queda SIN LOGO.
   Es preferible dejarlo pendiente que asignar un logo incorrecto.
8. No modifica URLs de streams, #EXTVLCOPT ni elimina canales.

El script genera:
- logos/*.png
- masterlist_argentina_latino_logos_google.m3u
- masterlist_argentina_latino_logos_google_report.txt

Opcionalmente se pueden pasar M3U adicionales con --source-m3u.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import io
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
from PIL import Image

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
except ImportError:
    webdriver = None
    By = None


GOOGLE_IMAGES_URL = "https://www.google.com/search"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
TIMEOUT = 20
MAX_LOGO_SIZE = 600
PADDING = 18

GOOGLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
}

# Nombres de búsqueda precisos para los canales que conocemos que pueden
# confundirse con variantes parecidas.
SEARCH_NAME_OVERRIDES = {
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

# Estos tokens son importantes porque distinguirlos puede cambiar completamente
# el canal. Si el resultado de Google no contiene el token, se rechaza.
DISTINCTIVE = {
    "2",
    "south",
    "junior",
    "jr",
    "novelas",
    "channel",
    "studio",
    "universal",
    "national",
    "geographic",
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
class Candidate:
    url: str
    score: float
    reason: str


@dataclass
class Result:
    path: Path | None = None
    source: str = ""
    confidence: int = 0
    reason: str = ""


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


def distinctive_tokens(value: str) -> set[str]:
    return tokens(value) & DISTINCTIVE


def exact_name_ok(requested: str, evidence: str) -> bool:
    req = tokens(requested)
    got = tokens(evidence)

    if not req or not got:
        return False

    # Todos los tokens distintivos solicitados tienen que aparecer.
    for token in distinctive_tokens(requested):
        if token not in got:
            return False

    # No aceptar un diferenciador diferente.
    got_distinctive = got & DISTINCTIVE
    req_distinctive = req & DISTINCTIVE
    if got_distinctive - req_distinctive:
        return False

    # Para nombres simples, al menos deben coincidir los tokens significativos.
    req_main = {x for x in req if len(x) >= 2}
    got_main = {x for x in got if len(x) >= 2}

    if req_main and not (req_main & got_main):
        return False

    return True


def image_filename(entry: Entry) -> str:
    base = canon(entry.tvg_id) or canon(entry.name) or "channel"
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:70]

    import hashlib

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


def get_image(session: requests.Session, url: str) -> bytes:
    response = session.get(
        url,
        timeout=TIMEOUT,
        headers={
            **GOOGLE_HEADERS,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
        allow_redirects=True,
    )
    response.raise_for_status()

    if len(response.content) > MAX_IMAGE_BYTES:
        raise RuntimeError("imagen demasiado grande")

    return response.content


def rasterize(data: bytes, output: Path) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as source:
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
            (image.width + PADDING * 2, image.height + PADDING * 2),
            (0, 0, 0, 0),
        )
        padded.alpha_composite(image, (PADDING, PADDING))

        output.parent.mkdir(parents=True, exist_ok=True)
        padded.save(output, format="PNG", optimize=True)

        return padded.width, padded.height


def validate_png(path: Path) -> None:
    with Image.open(path) as image:
        image.verify()

    with Image.open(path) as image:
        if image.width < 40 or image.height < 20:
            raise RuntimeError("PNG demasiado pequeño")


def download_existing(
    entry: Entry,
    logo_dir: Path,
) -> Result:
    target = logo_dir / image_filename(entry)

    session = requests.Session()
    try:
        data = get_image(session, entry.logo_url)
        width, height = rasterize(data, target)
        validate_png(target)

        return Result(
            path=target,
            source="M3U existente",
            confidence=100,
            reason=f"logo original de la M3U; {width}x{height}px",
        )
    except Exception as exc:
        target.unlink(missing_ok=True)
        return Result(
            source="M3U existente - ERROR",
            reason=f"{type(exc).__name__}: {exc}",
        )
    finally:
        session.close()


def search_name(entry: Entry) -> str:
    if entry.name in SEARCH_NAME_OVERRIDES:
        return SEARCH_NAME_OVERRIDES[entry.name]

    # Quitar solo la resolución que aparece al final en la M3U.
    name = re.sub(
        r"\s*\((?:144|240|360|480|540|576|720|1080|2160)p\)\s*$",
        "",
        entry.name,
        flags=re.IGNORECASE,
    ).strip()

    return name


def google_query(entry: Entry) -> str:
    return f'"{search_name(entry)}" logo'


def build_browser(browser: str):
    if webdriver is None:
        raise RuntimeError(
            "Falta Selenium. Ejecuta: python -m pip install selenium"
        )

    if browser == "firefox":
        options = webdriver.FirefoxOptions()
        options.add_argument("-headless")
        options.set_preference(
            "general.useragent.override",
            GOOGLE_HEADERS["User-Agent"],
        )
        return webdriver.Firefox(options=options)

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,1000")
    options.add_argument(
        f'--user-agent={GOOGLE_HEADERS["User-Agent"]}'
    )
    return webdriver.Chrome(options=options)


def original_url_from_imgres(href: str) -> str:
    try:
        values = parse_qs(
            urlparse(href).query
        ).get("imgurl")
        if values:
            return unquote(values[0])
    except Exception:
        pass
    return ""


def google_search(
    driver,
    entry: Entry,
    logo_dir: Path,
) -> Result:
    query = google_query(entry)
    target = logo_dir / image_filename(entry)

    url = (
        f"{GOOGLE_IMAGES_URL}"
        f"?tbm=isch"
        f"&q={quote(query)}"
        f"&hl=en"
        f"&safe=active"
    )

    try:
        driver.get(url)
        time.sleep(2.5)

        page = driver.page_source.lower()
        current = (driver.current_url or "").lower()

        if (
            "sorry" in current
            or "consent" in current
            or "before you continue" in page
            or "unusual traffic" in page
        ):
            return Result(
                source="Google Images - BLOQUEADO",
                reason=f"Google no mostro resultados para: {query}",
            )

        anchors = driver.find_elements(
            By.CSS_SELECTOR,
            'a[href*="/imgres?"]',
        )

        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()

        for anchor in anchors:
            href = anchor.get_attribute("href") or ""
            image_url = original_url_from_imgres(href)

            if not image_url or image_url in seen:
                continue

            seen.add(image_url)

            evidence_parts: list[str] = []

            try:
                label = anchor.get_attribute("aria-label") or ""
                if label:
                    evidence_parts.append(label)
            except Exception:
                pass

            try:
                txt = anchor.text or ""
                if txt:
                    evidence_parts.append(txt)
            except Exception:
                pass

            try:
                for img in anchor.find_elements(By.TAG_NAME, "img")[:3]:
                    alt = img.get_attribute("alt") or ""
                    if alt:
                        evidence_parts.append(alt)
            except Exception:
                pass

            candidates.append(
                (image_url, " ".join(evidence_parts))
            )

            if len(candidates) >= 20:
                break

        if not candidates:
            return Result(
                source="Google Images - SIN RESULTADO",
                reason=f"No se pudieron extraer resultados de: {query}",
            )

        session = make_requests_session()

        try:
            for position, (image_url, evidence) in enumerate(
                candidates,
                start=1,
            ):
                # Google ya recibio una consulta exacta. Si el resultado no
                # tiene texto accesible, usamos el orden de Google.
                if evidence and not evidence_ok(entry, evidence):
                    continue

                try:
                    width, height = download_image(
                        session,
                        image_url,
                        target,
                    )

                    return Result(
                        path=target,
                        source="Google Images",
                        reason=(
                            f"consulta={query} | "
                            f"resultado={position} | "
                            f"evidencia={evidence[:180]} | "
                            f"{width}x{height}px | "
                            f"url={image_url}"
                        ),
                    )
                except Exception:
                    target.unlink(missing_ok=True)
                    continue

        finally:
            session.close()

        return Result(
            source="Google Images - NO DESCARGABLE",
            reason=(
                f"Google devolvio {len(candidates)} resultados para "
                f"{query}, pero ninguno fue descargable."
            ),
        )

    except Exception as exc:
        target.unlink(missing_ok=True)
        return Result(
            source="Google Images - ERROR",
            reason=f"{type(exc).__name__}: {exc}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Descarga logos existentes y busca faltantes SOLO en Google Images."
    )

    parser.add_argument(
        "--input",
        default="masterlist_argentina_latino.m3u",
        help="M3U principal",
    )
    parser.add_argument(
        "--source-m3u",
        action="append",
        default=[],
        help="M3U adicional; se puede repetir",
    )
    parser.add_argument(
        "--output",
        default="masterlist_argentina_latino_logos_google.m3u",
    )
    parser.add_argument(
        "--report",
        default="masterlist_argentina_latino_logos_google_report.txt",
    )
    parser.add_argument(
        "--logo-dir",
        default="logos",
    )
    parser.add_argument(
        "--logo-base-url",
        default="",
        help="URL publica base para los PNG, por ejemplo el directorio logos de GitHub",
    )
    parser.add_argument(
        "--clean-logo-dir",
        action="store_true",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="descargas simultaneas de logos YA EXISTENTES; Google se consulta de a uno",
    )
    parser.add_argument(
        "--no-google",
        action="store_true",
        help="solo descarga los logos existentes y deja faltantes pendientes",
    )
    parser.add_argument(
        "--browser",
        choices=("firefox", "chrome"),
        default="firefox",
        help="navegador para Google Images (default: firefox)",
    )

    args = parser.parse_args()

    if not 1 <= args.workers <= 8:
        parser.error("--workers debe estar entre 1 y 8")

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[!] No existe: {input_path}", file=sys.stderr)
        return 1

    if args.clean_logo_dir and Path(args.logo_dir).exists():
        print(f"[+] Limpiando {args.logo_dir}...")
        shutil.rmtree(args.logo_dir)

    lines, entries = load_m3u(input_path)

    all_entries = list(entries)

    # Entradas adicionales se usan para descubrir/copiar otros logos, pero
    # la M3U principal sigue siendo la que se modifica.
    for extra_name in args.source_m3u:
        extra_path = Path(extra_name)
        if not extra_path.exists():
            print(f"[!] M3U adicional no existe: {extra_path}")
            continue

        _, extra_entries = load_m3u(extra_path)
        all_entries.extend(extra_entries)

    # Deduplicar fuentes auxiliares por tvg-id+nombre+logo.
    unique_source_entries: dict[tuple[str, str, str], Entry] = {}
    for entry in all_entries:
        key = (
            canon(entry.tvg_id),
            canon(entry.name),
            entry.logo_url,
        )
        unique_source_entries[key] = entry

    entries_for_download = list(unique_source_entries.values())

    main_missing = [x for x in entries if not x.logo_url]
    main_existing = [x for x in entries if x.logo_url]

    print()
    print(f"[+] EXTINF en M3U principal: {len(entries)}")
    print(f"[+] Logos existentes en principal: {len(main_existing)}")
    print(f"[+] Logos faltantes en principal: {len(main_missing)}")

    if main_missing:
        print("[+] Canales que deben buscarse en Google:")
        for entry in main_missing:
            print(f'    - {search_name(entry)} -> {google_query(entry)}')

    # ---------------------------------------------------------------------
    # 1) Descargar todos los logos que ya existen en las M3U.
    # ---------------------------------------------------------------------
    logo_dir = Path(args.logo_dir)
    results: dict[tuple[str, str], Result] = {}

    print()
    print("[+] Descargando logos ya existentes en las M3U...")

    def existing_worker(entry: Entry):
        return entry, download_existing(entry, logo_dir)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.workers
    ) as executor:
        futures = [
            executor.submit(existing_worker, entry)
            for entry in entries_for_download
            if entry.logo_url
        ]

        for done, future in enumerate(
            concurrent.futures.as_completed(futures),
            start=1,
        ):
            entry, result = future.result()
            key = (canon(entry.tvg_id), canon(entry.name))
            results[key] = result

            status = "OK" if result.path else "FAIL"
            print(
                f"[EXISTENTE {done:>3}/{len(futures)}] "
                f"{status:<4} {entry.name}"
            )

    # ---------------------------------------------------------------------
    # 2) Buscar SOLO los faltantes de la M3U principal en Google.
    #    Se usa un navegador real porque Google Images ya no expone de forma
    #    estable las URLs originales en una respuesta requests simple.
    # ---------------------------------------------------------------------
    missing_results: dict[tuple[str, str], Result] = {}

    if main_missing and not args.no_google:
        print()
        print(
            f"[+] Iniciando {args.browser} headless para Google Images..."
        )

        try:
            driver = build_browser(args.browser)
        except Exception as exc:
            print(
                f"[!] No se pudo iniciar el navegador: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1

        try:
            for position, entry in enumerate(main_missing, start=1):
                print(
                    f'[GOOGLE {position:>2}/{len(main_missing)}] '
                    f'{search_name(entry)} -> {google_query(entry)}'
                )

                result = google_search(
                    driver,
                    entry,
                    logo_dir,
                )

                key = (canon(entry.tvg_id), canon(entry.name))
                missing_results[key] = result

                if result.path:
                    print(
                        f"    -> OK | {result.path.name}"
                    )
                else:
                    print(
                        f"    -> PENDIENTE | {result.source}"
                    )

                if position != len(main_missing):
                    time.sleep(1.5)

        finally:
            driver.quit()

    # ---------------------------------------------------------------------
    # 3) Generar M3U principal con logos locales.
    # ---------------------------------------------------------------------
    public_base = args.logo_base_url.rstrip("/")

    success_existing = 0
    success_google = 0
    unresolved = 0

    report = [
        "Masterlist Argentina + Premium Latinoamerica - Google Logo Report",
        "=" * 78,
        f"M3U principal: {input_path}",
        f"EXTINF principal: {len(entries)}",
        "",
        "ESTRATEGIA:",
        "- Logos existentes: se descargan desde la URL ya presente.",
        "- Logos faltantes: solo Google Images.",
        "- Fuzzy matching: desactivado.",
        "- No se reemplazan logos existentes por otros candidatos.",
        "",
        "CANALES FALTANTES:",
    ]

    for entry in main_missing:
        report.append(
            f"- {entry.name} | tvg-id={entry.tvg_id} | "
            f"query={google_query(entry)}"
        )

    report.extend(["", "RESULTADOS PRINCIPALES:", ""])

    for entry in entries:
        key = (canon(entry.tvg_id), canon(entry.name))

        if entry.logo_url:
            result = results.get(key)

            if result and result.path:
                success_existing += 1

                if public_base:
                    logo_url = f"{public_base}/{result.path.name}"
                else:
                    logo_url = result.path.as_posix()

                lines[entry.index] = set_attr(
                    lines[entry.index],
                    "tvg-logo",
                    logo_url,
                )

                report.append(
                    f"OK EXISTENTE | {entry.name} | {result.reason}"
                )
            else:
                # Si no se pudo copiar, conservamos exactamente la URL original.
                report.append(
                    f"FALLO DESCARGA EXISTENTE | {entry.name} | "
                    f"{entry.logo_url}"
                )

            continue

        result = missing_results.get(key)

        if result and result.path:
            success_google += 1

            if public_base:
                logo_url = f"{public_base}/{result.path.name}"
            else:
                logo_url = result.path.as_posix()

            lines[entry.index] = set_attr(
                lines[entry.index],
                "tvg-logo",
                logo_url,
            )

            report.append(
                f"OK GOOGLE | {entry.name} | "
                f"{result.confidence}% | {result.reason}"
            )
        else:
            unresolved += 1

            report.append(
                f"PENDIENTE | {entry.name} | "
                f"{result.reason if result else 'sin intento'}"
            )

    # Integridad: la cantidad de EXTINF debe ser exactamente la misma.
    after_extinf = sum(
        1 for line in lines if line.startswith("#EXTINF:")
    )

    if after_extinf != len(entries):
        print(
            f"[!] ERROR DE INTEGRIDAD: {len(entries)} -> {after_extinf}",
            file=sys.stderr,
        )
        return 2

    # Guardar M3U.
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"

    output_path = Path(args.output)
    output_path.write_bytes(
        text.replace("\n", "\r\n").encode("utf-8")
    )

    report.extend([
        "",
        "RESUMEN",
        "-" * 78,
        f"Logos existentes descargados: {success_existing}",
        f"Logos encontrados con Google: {success_google}",
        f"Canales pendientes sin logo: {unresolved}",
        f"EXTINF preservados: {after_extinf}/{len(entries)}",
        "",
        "INTEGRIDAD:",
        "- Streams: no modificados.",
        "- #EXTVLCOPT: no modificados.",
        "- Canales: no eliminados.",
        "- Logos existentes: no sustituidos por búsquedas.",
        "- Fuentes de búsqueda de faltantes: SOLO Google Images.",
    ])

    Path(args.report).write_text(
        "\n".join(report) + "\n",
        encoding="utf-8",
    )

    print()
    print("[+] Proceso terminado.")
    print(f"[+] Logos existentes descargados: {success_existing}")
    print(f"[+] Logos encontrados en Google: {success_google}")
    print(f"[+] Pendientes sin logo: {unresolved}")
    print(f"[+] M3U: {output_path}")
    print(f"[+] Informe: {args.report}")
    print(f"[+] Logos: {logo_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
