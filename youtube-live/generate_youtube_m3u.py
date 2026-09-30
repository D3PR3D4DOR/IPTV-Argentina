import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "youtubeLink.txt"
OUTPUT_FILE = BASE_DIR / "youtube.m3u8"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/156.0.0.0 Safari/537.36"
)

MAX_ATTEMPTS = 15
RETRY_DELAY = 2

HLS_PATTERN = re.compile(
    r"https://manifest\.googlevideo\.com/[^\"'<>\s\\]+?\.m3u8"
)


def fetch_url(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.youtube.com/",
        },
    )

    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_hls(html: str) -> str | None:
    # YouTube puede escapar barras y ampersands dentro del JSON/HTML.
    html = html.replace("\\/", "/")
    html = html.replace("\\u0026", "&")

    match = HLS_PATTERN.search(html)

    if not match:
        return None

    return match.group(0)


def validate_hls(url: str) -> bool:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.youtube.com/",
        },
    )

    with urlopen(request, timeout=20) as response:
        if response.status != 200:
            return False

        content_type = response.headers.get("Content-Type", "").lower()
        sample = response.read(512)

        return (
            b"#EXTM3U" in sample
            or "mpegurl" in content_type
            or "vnd.apple.mpegurl" in content_type
        )


def grab_hls(channel_name: str, youtube_url: str) -> str:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(
            f"[{channel_name}] Intento "
            f"{attempt}/{MAX_ATTEMPTS}..."
        )

        try:
            html = fetch_url(youtube_url)
            hls = extract_hls(html)

            if not hls:
                print(f"[{channel_name}] No se encontró HLS.")
            else:
                print(f"[{channel_name}] HLS encontrada.")

                try:
                    if validate_hls(hls):
                        print(f"[{channel_name}] HLS validada correctamente.")
                        return hls

                    print(f"[{channel_name}] HLS encontrada pero no valida.")

                except Exception as exc:
                    print(
                        f"[{channel_name}] Error validando HLS: {exc}"
                    )

        except Exception as exc:
            print(f"[{channel_name}] Error: {exc}")

        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_DELAY)

    raise RuntimeError(
        f"No se pudo obtener una HLS válida para {channel_name} "
        f"después de {MAX_ATTEMPTS} intentos."
    )


def read_channels():
    channels = []

    with INPUT_FILE.open("r", encoding="utf-8-sig") as file:
        lines = [line.strip() for line in file]

    current = None

    for line in lines:
        if not line or line.startswith("#"):
            continue

        if line.startswith("https://") or line.startswith("http://"):
            if current is None:
                raise RuntimeError(
                    f"URL sin datos de canal: {line}"
                )

            current["url"] = line
            channels.append(current)
            current = None

        else:
            parts = [part.strip() for part in line.split("||")]

            if len(parts) != 3:
                raise RuntimeError(
                    f"Línea de canal inválida: {line}"
                )

            current = {
                "name": parts[0],
                "id": parts[1],
                "group": parts[2],
                "url": None,
            }

    return channels


def main():
    channels = read_channels()

    if not channels:
        raise RuntimeError("No se encontraron canales.")

    print(f"Canales encontrados: {len(channels)}")

    results = []

    for channel in channels:
        hls = grab_hls(
            channel["name"],
            channel["url"],
        )

        results.append(
            (
                channel["name"],
                channel["id"],
                channel["group"],
                hls,
            )
        )

    output = ["#EXTM3U", ""]

    for name, channel_id, group, hls in results:
        output.append(
            f'#EXTINF:-1 tvg-id="{channel_id}" '
            f'tvg-name="{name}" '
            f'group-title="{group}", {name}'
        )
        output.append(hls)
        output.append("")

    # Solo escribimos el archivo si TODOS los canales fueron obtenidos.
    OUTPUT_FILE.write_text(
        "\n".join(output),
        encoding="utf-8",
        newline="\n",
    )

    print()
    print(f"Playlist generada: {OUTPUT_FILE}")
    print(f"Canales actualizados: {len(results)}")


if __name__ == "__main__":
    main()