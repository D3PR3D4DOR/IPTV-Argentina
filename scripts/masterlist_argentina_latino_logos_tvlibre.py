#!/usr/bin/env python3
"""
Masterlist Argentina + Premium Latinoamerica - TVLibre Online Logo Manager

Estrategia:
- Descarga todos los logos que YA existen en la M3U desde sus URLs originales.
- No reemplaza esos logos por búsquedas externas.
- Nunca borra la carpeta de logos ni elimina un archivo anterior por un FAIL.
- Detecta respuestas sospechosamente idénticas de muchas URLs distintas para evitar
  guardar una misma imagen placeholder en cientos de canales.
- Detecta los canales que no tienen tvg-logo.
- Para los faltantes, busca el canal en https://tvlibreonline.st/
- Extrae el logo de la página del canal.
- Solo acepta una imagen cuando el nombre del canal de la página coincide
  con el canal buscado; no usa fuzzy matching para elegir otro canal.
- Si el sitio no tiene ese canal o no se puede identificar su logo, queda
  pendiente para revisión manual.
- No modifica streams, #EXTVLCOPT ni elimina canales.

Dependencias:
    python -m pip install requests Pillow beautifulsoup4

Uso:
    python scripts/masterlist_argentina_latino_logos_tvlibre.py \
      --input masterlist_argentina_latino.m3u \
      --output masterlist_argentina_latino_logos_tvlibre.m3u \
      --report masterlist_argentina_latino_logos_tvlibre_report.txt \
      --logo-dir logos \
      --logo-base-url "https://raw.githubusercontent.com/D3PR3D4DOR/IPTV-Argentina/main/logos" \
      --clean-logo-dir

Para probar solo la detección de faltantes:
    --no-download-existing
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import re
import shutil
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image


SITE = "https://tvlibreonline.st/"
CHANNEL_ROOT = urljoin(SITE, "en-vivo/")
TIMEOUT = 25
DOWNLOAD_RETRIES = 3
EXISTING_WORKERS = 8
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_LOGO_SIZE = 600
PADDING = 18


# El sitio usa algunos nombres distintos al nombre que tenemos en la M3U.
SITE_NAME_OVERRIDES = {
    "AMC": ["AMC"],
    "AXN": ["AXN"],
    "AXN South": ["AXN South"],
    "Cinecanal": ["Cinecanal"],
    "Comedy Central": ["Comedy Central"],
    "Disney Channel": ["Disney Channel"],
    "Disney Jr.": ["Disney JR", "Disney Junior"],
    "FX": ["FX"],
    "History 2": ["History Channel 2", "History 2"],
    "History": ["History Channel", "History"],
    "Lifetime": ["Lifetime"],
    "National Geographic": ["National Geographic"],
    "Sony Channel": ["Sony Channel"],
    "Star Channel": ["Star Channel"],
    "Studio Universal": ["Studio Universal"],
    "TNT Novelas": ["TNT Novelas"],
    "Universal TV": ["Universal TV"],
}


SITE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.7",
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
    content_sha256: str = ""


def canon(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )
    value = value.lower()
    value = re.sub(r"([a-z])([0-9])", r"\1 \2", value)
    value = re.sub(r"([0-9])([a-z])", r"\1 \2", value)
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def significant(value: str) -> set[str]:
    stop = {
        "tv",
        "channel",
        "canal",
        "online",
        "vivo",
        "en",
        "directo",
        "gratis",
        "por",
        "internet",
        "television",
    }
    return {
        x for x in canon(value).split()
        if len(x) >= 2 and x not in stop
    }


def exact_site_name(requested: str, found: str) -> bool:
    requested_tokens = significant(requested)
    found_tokens = significant(found)

    if not requested_tokens or not found_tokens:
        return False

    # Todos los términos significativos pedidos deben aparecer.
    if not requested_tokens.issubset(found_tokens):
        return False

    # No aceptar variantes diferentes para diferenciadores importantes.
    distinctive = {
        "2",
        "south",
        "junior",
        "jr",
        "novelas",
    }

    if (found_tokens & distinctive) - (requested_tokens & distinctive):
        return False

    return True


def extract_attr(line: str, attr: str) -> str:
    match = re.search(
        rf'{re.escape(attr)}="([^"]*)"',
        line,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


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


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(SITE_HEADERS)
    return session


def get_bytes(session: requests.Session, url: str) -> bytes:
    last_error: Exception | None = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            response = session.get(
                url,
                timeout=TIMEOUT,
                allow_redirects=True,
                headers={
                    **SITE_HEADERS,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            response.raise_for_status()

            if len(response.content) > MAX_IMAGE_BYTES:
                raise RuntimeError("imagen demasiado grande")

            return response.content

        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc

            if attempt < DOWNLOAD_RETRIES:
                time.sleep(0.6 * attempt)

    raise RuntimeError(
        f"No se pudo descargar tras {DOWNLOAD_RETRIES} intentos: {last_error}"
    )


def image_filename(entry: Entry) -> str:
    base = re.sub(
        r"[^a-z0-9]+",
        "-",
        canon(entry.tvg_id or entry.name),
    ).strip("-")[:70]

    digest = hashlib.sha1(
        f"{entry.tvg_id}|{entry.name}".encode("utf-8")
    ).hexdigest()[:10]

    return f"{base}-{digest}.png"


def save_png(data: bytes, target: Path) -> tuple[int, int]:
    with Image.open(__import__("io").BytesIO(data)) as source:
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

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        padded.save(
            target,
            format="PNG",
            optimize=True,
        )

    with Image.open(target) as check:
        check.verify()

    return padded.width, padded.height


def download_existing(
    entry: Entry,
    logo_dir: Path,
    force_redownload: bool = False,
) -> Result:
    target = logo_dir / image_filename(entry)

    # Si ya tenemos un PNG válido, no lo volvemos a descargar ni lo pisamos.
    # Esto evita destruir resultados buenos por un FAIL remoto.
    if target.exists() and not force_redownload:
        try:
            with Image.open(target) as check:
                check.verify()

            return Result(
                path=target,
                source="M3U - EXISTENTE LOCAL",
                reason=(
                    "PNG local conservado; "
                    f"URL original={entry.logo_url}"
                ),
            )
        except Exception:
            # Un archivo local inválido sí se puede regenerar.
            target.unlink(missing_ok=True)

    session = make_session()

    try:
        data = get_bytes(
            session,
            entry.logo_url,
        )

        # Validar que realmente sea una imagen antes de guardarla.
        with Image.open(__import__("io").BytesIO(data)) as check:
            check.verify()

        content_sha256 = hashlib.sha256(data).hexdigest()

        width, height = save_png(
            data,
            target,
        )

        return Result(
            path=target,
            source="M3U",
            reason=(
                f"URL original; {width}x{height}px; "
                f"{entry.logo_url}"
            ),
            content_sha256=content_sha256,
        )

    except Exception as exc:
        # IMPORTANTE: no borrar un PNG anterior por un fallo remoto.
        target_exists = target.exists()

        return Result(
            path=target if target_exists else None,
            source="M3U - ERROR DESCARGA",
            reason=(
                f"{type(exc).__name__}: {exc} | "
                f"{entry.logo_url}"
                + (" | PNG local conservado" if target_exists else "")
            ),
        )

    finally:
        session.close()

def site_names_for(entry: Entry) -> list[str]:
    if entry.name in SITE_NAME_OVERRIDES:
        return SITE_NAME_OVERRIDES[entry.name]

    clean = re.sub(
        r"\s*\((?:144|240|360|480|540|576|720|1080|1440|2160)p\)\s*$",
        "",
        entry.name,
        flags=re.IGNORECASE,
    ).strip()

    return [clean]


def site_page_links(session: requests.Session) -> dict[str, str]:
    response = session.get(
        SITE,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    result: dict[str, str] = {}

    for anchor in soup.find_all("a", href=True):
        href = urljoin(
            SITE,
            anchor.get("href", ""),
        )

        parsed = urlparse(href)

        if (
            parsed.netloc
            and parsed.netloc != urlparse(SITE).netloc
        ):
            continue

        if not parsed.path.startswith("/en-vivo/"):
            continue

        text = anchor.get_text(
            " ",
            strip=True,
        )

        if not text:
            continue

        # El texto de la home suele ser "Argentina AXN Ver canal".
        text = re.sub(
            r"^(Argentina|Deportes|Documentales|Gastronomía|Entretenimiento|Infantiles|Música|España|Estados Unidos)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        )

        text = re.sub(
            r"\s+Ver canal\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

        result[canon(text)] = href

    return result


def direct_slug(entry: Entry) -> str:
    name = site_names_for(entry)[0]
    slug = canon(name).replace(" ", "-")
    return urljoin(CHANNEL_ROOT, slug + "/")


def find_page_for_entry(
    session: requests.Session,
    entry: Entry,
    links: dict[str, str],
) -> tuple[str, str]:
    candidates = site_names_for(entry)

    # Primero links descubiertos desde la home.
    for wanted in candidates:
        wanted_canon = canon(wanted)

        for found_name, href in links.items():
            if exact_site_name(wanted, found_name):
                return href, found_name

        # Nombre completamente idéntico.
        if wanted_canon in links:
            return links[wanted_canon], wanted

    # Fallback: slug directo para no depender únicamente del menú.
    for wanted in candidates:
        candidate = urljoin(
            CHANNEL_ROOT,
            canon(wanted).replace(" ", "-") + "/",
        )

        try:
            r = session.get(
                candidate,
                timeout=TIMEOUT,
            )

            if r.ok and "text/html" in r.headers.get(
                "Content-Type",
                "",
            ):
                soup = BeautifulSoup(
                    r.text,
                    "html.parser",
                )

                h1 = soup.find("h1")
                title = (
                    h1.get_text(" ", strip=True)
                    if h1
                    else ""
                )

                if title and exact_site_name(
                    wanted,
                    title,
                ):
                    return candidate, title

        except requests.RequestException:
            continue

    return "", ""


def choose_logo_image(
    soup: BeautifulSoup,
    entry: Entry,
    page_name: str,
    page_url: str,
) -> tuple[str, str]:
    wanted_names = site_names_for(entry) + [page_name]

    candidates: list[tuple[int, str, str]] = []

    # Featured images / imágenes con alt: es la fuente preferida.
    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or ""
        )

        if not src:
            srcset = (
                img.get("srcset")
                or img.get("data-srcset")
                or ""
            )
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]

        if not src:
            continue

        absolute = urljoin(
            page_url,
            src,
        )

        if not absolute.startswith(("http://", "https://")):
            continue

        alt = img.get("alt", "")
        title = img.get("title", "")
        classes = " ".join(
            img.get("class", [])
        )

        evidence = " ".join(
            [alt, title, classes]
        )

        score = 0

        if any(
            exact_site_name(name, alt)
            for name in wanted_names
            if alt
        ):
            score += 100

        if any(
            exact_site_name(name, title)
            for name in wanted_names
            if title
        ):
            score += 40

        if "wp-post-image" in classes:
            score += 25

        if "post-thumbnail" in classes:
            score += 20

        if "logo" in evidence.lower():
            score += 20

        if canon(page_name) and (
            canon(page_name) in canon(alt)
            or canon(page_name) in canon(title)
        ):
            score += 30

        candidates.append(
            (score, absolute, evidence)
        )

    # Fallback: OpenGraph / Twitter image.
    for meta in soup.find_all(
        "meta",
        attrs={"content": True},
    ):
        prop = (
            meta.get("property")
            or meta.get("name")
            or ""
        ).lower()

        if prop not in {
            "og:image",
            "og:image:url",
            "twitter:image",
        }:
            continue

        url = urljoin(
            page_url,
            meta.get("content", ""),
        )

        if url.startswith(("http://", "https://")):
            candidates.append(
                (
                    45,
                    url,
                    f"meta={prop}",
                )
            )

    if not candidates:
        return "", ""

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    for score, url, evidence in candidates:
        if score <= 0:
            continue

        return url, evidence

    return "", ""


def download_from_site(
    entry: Entry,
    logo_dir: Path,
    links: dict[str, str],
) -> Result:
    session = make_session()

    try:
        page_url, page_name = find_page_for_entry(
            session,
            entry,
            links,
        )

        if not page_url:
            return Result(
                source="TVLibre - NO ENCONTRADO",
                reason=(
                    "No se encontró una página del canal "
                    "con coincidencia exacta."
                ),
            )

        response = session.get(
            page_url,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        image_url, evidence = choose_logo_image(
            soup,
            entry,
            page_name,
            page_url,
        )

        if not image_url:
            return Result(
                source="TVLibre - SIN IMAGEN",
                reason=f"Página encontrada: {page_url}",
            )

        target = logo_dir / image_filename(entry)

        try:
            data = get_bytes(
                session,
                image_url,
            )

            width, height = save_png(
                data,
                target,
            )

            return Result(
                path=target,
                source="TVLibre",
                reason=(
                    f"pagina={page_url} | "
                    f"nombre={page_name} | "
                    f"imagen={image_url} | "
                    f"evidencia={evidence} | "
                    f"{width}x{height}px"
                ),
            )

        except Exception as exc:
            target.unlink(missing_ok=True)

            return Result(
                source="TVLibre - ERROR IMAGEN",
                reason=(
                    f"{type(exc).__name__}: {exc} | "
                    f"imagen={image_url}"
                ),
            )

    except requests.RequestException as exc:
        return Result(
            source="TVLibre - ERROR PAGINA",
            reason=f"{type(exc).__name__}: {exc}",
        )

    finally:
        session.close()


def save_m3u(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines)

    if not text.endswith("\n"):
        text += "\n"

    path.write_bytes(
        text.replace("\n", "\r\n").encode("utf-8")
    )


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input",
        default="masterlist_argentina_latino.m3u",
    )
    parser.add_argument(
        "--output",
        default="masterlist_argentina_latino_logos_tvlibre.m3u",
    )
    parser.add_argument(
        "--report",
        default="masterlist_argentina_latino_logos_tvlibre_report.txt",
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
        "--clean-logo-dir",
        action="store_true",
    )
    parser.add_argument(
        "--no-download-existing",
        action="store_true",
        help="no descargar los logos que ya existen",
    )
    parser.add_argument(
        "--force-redownload-existing",
        action="store_true",
        help="volver a descargar los logos existentes y reemplazar el PNG local",
    )
    parser.add_argument(
        "--no-site",
        action="store_true",
        help="no buscar los logos faltantes en TVLibre",
    )
    parser.add_argument(
        "--source-m3u",
        action="append",
        default=[],
        help="M3U adicional; sirve para copiar logos exactos de otra M3U",
    )

    args = parser.parse_args()

    input_path = Path(args.input)

    if not input_path.exists():
        print(
            f"[!] No existe la M3U: {input_path}",
            file=sys.stderr,
        )
        return 1

    if args.clean_logo_dir:
        print(
            "[!] --clean-logo-dir queda deshabilitado por seguridad: "
            "el script NO borra la carpeta de logos ni sus archivos."
        )

    lines, entries = load_m3u(input_path)

    additional_entries: list[Entry] = []

    for source_path in args.source_m3u:
        p = Path(source_path)

        if not p.exists():
            print(
                f"[!] M3U adicional no existe: {p}"
            )
            continue

        _, extra = load_m3u(p)
        additional_entries.extend(extra)

        print(
            f"[+] M3U adicional cargada: "
            f"{p} | {len(extra)} entradas"
        )

    existing = [
        e for e in entries
        if e.logo_url
    ]

    missing = [
        e for e in entries
        if not e.logo_url
    ]

    print()
    print(
        f"[+] EXTINF principal: {len(entries)}"
    )
    print(
        f"[+] Logos existentes: {len(existing)}"
    )
    print(
        f"[+] Logos faltantes: {len(missing)}"
    )

    if missing:
        print()
        print(
            "[+] Logos faltantes que se buscaran en TVLibre:"
        )

        for entry in missing:
            print(
                f"    - {entry.name}"
            )

    logo_dir = Path(args.logo_dir)
    logo_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ---------------------------------------------------------------
    # 1) Descargar todos los logos ya existentes.
    #    Se descarga cada URL única una sola vez y luego se copia el PNG
    #    a cada canal que comparte esa misma URL. Esto evita falsos FAIL
    #    en canales como Telefe Interior/Telefe Rosario.
    # ---------------------------------------------------------------
    existing_results: dict[tuple[str, str], Result] = {}

    if not args.no_download_existing:
        print()
        print(
            "[+] Descargando logos ya existentes "
            "desde sus URLs originales..."
        )

        url_owner: dict[str, Entry] = {}
        for entry in existing + [
            x for x in additional_entries if x.logo_url
        ]:
            url_owner.setdefault(entry.logo_url, entry)

        jobs = list(url_owner.values())

        def download_unique(entry: Entry) -> tuple[str, Entry, Result]:
            result = download_existing(
                entry,
                logo_dir,
                force_redownload=args.force_redownload_existing,
            )
            return entry.logo_url, entry, result

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=EXISTING_WORKERS
        ) as executor:
            futures = [
                executor.submit(download_unique, entry)
                for entry in jobs
            ]

            downloaded_by_url: dict[str, tuple[Entry, Result]] = {}

            for position, future in enumerate(
                concurrent.futures.as_completed(futures),
                start=1,
            ):
                url, entry, result = future.result()

                status = "OK" if result.path else "FAIL"

                print(
                    f"[M3U {position:>3}/{len(jobs)}] "
                    f"{status:<4} "
                    f"{entry.name}"
                )

                downloaded_by_url[url] = (entry, result)

            # Varias URLs distintas devolviendo exactamente la misma imagen
            # suele indicar placeholder, hotlink blocker o respuesta proxy.
            # No guardamos esos resultados para evitar llenar logos/ con
            # cientos de copias del mismo logo incorrecto.
            hash_to_urls: dict[str, list[str]] = {}

            for url, (_, result) in downloaded_by_url.items():
                if result.path and result.content_sha256:
                    hash_to_urls.setdefault(
                        result.content_sha256,
                        [],
                    ).append(url)

            suspicious_hashes = {
                sha
                for sha, urls in hash_to_urls.items()
                if len(urls) >= 4
            }

            if suspicious_hashes:
                print()
                print(
                    "[!] Se detectaron imágenes sospechosamente repetidas "
                    f"en {sum(len(hash_to_urls[h]) for h in suspicious_hashes)} "
                    "URLs distintas."
                )
                print(
                    "[!] Esas imágenes NO se usarán como logos válidos."
                )

            for url, (entry, result) in downloaded_by_url.items():
                if (
                    result.content_sha256
                    and result.content_sha256 in suspicious_hashes
                    and result.source == "M3U"
                ):
                    result.path.unlink(missing_ok=True)

                    downloaded_by_url[url] = (
                        entry,
                        Result(
                            path=None,
                            source="M3U - IMAGEN SOSPECHOSA",
                            reason=(
                                "La URL devolvió una imagen idéntica a "
                                "3 o más URLs distintas; posible placeholder. "
                                f"url={url}"
                            ),
                        ),
                    )

            # Guardar/copiar el resultado del URL original solo después
            # de pasar la validación anterior.
            for position, (url, (entry, result)) in enumerate(
                downloaded_by_url.items(),
                start=1,
            ):
                shared_entries = [
                    x for x in existing
                    if x.logo_url == url
                ] + [
                    x for x in additional_entries
                    if x.logo_url == url
                ]

                for target_entry in shared_entries:
                    target_key = (
                        canon(target_entry.tvg_id),
                        canon(target_entry.name),
                    )

                    target = logo_dir / image_filename(target_entry)

                    if result.path:
                        if target_entry is entry:
                            existing_results[target_key] = result
                            continue

                        try:
                            shutil.copy2(
                                result.path,
                                target,
                            )
                            existing_results[target_key] = Result(
                                path=target,
                                source="M3U (URL compartida)",
                                reason=(
                                    f"misma URL de logo que {entry.name}; "
                                    f"{url}"
                                ),
                            )
                        except Exception as exc:
                            existing_results[target_key] = Result(
                                source="M3U - ERROR COPIA",
                                reason=(
                                    f"{type(exc).__name__}: {exc} | "
                                    f"url={url}"
                                ),
                            )
                    else:
                        existing_results[target_key] = result

    # ---------------------------------------------------------------
    # 2) Preparar links de TVLibre y resolver faltantes.
    # ---------------------------------------------------------------
    site_links: dict[str, str] = {}

    if missing and not args.no_site:
        print()
        print(
            f"[+] Leyendo el catalogo de {SITE}..."
        )

        site_session = make_session()

        try:
            site_links = site_page_links(
                site_session
            )
            print(
                f"[+] Paginas de canales encontradas en el catalogo: "
                f"{len(site_links)}"
            )
        except Exception as exc:
            print(
                f"[!] No se pudo leer el catalogo de TVLibre: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        finally:
            site_session.close()

    missing_results: dict[tuple[str, str], Result] = {}

    if missing and not args.no_site:
        print()
        print(
            "[+] Extrayendo logos faltantes desde TVLibre..."
        )

        # Secuencial para no cargar innecesariamente el sitio.
        for position, entry in enumerate(
            missing,
            start=1,
        ):
            print(
                f"[TVLIBRE {position:>2}/{len(missing)}] "
                f"{entry.name}"
            )

            result = download_from_site(
                entry,
                logo_dir,
                site_links,
            )

            key = (
                canon(entry.tvg_id),
                canon(entry.name),
            )

            missing_results[key] = result

            if result.path:
                print(
                    f"    -> OK | {result.path.name}"
                )
            else:
                print(
                    f"    -> PENDIENTE | {result.source}"
                )

            if position != len(missing):
                time.sleep(0.5)

    # ---------------------------------------------------------------
    # 3) Aplicar solo logos que realmente faltaban.
    # ---------------------------------------------------------------
    public_base = args.logo_base_url.rstrip("/")

    ok_existing = 0
    fail_existing = 0
    ok_site = 0
    pending_site = 0

    report: list[str] = [
        "Masterlist Argentina + Premium Latinoamerica",
        "Logo Manager - TVLibre Online",
        "=" * 78,
        f"M3U principal: {input_path}",
        "",
        "REGLAS:",
        "- Logos existentes: se conservan; se usa su URL original como fuente.",
        "- Logos faltantes: se buscan exclusivamente en TVLibre Online.",
        "- Fuzzy matching: desactivado.",
        "- No se modifican streams ni #EXTVLCOPT.",
        "- Nunca se borra la carpeta de logos por un FAIL.",
        "- Respuestas idénticas de muchas URLs distintas se marcan como sospechosas.",
        "",
    ]

    for entry in entries:
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
                    f"OK EXISTENTE | {entry.name} | {result.reason}"
                )
            else:
                fail_existing += 1

                report.append(
                    f"FAIL EXISTENTE | {entry.name} | "
                    f"{result.reason if result else 'sin resultado'}"
                )

            continue

        result = missing_results.get(key)

        if result and result.path:
            ok_site += 1

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
                f"OK TVLIBRE | {entry.name} | {result.reason}"
            )
        else:
            pending_site += 1

            report.append(
                f"PENDIENTE | {entry.name} | "
                f"{result.reason if result else 'sin resultado'}"
            )

    before_extinf = len(entries)
    after_extinf = sum(
        1 for line in lines
        if line.startswith("#EXTINF:")
    )

    if before_extinf != after_extinf:
        print(
            f"[!] ERROR DE INTEGRIDAD: "
            f"{before_extinf} -> {after_extinf}",
            file=sys.stderr,
        )
        return 2

    output_path = Path(args.output)
    report_path = Path(args.report)

    # El directorio de logos es deliberadamente acumulativo y seguro:
    # no se limpia automáticamente.


    save_m3u(
        output_path,
        lines,
    )

    report.extend([
        "",
        "RESUMEN",
        "-" * 78,
        f"EXTINF: {after_extinf}/{before_extinf}",
        f"Logos existentes descargados: {ok_existing}",
        f"Logos existentes con error real: {fail_existing}",
        f"Logos faltantes resueltos por TVLibre: {ok_site}",
        f"Logos faltantes pendientes: {pending_site}",
        "",
        "INTEGRIDAD:",
        "- Streams: no modificados.",
        "- #EXTVLCOPT: no modificados.",
        "- Canales: no eliminados.",
        "- Logos existentes: no sustituidos.",
        "- Fuzzy matching: desactivado.",
        "- Fuente de logos faltantes: TVLibre Online.",
    ])

    report_path.write_text(
        "\n".join(report) + "\n",
        encoding="utf-8",
    )

    print()
    print(
        "[+] Proceso terminado."
    )
    print(
        f"[+] Logos existentes descargados: {ok_existing}"
    )
    print(
        f"[+] Logos existentes con error: {fail_existing}"
    )
    print(
        f"[+] Logos faltantes encontrados en TVLibre: {ok_site}"
    )
    print(
        f"[+] Logos faltantes pendientes: {pending_site}"
    )
    print(
        f"[+] M3U: {output_path}"
    )
    print(
        f"[+] Informe: {report_path}"
    )
    print(
        f"[+] Logos: {logo_dir}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
