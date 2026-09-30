#!/usr/bin/env python3
"""
Logo Manager - solo logos faltantes desde TVLibre Online.

Reglas:
- Lee la M3U principal.
- NO descarga ni vuelve a procesar los canales que ya tienen tvg-logo.
- Solo trabaja con entradas sin tvg-logo.
- Busca esos canales exclusivamente en TVLibre Online.
- Solo acepta coincidencias exactas; no usa fuzzy matching.
- Descarga únicamente el logo encontrado desde la página de TVLibre.
- Modifica la M3U solo para agregar tvg-logo a los canales que estaban vacíos.
- No elimina canales, streams ni #EXTVLCOPT.
- No limpia ni borra la carpeta de logos.

Dependencias:
    python -m pip install requests Pillow beautifulsoup4
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image


SITE = "https://tvlibreonline.st/"
CHANNEL_ROOT = urljoin(SITE, "en-vivo/")
TIMEOUT = 25
DOWNLOAD_RETRIES = 3
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_LOGO_SIZE = 600
PADDING = 18

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


@dataclass
class Result:
    path: Path | None = None
    source: str = ""
    reason: str = ""


def canon(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = re.sub(r"([a-z])([0-9])", r"\1 \2", value)
    value = re.sub(r"([0-9])([a-z])", r"\1 \2", value)
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def significant(value: str) -> set[str]:
    stop = {
        "tv", "channel", "canal", "online", "vivo", "en", "directo",
        "gratis", "por", "internet", "television",
    }
    return {
        token for token in canon(value).split()
        if len(token) >= 2 and token not in stop
    }


def exact_site_name(requested: str, found: str) -> bool:
    requested_tokens = significant(requested)
    found_tokens = significant(found)

    if not requested_tokens or not found_tokens:
        return False

    if not requested_tokens.issubset(found_tokens):
        return False

    distinctive = {"2", "south", "junior", "jr", "novelas"}

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

        entries.append(
            Entry(
                index=i,
                extinf=line,
                name=visible_name(line),
                tvg_id=extract_attr(line, "tvg-id"),
                logo_url=extract_attr(line, "tvg-logo"),
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


def save_png(data: bytes, target: Path) -> None:
    with Image.open(BytesIO(data)) as source:
        source.verify()

    with Image.open(BytesIO(data)) as source:
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

        target.parent.mkdir(parents=True, exist_ok=True)
        padded.save(target, format="PNG", optimize=True)

    with Image.open(target) as check:
        check.verify()


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
    response = session.get(SITE, timeout=TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    result: dict[str, str] = {}

    for anchor in soup.find_all("a", href=True):
        href = urljoin(SITE, anchor.get("href", ""))
        parsed = urlparse(href)

        if parsed.netloc and parsed.netloc != urlparse(SITE).netloc:
            continue

        if not parsed.path.startswith("/en-vivo/"):
            continue

        text = anchor.get_text(" ", strip=True)
        if not text:
            continue

        text = re.sub(
            r"^(Argentina|Deportes|Documentales|Gastronomía|Entretenimiento|Infantiles|Música|España|Estados Unidos)\s+",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s+Ver canal\s*$", "", text, flags=re.IGNORECASE).strip()

        result[canon(text)] = href

    return result


def direct_slug_candidates(entry: Entry) -> list[str]:
    return [
        urljoin(CHANNEL_ROOT, canon(name).replace(" ", "-") + "/")
        for name in site_names_for(entry)
    ]


def find_page_for_entry(
    session: requests.Session,
    entry: Entry,
    links: dict[str, str],
) -> tuple[str, str]:
    for wanted in site_names_for(entry):
        wanted_canon = canon(wanted)

        if wanted_canon in links:
            return links[wanted_canon], wanted

        for found_name, href in links.items():
            if exact_site_name(wanted, found_name):
                return href, found_name

    for candidate in direct_slug_candidates(entry):
        try:
            response = session.get(candidate, timeout=TIMEOUT)

            if not response.ok or "text/html" not in response.headers.get(
                "Content-Type", ""
            ):
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            h1 = soup.find("h1")
            title = h1.get_text(" ", strip=True) if h1 else ""

            for wanted in site_names_for(entry):
                if title and exact_site_name(wanted, title):
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

    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or ""
        )

        if not src:
            srcset = img.get("srcset") or img.get("data-srcset") or ""
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]

        if not src:
            continue

        absolute = urljoin(page_url, src)
        if not absolute.startswith(("http://", "https://")):
            continue

        alt = img.get("alt", "")
        title = img.get("title", "")
        classes = " ".join(img.get("class", []))
        evidence = " ".join([alt, title, classes])

        score = 0

        if any(exact_site_name(name, alt) for name in wanted_names if alt):
            score += 100

        if any(exact_site_name(name, title) for name in wanted_names if title):
            score += 40

        if "wp-post-image" in classes:
            score += 25
        if "post-thumbnail" in classes:
            score += 20
        if "logo" in evidence.lower():
            score += 20

        candidates.append((score, absolute, evidence))

    for meta in soup.find_all("meta", attrs={"content": True}):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if prop not in {"og:image", "og:image:url", "twitter:image"}:
            continue

        url = urljoin(page_url, meta.get("content", ""))
        if url.startswith(("http://", "https://")):
            candidates.append((45, url, f"meta={prop}"))

    candidates.sort(key=lambda item: item[0], reverse=True)

    for score, url, evidence in candidates:
        if score > 0:
            return url, evidence

    return "", ""


def download_missing(
    entry: Entry,
    logo_dir: Path,
    links: dict[str, str],
) -> Result:
    session = make_session()

    try:
        target = logo_dir / image_filename(entry)

        if target.exists():
            try:
                with Image.open(target) as check:
                    check.verify()

                return Result(
                    path=target,
                    source="LOCAL",
                    reason="PNG existente para este canal; no se volvió a descargar.",
                )
            except Exception:
                target.unlink(missing_ok=True)

        page_url, page_name = find_page_for_entry(session, entry, links)

        if not page_url:
            return Result(
                source="TVLIBRE - NO ENCONTRADO",
                reason="No se encontró una página con coincidencia exacta.",
            )

        response = session.get(page_url, timeout=TIMEOUT)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        image_url, evidence = choose_logo_image(
            soup,
            entry,
            page_name,
            page_url,
        )

        if not image_url:
            return Result(
                source="TVLIBRE - SIN IMAGEN",
                reason=f"Página encontrada: {page_url}",
            )

        data = get_bytes(session, image_url)
        save_png(data, target)

        return Result(
            path=target,
            source="TVLIBRE",
            reason=(
                f"pagina={page_url} | nombre={page_name} | "
                f"imagen={image_url} | evidencia={evidence}"
            ),
        )

    except Exception as exc:
        return Result(
            source="TVLIBRE - ERROR",
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
    parser = argparse.ArgumentParser(
        description="Agrega únicamente logos faltantes desde TVLibre Online."
    )
    parser.add_argument(
        "--input",
        default="masterlist_argentina_latino.m3u",
    )
    parser.add_argument(
        "--output",
        default="masterlist_argentina_latino_logos_tvlibre_missing.m3u",
    )
    parser.add_argument(
        "--report",
        default="masterlist_argentina_latino_logos_tvlibre_missing_report.txt",
    )
    parser.add_argument(
        "--logo-dir",
        default="logos",
        help="carpeta donde se guardan los nuevos logos (por defecto: logos)",
    )
    parser.add_argument(
        "--logo-base-url",
        default="https://raw.githubusercontent.com/D3PR3D4DOR/IPTV-Argentina/main/logos",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[!] No existe la M3U: {input_path}", file=sys.stderr)
        return 1

    lines, entries = load_m3u(input_path)
    missing = [entry for entry in entries if not entry.logo_url]
    existing = [entry for entry in entries if entry.logo_url]

    print(f"[+] EXTINF principal: {len(entries)}")
    print(f"[+] Logos existentes: {len(existing)} (NO se descargaran)")
    print(f"[+] Logos faltantes: {len(missing)}")

    if not missing:
        print("[+] No hay logos faltantes.")
        return 0

    print()
    print("[+] Canales que se buscaran exclusivamente en TVLibre:")
    for entry in missing:
        print(f"    - {entry.name}")

    print()
    print(f"[+] Leyendo catalogo de {SITE}...")

    session = make_session()
    try:
        links = site_page_links(session)
    except Exception as exc:
        print(
            f"[!] No se pudo leer TVLibre: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2
    finally:
        session.close()

    print(f"[+] Paginas de canales encontradas: {len(links)}")
    print()
    print("[+] Buscando y descargando SOLO los logos faltantes...")

    logo_dir = Path(args.logo_dir)
    logo_dir.mkdir(parents=True, exist_ok=True)

    report: list[str] = [
        "Logo Manager - solo faltantes desde TVLibre Online",
        "=" * 78,
        f"M3U: {input_path}",
        f"EXTINF: {len(entries)}",
        f"Existentes (sin tocar): {len(existing)}",
        f"Faltantes a procesar: {len(missing)}",
        "",
        "REGLAS:",
        "- Los canales con tvg-logo NO se descargan ni se modifican.",
        "- Solo los canales sin tvg-logo se buscan en TVLibre.",
        "- Coincidencia exacta; fuzzy matching desactivado.",
        "- No se eliminan canales ni streams.",
        "",
    ]

    ok = 0
    pending = 0

    for position, entry in enumerate(missing, start=1):
        print(f"[TVLIBRE {position:>2}/{len(missing)}] {entry.name}")

        result = download_missing(entry, logo_dir, links)

        if result.path:
            public_url = (
                f"{args.logo_base_url.rstrip('/')}/{result.path.name}"
                if args.logo_base_url
                else result.path.as_posix()
            )

            lines[entry.index] = set_attr(
                lines[entry.index],
                "tvg-logo",
                public_url,
            )

            ok += 1
            print(f"    -> OK | {result.path.name}")
            report.append(
                f"OK | {entry.name} | {result.reason} | logo={public_url}"
            )
        else:
            pending += 1
            print(f"    -> PENDIENTE | {result.source}")
            report.append(
                f"PENDIENTE | {entry.name} | {result.source} | {result.reason}"
            )

        if position != len(missing):
            time.sleep(0.4)

    before = len(entries)
    after = sum(1 for line in lines if line.startswith("#EXTINF:"))

    if before != after:
        print(
            f"[!] ERROR DE INTEGRIDAD: EXTINF {before} -> {after}",
            file=sys.stderr,
        )
        return 3

    output_path = Path(args.output)
    report_path = Path(args.report)

    save_m3u(output_path, lines)

    report.extend([
        "",
        "RESUMEN",
        "-" * 78,
        f"EXTINF final: {after}/{before}",
        f"Logos faltantes agregados: {ok}",
        f"Logos faltantes pendientes: {pending}",
        "",
        "GARANTIAS:",
        "- Los logos que ya estaban en la M3U no fueron descargados.",
        "- Los logos que ya estaban en la M3U no fueron reemplazados.",
        "- No se borró la carpeta logos.",
        "- No se modificaron streams ni #EXTVLCOPT.",
        "- No se eliminaron canales.",
    ])

    report_path.write_text(
        "\n".join(report) + "\n",
        encoding="utf-8",
    )

    print()
    print("[+] Proceso terminado.")
    print(f"[+] Logos existentes sin tocar: {len(existing)}")
    print(f"[+] Logos faltantes agregados: {ok}")
    print(f"[+] Logos faltantes pendientes: {pending}")
    print(f"[+] M3U: {output_path}")
    print(f"[+] Informe: {report_path}")
    print(f"[+] Logos nuevos: {logo_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
