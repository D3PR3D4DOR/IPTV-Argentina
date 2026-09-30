#!/usr/bin/env python3
"""
Masterlist Argentina + Premium Latinoamérica v8

Objetivo:
- reunir candidatos de varias listas públicas;
- agrupar duplicados por canal;
- comprobar en paralelo cuáles streams están realmente utilizables;
- inspeccionar HLS (.m3u8) para detectar resolución y bitrate cuando están publicados;
- elegir automáticamente el mejor candidato que esté ONLINE;
- guardar una sola entrada por canal en la Masterlist;
- generar un informe detallado de todos los candidatos.

Archivos generados:
  masterlist_argentina_latino_v8.m3u
  masterlist_argentina_latino_v8_report.txt

Uso normal:
  python3 masterlist_argentina_latino_v5.py

Prueba con más detalle:
  python3 masterlist_argentina_latino_v5.py --workers 20 --timeout 12

NOTA:
- Solo se consultan URLs HTTP/HTTPS que ya aparecen en las fuentes.
- No intenta acceder a DRM, cuentas, claves ni protecciones.
- La calidad "desconocida" no se inventa: se registra como N/D.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit

import requests


# ---------------------------------------------------------------------------
# FUENTES
# ---------------------------------------------------------------------------

ARG_SOURCES = [
    ("Argentina - dearbulut", "https://dearbulut.github.io/iptv/playlists/country/ar.m3u"),
    ("Argentina - iptv-org", "https://iptv-org.github.io/iptv/countries/ar.m3u"),
    ("Argentina - okmaxx", "https://raw.githubusercontent.com/okmaxx/Argentina-IPTV/refs/heads/main/playlist/argentina_completa.m3u"),
    ("Argentina - camammoli", "https://raw.githubusercontent.com/camammoli/iptv/refs/heads/master/canales.m3u"),
    ("Argentina - reynaldo1311", "https://raw.githubusercontent.com/reynaldo1311/listas/refs/heads/main/argentina.m3u"),
]

LATAM_SOURCES = [
    (
        "Premium LATAM - iptv-org",
        "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/us.m3u",
        "premium",
    ),
    (
        "Premium LATAM - community 1",
        "https://gist.githubusercontent.com/darkmago1981/36b33d2f4df1f5dc3485e043e4ef7338/raw/",
        "premium_community",
    ),
    (
        "Premium LATAM - community 2",
        "https://gist.githubusercontent.com/anherukun/786e819a34d4af2d81aa8bf1f9f1d615/raw/",
        "premium_community",
    ),
    (
        "Premium LATAM - PABLOPOM",
        "https://raw.githubusercontent.com/PABLOPOM/m3u/main/TELEVISION3",
        "premium_community",
    ),
]

PREMIUM = {
    "a&e", "amc", "animal planet", "axn",
    "boomerang", "cartoon network", "cartoonito",
    "cinecanal", "cinemax", "comedy central",
    "discovery", "discovery channel", "discovery kids",
    "discovery science", "discovery world", "discovery home & health",
    "discovery theater", "discovery theatre",
    "disney channel", "disney junior", "disney xd",
    "e!", "fx", "food network",
    "hbo", "hbo 2", "hbo family", "hbo signature", "hbo plus",
    "hbo pop", "hbo mundi", "hbo xtreme",
    "history", "history 2", "history channel", "history channel 2",
    "hgtv", "htv", "investigation discovery", "id",
    "lifetime", "mtv",
    "national geographic", "nat geo", "nat geo wild",
    "nickelodeon", "nick jr",
    "paramount channel", "paramount network",
    "sony", "sony channel", "space", "star channel",
    "studio universal", "sundance channel", "syfy",
    "tcm", "tnt", "tnt series", "tnt novelas",
    "tooncast", "tlc", "universal tv", "universal channel",
    "warner channel", "warner", "adult swim",
}

FOREIGN_BAD = (
    "españa", "spain", "portugal", "brasil", "brazil",
    "united states", "usa", "canada", "france", "italy", "germany",
)

LATAM_MARKERS = (
    "latin america", "latinoamerica", "latinoamérica", "panregional",
    "south", "andes", "mexico", "chile", "argentina",
    "central america", "america latina", "américa latina",
)

EXCLUDE_ARG_NAMES = {
    "3/24",
    "7nn noticias",
    "canal 24 horas",
}

EXCLUDE_ARG_CLOSED = {
    "canal e",
    "ip noticias",
}

UA = "Masterlist-Argentina-LATAM/5.0"
SOURCE_TIMEOUT = 30


# ---------------------------------------------------------------------------
# MODELOS
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    extinf: str
    url: str
    source: str
    tvg_id: str
    name: str
    group: str
    kind: str  # argentina | premium

    def clean_name(self) -> str:
        return clean_name(self.name)


@dataclass
class Probe:
    candidate: Candidate
    online: bool = False
    http_status: Optional[int] = None
    final_url: str = ""
    content_type: str = ""
    elapsed_ms: int = 0
    stream_type: str = "unknown"  # master | media | non_hls | unknown
    resolution_w: int = 0
    resolution_h: int = 0
    bitrate: int = 0
    variants: int = 0
    working_variant: str = ""
    working_segment: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def resolution_label(self) -> str:
        if self.resolution_w and self.resolution_h:
            if self.resolution_h >= 1080:
                return "1080p"
            if self.resolution_h >= 720:
                return "720p"
            if self.resolution_h >= 576:
                return "576p"
            if self.resolution_h >= 480:
                return "480p"
            if self.resolution_h >= 360:
                return "360p"
            return f"{self.resolution_h}p"
        return "N/D"

    @property
    def score(self) -> int:
        """
        Puntaje técnico para elegir entre streams ONLINE.
        La resolución pesa más que el bitrate, pero ambos cuentan.
        HTTPS, feed regional y HLS suman como desempate.
        """
        if not self.online:
            return -1

        height = self.resolution_h or 0
        width = self.resolution_w or 0

        score = 0
        score += min(height, 2160) * 100
        score += min(width, 3840) // 4

        if self.bitrate:
            # Saturación para no dar demasiado peso a un bitrate enorme.
            score += min(self.bitrate, 20_000_000) // 10_000

        if self.stream_type == "master":
            score += 100
        elif self.stream_type == "media":
            score += 40

        if self.final_url.lower().startswith("https://"):
            score += 20

        text = f"{self.candidate.tvg_id} {self.candidate.name}".lower()
        if "south" in text or "argentina" in text:
            score += 80
        elif "panregional" in text or "latin america" in text:
            score += 70
        elif "andes" in text:
            score += 60
        elif "mexico" in text:
            score += 30
        elif "chile" in text:
            score += 20

        if "1080p" in text:
            score += 10
        elif "720p" in text:
            score += 6

        # Fuentes con stream-check de terceros tienen una pequeña ventaja,
        # pero nunca superan a una calidad claramente mayor.
        source = self.candidate.source.lower()
        if "dearbulut" in source:
            score += 30
        elif "iptv-org" in source:
            score += 20
        elif "pablopom" in source:
            score += 10
        elif "community" in source:
            score += 5

        return score


# ---------------------------------------------------------------------------
# PARSING Y NORMALIZACIÓN
# ---------------------------------------------------------------------------

def canon(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"\([^)]*\)", "", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_name(s: str) -> str:
    s = re.sub(r"\s*\[[^\]]*\]", "", s)
    s = re.sub(
        r"\s*\((?:1080p|720p|576p|540p|480p|360p|240p)\)",
        "",
        s,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", s).strip()


def premium_canon(s: str) -> str:
    n = canon(s)
    aliases = {
        "a e latin america": "a&e",
        "amc latin america": "amc",
        "axn latin america": "axn",
        "axn latin america south": "axn",
        "cinemax hits": "cinemax",
        "comedy central latin america": "comedy central",
        "discovery channel latin america": "discovery channel",
        "discovery kids latin america": "discovery kids",
        "disney channel latin america": "disney channel",
        "disney channel latin america panregional hd": "disney channel",
        "disney jr latin america": "disney junior",
        "disney jr latin america south hd": "disney junior",
        "e latin america": "e!",
        "elatinamerica": "e!",
        "fx latin america": "fx",
        "history latin america": "history",
        "history 2 latin america": "history 2",
        "lifetime latin america": "lifetime",
        "national geographic latin america": "national geographic",
        "nat geo latin america": "national geographic",
        "nickelodeon latin america": "nickelodeon",
        "sony channel andes": "sony",
        "sony channel": "sony",
        "space latin america": "space",
        "star channel latin america": "star channel",
        "studio universal latin america": "studio universal",
        "tnt latin america": "tnt",
        "tnt series latin america": "tnt series",
        "tnt novelas latin america": "tnt novelas",
        "universal tv latin america": "universal tv",
        "universal channel latin america": "universal channel",
        "warner channel latin america": "warner channel",
        "warner": "warner channel",
        "nick": "nickelodeon",
        "warner channel": "warner channel",
    }
    n = re.sub(r"\b(?:1080p|720p|576p|540p|480p|360p|240p)\b", "", n).strip()
    return aliases.get(n, n)


def parse_extinf(line: str) -> tuple[str, str, str]:
    m = re.search(r'tvg-id="([^"]*)"', line, re.I)
    tvg_id = m.group(1).strip() if m else ""

    m = re.search(r'group-title="([^"]*)"', line, re.I)
    group = m.group(1).strip() if m else ""

    name = line.rsplit(",", 1)[-1].strip() if "," in line else line.strip()
    return tvg_id, name, group


def parse_m3u(text: str, source: str, kind: str) -> list[Candidate]:
    lines = [x.strip() for x in text.splitlines()]
    result: list[Candidate] = []
    pending: Optional[str] = None

    for line in lines:
        if not line or line.startswith("#EXTM3U"):
            continue

        if line.startswith("#EXTINF:"):
            pending = line
            continue

        if pending and line.startswith(("http://", "https://")):
            # Esta versión usa HLS. Dejamos fuera MPD/DASH para no incorporar
            # fuentes con DRM/ClearKey en esta etapa.
            if ".mpd" in line.lower():
                pending = None
                continue

            tvg_id, name, group = parse_extinf(pending)
            result.append(Candidate(pending, line, source, tvg_id, name, group, kind))
            pending = None
            continue

        if line.startswith((
            "plugin://", "rtmp://", "rtmps://", "rtsp://",
            "udp://", "p2p://",
        )):
            pending = None

    return result


# ---------------------------------------------------------------------------
# FILTROS DE CANALES
# ---------------------------------------------------------------------------

def is_argentina(e: Candidate) -> bool:
    text = f"{e.tvg_id} {e.name} {e.group}".lower()
    name = canon(e.name)

    if name in EXCLUDE_ARG_NAMES or name in EXCLUDE_ARG_CLOSED:
        return False

    if any(x in text for x in FOREIGN_BAD):
        return False

    tvgid = e.tvg_id.lower()

    # Si tvg-id declara explícitamente un país, Argentina debe ser .ar.
    if "." in tvgid:
        return tvgid.endswith(".ar") or ".ar@" in tvgid

    ar_markers = (
        "argentina", "cordoba", "córdoba", "rosario", "santa fe",
        "mendoza", "tucuman", "tucumán", "salta", "jujuy", "misiones",
        "posadas", "neuquen", "neuquén", "san juan", "san luis",
        "catamarca", "chaco", "formosa", "la pampa", "la rioja",
        "rio negro", "río negro", "santa cruz", "tierra del fuego",
        "ushuaia", "mar del plata", "pinamar", "resistencia", "corrientes",
        "entre rios", "entre ríos", "trelew", "esquel", "bariloche",
        "la plata", "bahia blanca", "bahía blanca", "telefe",
        "el trece", "america tv", "américa tv", "tn", "c5n", "crónica",
        "a24", "ln+",
    )
    return any(x in text for x in ar_markers)


def is_latam_premium(e: Candidate) -> bool:
    text = f"{e.tvg_id} {e.name} {e.group}".lower()

    if any(x in text for x in FOREIGN_BAD):
        return False

    # Para fuentes comunitarias nuevas exigimos HLS. Esto evita incorporar
    # entradas no-HLS, URLs con formatos propietarios o listas de acceso
    # que no podamos inspeccionar como una playlist HLS.
    source_lower = e.source.lower()
    if "community" in source_lower and not e.url.lower().split("?", 1)[0].endswith(".m3u8"):
        return False

    # iptv-org exige una marca regional explícita.
    if source_lower.endswith("iptv-org") and not any(m in text for m in LATAM_MARKERS):
        return False

    return premium_canon(e.name) in PREMIUM


# ---------------------------------------------------------------------------
# DEDUPLICACIÓN
# ---------------------------------------------------------------------------

def display_key(e: Candidate) -> str:
    """
    Deduplica variantes que son el mismo canal.
    Mantiene separados canales locales distintos (por ejemplo Canal 2
    Mar del Plata vs Canal 2 Gualeguay).
    """
    n = canon(clean_name(e.name))

    aliases = {
        "5 tv": "5 tv",
        "disney channel latin america": "disney channel",
        "disney channel latin america panregional hd": "disney channel",
        "disney jr latin america": "disney junior",
        "disney jr latin america south hd": "disney junior",
        "el gourmet south": "el gourmet",
        "garage tv latin america": "garage tv",
        "cosmos tv sd": "cosmos tv",
        "bragado tv": "bragado tv",
        "aire de santa fe": "aire de santa fe",
        "catamarca tv": "catamarca tv",
        "cadena103 tv": "cadena103 tv",
        "5tv": "5tv",
        "el trece internacional latin america": "el trece internacional",
    }

    return aliases.get(n, n)


# ---------------------------------------------------------------------------
# HTTP + HLS
# ---------------------------------------------------------------------------

def parse_attrs(value: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, val in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', value):
        attrs[key] = val.strip('"')
    return attrs


def parse_hls_variants(text: str, base_url: str) -> list[dict]:
    """
    Extrae variantes de una master playlist #EXT-X-STREAM-INF.
    """
    variants = []
    lines = [x.strip() for x in text.splitlines()]

    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue

        attrs = parse_attrs(line.split(":", 1)[1])

        variant_url = ""
        for j in range(i + 1, min(i + 4, len(lines))):
            nxt = lines[j]
            if nxt and not nxt.startswith("#"):
                variant_url = urljoin(base_url, nxt)
                break

        if not variant_url:
            continue

        resolution_w = 0
        resolution_h = 0
        if "RESOLUTION" in attrs and "x" in attrs["RESOLUTION"]:
            try:
                resolution_w, resolution_h = map(int, attrs["RESOLUTION"].split("x", 1))
            except ValueError:
                pass

        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0"))
        except ValueError:
            bandwidth = 0

        try:
            avg_bandwidth = int(attrs.get("AVERAGE-BANDWIDTH", "0"))
        except ValueError:
            avg_bandwidth = 0

        variants.append({
            "url": variant_url,
            "w": resolution_w,
            "h": resolution_h,
            "bandwidth": bandwidth,
            "avg_bandwidth": avg_bandwidth,
        })

    return variants


def parse_hls_segments(text: str, base_url: str) -> list[str]:
    segments: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            segments.append(line)
        else:
            segments.append(urljoin(base_url, line))

    return segments


def request_text(session: requests.Session, url: str, timeout: float) -> tuple[requests.Response, str]:
    """
    Descarga solo una cantidad limitada de bytes de una respuesta.
    Esto evita quedar esperando indefinidamente si un servidor defectuoso
    mantiene una conexión HTTP abierta y sigue enviando datos.
    """
    r = session.get(
        url,
        stream=True,
        timeout=(min(timeout, 5), min(timeout, 5)),
        headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Cache-Control": "no-cache",
        },
        allow_redirects=True,
    )

    if not (200 <= r.status_code < 400):
        return r, ""

    chunks = []
    total = 0
    max_bytes = 512 * 1024

    try:
        for chunk in r.iter_content(chunk_size=16384):
            if not chunk:
                continue
            remaining = max_bytes - total
            if remaining <= 0:
                break
            data = chunk[:remaining]
            chunks.append(data)
            total += len(data)
            if total >= max_bytes:
                break
    except requests.RequestException:
        # La cabecera/estado puede ser válido pero el cuerpo no.
        # El contenido parcial todavía puede servir para analizar un m3u8.
        pass
    finally:
        r.close()

    raw = b"".join(chunks)
    encoding = r.encoding or "utf-8"
    try:
        text = raw.decode(encoding, errors="replace")
    except (LookupError, UnicodeError):
        text = raw.decode("utf-8", errors="replace")

    return r, text

def test_small_get(session: requests.Session, url: str, timeout: float) -> bool:
    """
    Comprueba un recurso con timeout de conexión/lectura y sin descargarlo.
    """
    try:
        with session.get(
            url,
            stream=True,
            timeout=(min(timeout, 5), min(timeout, 5)),
            headers={
                "User-Agent": UA,
                "Range": "bytes=0-4095",
            },
            allow_redirects=True,
        ) as r:
            return 200 <= r.status_code < 400
    except requests.RequestException:
        return False

def bitrate_mbps(value: int) -> str:
    if not value:
        return "N/D"
    return f"{value / 1_000_000:.2f} Mbps"


def probe_candidate(candidate: Candidate, timeout: float) -> Probe:
    start = time.monotonic()
    p = Probe(candidate=candidate)

    session = requests.Session()

    try:
        r, text = request_text(session, candidate.url, timeout)
        p.http_status = r.status_code
        p.final_url = r.url
        p.content_type = r.headers.get("Content-Type", "")

        if not (200 <= r.status_code < 400):
            p.error = f"HTTP {r.status_code}"
            return p

        # Señal HLS.
        is_hls = (
            "#EXTM3U" in text[:1000]
            or ".m3u8" in candidate.url.lower()
            or "application/vnd.apple.mpegurl" in p.content_type.lower()
            or "application/x-mpegurl" in p.content_type.lower()
        )

        if not is_hls:
            # La fuente respondió, pero no podemos medir calidad HLS.
            p.stream_type = "non_hls"
            p.online = True
            p.notes.append("Respuesta HTTP válida; calidad no publicada como HLS.")
            return p

        p.stream_type = "master" if "#EXT-X-STREAM-INF:" in text else "media"

        variants = parse_hls_variants(text, p.final_url)

        if variants:
            p.variants = len(variants)

            # Mejor variante primero: resolución y luego bitrate.
            variants.sort(
                key=lambda x: (
                    x["h"],
                    x["w"],
                    x["avg_bandwidth"] or x["bandwidth"],
                ),
                reverse=True,
            )

            # Intentamos comprobar primero la mejor, luego hasta 3 adicionales.
            checked = 0
            for v in variants[:4]:
                try:
                    vr, vtext = request_text(session, v["url"], timeout)
                except requests.RequestException:
                    checked += 1
                    continue

                if not (200 <= vr.status_code < 400):
                    checked += 1
                    continue

                segments = parse_hls_segments(vtext, vr.url)

                if not segments:
                    checked += 1
                    continue

                if test_small_get(session, segments[0], timeout):
                    p.online = True
                    p.working_variant = v["url"]
                    p.working_segment = segments[0]
                    p.resolution_w = v["w"]
                    p.resolution_h = v["h"]
                    p.bitrate = v["avg_bandwidth"] or v["bandwidth"]
                    if not p.resolution_h:
                        p.notes.append("La variante funciona pero no publica RESOLUTION.")
                    if not p.bitrate:
                        p.notes.append("La variante funciona pero no publica BANDWIDTH.")
                    break

                checked += 1

            if not p.online:
                p.error = "Master playlist responde, pero no se pudo comprobar una variante/segmento."
                return p

        else:
            # Media playlist directa.
            segments = parse_hls_segments(text, p.final_url)

            if not segments:
                p.error = "m3u8 válida pero sin segmentos detectables."
                return p

            # Probamos hasta los dos primeros segmentos; con uno alcanza para una
            # comprobación práctica y evita descargar vídeo innecesariamente.
            for seg in segments[:2]:
                if test_small_get(session, seg, timeout):
                    p.online = True
                    p.working_segment = seg
                    break

            if not p.online:
                p.error = "La playlist responde, pero no se pudo comprobar ningún segmento."

            p.notes.append("Playlist HLS sin metadatos de resolución/bitrate.")

    except requests.Timeout:
        p.error = "timeout"
    except requests.RequestException as exc:
        p.error = type(exc).__name__
    except Exception as exc:
        p.error = f"{type(exc).__name__}: {exc}"
    finally:
        p.elapsed_ms = int((time.monotonic() - start) * 1000)
        session.close()

    return p


# ---------------------------------------------------------------------------
# ELECCIÓN DEL MEJOR STREAM
# ---------------------------------------------------------------------------

def choose_best(results: list[Probe]) -> Optional[Probe]:
    online = [x for x in results if x.online]
    if not online:
        return None

    online.sort(
        key=lambda p: (
            p.score,
            p.resolution_h,
            p.resolution_w,
            p.bitrate,
            -p.elapsed_ms,
        ),
        reverse=True,
    )
    return online[0]


def pretty_region(name: str) -> str:
    replacements = {
        " Latin America": "",
        " Latin America South": "",
        " Panregional": "",
        " Andes": "",
        " Mexico": "",
    }
    out = name
    for a, b in replacements.items():
        out = out.replace(a, b)
    return clean_name(out).strip()


def make_extinf(entry: Candidate, group: str, output_name: str) -> str:
    line = entry.extinf

    if re.search(r'group-title="[^"]*"', line, re.I):
        line = re.sub(
            r'group-title="[^"]*"',
            f'group-title="{group}"',
            line,
            flags=re.I,
        )
    else:
        line = line.replace(
            "#EXTINF:-1",
            f'#EXTINF:-1 group-title="{group}"',
            1,
        )

    return line.rsplit(",", 1)[0] + "," + output_name


# ---------------------------------------------------------------------------
# FUENTES
# ---------------------------------------------------------------------------

def download_source(name: str, url: str, kind: str) -> list[Candidate]:
    print(f"[+] {name}")
    try:
        r = requests.get(url, timeout=SOURCE_TIMEOUT, headers={"User-Agent": UA})
        r.raise_for_status()
        entries = parse_m3u(r.text, name, kind)

        if kind == "argentina":
            entries = [e for e in entries if is_argentina(e)]
            print(f"    {len(entries)} candidatos argentinos")
        else:
            entries = [e for e in entries if is_latam_premium(e)]
            print(f"    {len(entries)} candidatos premium LATAM")

        return entries
    except Exception as exc:
        print(f"    [!] Error: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# INFORME
# ---------------------------------------------------------------------------

def report_channel(name: str, results: list[Probe], selected: Optional[Probe]) -> str:
    lines = [f"{name}"]

    for p in sorted(
        results,
        key=lambda x: (
            0 if (selected and x is selected) else 1,
            0 if x.online else 1,
            -x.score,
            x.candidate.source,
        ),
    ):
        if p.online:
            state = "✅ ONLINE"
            quality = p.resolution_label
            br = bitrate_mbps(p.bitrate)
            selected_mark = "  <-- SELECCIONADO" if selected is p else ""
            lines.append(
                f"  {state}{selected_mark}"
                f" | {quality}"
                f" | bitrate {br}"
                f" | score {p.score}"
                f" | {p.candidate.source}"
            )
            lines.append(f"      URL: {p.candidate.url}")
            if p.notes:
                lines.append(f"      Nota: {'; '.join(p.notes)}")
        else:
            err = p.error or "no comprobado"
            lines.append(
                f"  ❌ OFFLINE | {err} | {p.candidate.source}"
            )
            lines.append(f"      URL: {p.candidate.url}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="masterlist_argentina_latino_v8.m3u",
    )
    parser.add_argument(
        "--report",
        default="masterlist_argentina_latino_v8_report.txt",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=24,
        help="streams comprobados simultáneamente (default: 20)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8,
        help="timeout por petición de stream en segundos (default: 8)",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="no comprobar streams; útil solo para depurar fuentes",
    )
    args = parser.parse_args()

    if args.workers < 1 or args.workers > 50:
        parser.error("--workers debe estar entre 1 y 50")

    # Descargamos fuentes en paralelo para acelerar el arranque.
    source_jobs = [(n, u, "argentina") for n, u in ARG_SOURCES]
    source_jobs.extend((n, u, kind) for n, u, kind in LATAM_SOURCES)

    all_candidates: list[Candidate] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(source_jobs))) as ex:
        futures = [
            ex.submit(download_source, name, url, kind)
            for name, url, kind in source_jobs
        ]
        for fut in futures:
            all_candidates.extend(fut.result())

    # Deduplicación de URLs idénticas y agrupación por canal.
    unique_by_url: dict[str, Candidate] = {}
    for c in all_candidates:
        unique_by_url.setdefault(c.url, c)

    candidates = list(unique_by_url.values())

    grouped: dict[tuple[str, str], list[Candidate]] = {}
    for c in candidates:
        grouped.setdefault((c.kind, display_key(c)), []).append(c)

    print()
    print(f"[+] Candidatos únicos a comprobar: {len(candidates)}")
    print(f"[+] Canales diferentes: {len(grouped)}")
    print()

    probes: list[Probe] = []

    if args.no_check:
        for c in candidates:
            probes.append(Probe(candidate=c, online=True, notes=["NO-CHECK"]))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            future_map = {
                ex.submit(probe_candidate, c, args.timeout): c
                for c in candidates
            }

            done = 0
            total = len(future_map)

            for fut in concurrent.futures.as_completed(future_map):
                p = fut.result()
                probes.append(p)
                done += 1

                status = "ONLINE" if p.online else "FAIL"
                q = p.resolution_label
                br = bitrate_mbps(p.bitrate)
                print(
                    f"[{done:>3}/{total}] {status:<6} "
                    f"{q:<5} {br:<9} {p.candidate.name}"
                )

    probes_by_key: dict[tuple[str, str], list[Probe]] = {}
    for p in probes:
        probes_by_key.setdefault(
            (p.candidate.kind, display_key(p.candidate)),
            [],
        ).append(p)

    selected_arg: list[Probe] = []
    selected_latam: list[Probe] = []

    report_parts: list[str] = []
    online_channels = 0
    offline_channels = 0

    for key in sorted(probes_by_key):
        channel_results = probes_by_key[key]
        selected = choose_best(channel_results)

        if selected:
            online_channels += 1
            if key[0] == "argentina":
                selected_arg.append(selected)
            else:
                selected_latam.append(selected)
        else:
            offline_channels += 1

        report_parts.append(
            report_channel(
                clean_name(
                    channel_results[0].candidate.name
                ),
                channel_results,
                selected,
            )
        )

    # Orden alfabético.
    selected_arg.sort(key=lambda p: p.candidate.clean_name().lower())
    selected_latam.sort(key=lambda p: p.candidate.clean_name().lower())

    output = Path(args.output)
    with output.open("w", encoding="utf-8", newline="\r\n") as f:
        f.write(
            '#EXTM3U x-tvg-url="https://iptv-org.github.io/epg/guides/tv/argentina.epg.xml"\r\n'
        )
        f.write("# Masterlist Argentina + Premium Latinoamérica v8\r\n")
        f.write("# Selección automática: ONLINE primero, luego calidad técnica.\r\n")
        f.write("# Una sola entrada por canal.\r\n\r\n")

        f.write("# ================== ARGENTINA ==================\r\n")
        for p in selected_arg:
            name = clean_name(p.candidate.name)
            # Quita indicadores regionales/resolución del nombre visible.
            line = make_extinf(p.candidate, "Argentina", name)
            f.write(line + "\r\n" + p.candidate.url + "\r\n")

        f.write("\r\n# ============ PREMIUM LATINOAMÉRICA ============\r\n")
        for p in selected_latam:
            name = pretty_region(clean_name(p.candidate.name))
            line = make_extinf(p.candidate, "Premium Latinoamérica", name)
            f.write(line + "\r\n" + p.candidate.url + "\r\n")

    report = Path(args.report)
    with report.open("w", encoding="utf-8") as f: