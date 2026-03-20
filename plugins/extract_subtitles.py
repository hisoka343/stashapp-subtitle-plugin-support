import json
import sys
import os
import subprocess
import re
import urllib.request

PLUGIN_ID = "ass-subtitles"

# --- Stash plugin helpers ---

def get_input():
    """Read JSON input from stdin (Stash passes plugin args this way)."""
    raw = sys.stdin.read()
    return json.loads(raw)


def graphql_request(query, variables=None):
    """Send a GraphQL request to the Stash server."""
    server_url = os.environ.get("STASH_URL", "http://localhost:9999")
    api_key = os.environ.get("STASH_API_KEY", "")
    url = f"{server_url}/graphql"

    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["ApiKey"] = api_key

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def log(level, msg):
    """Emit a log line that Stash picks up."""
    out = json.dumps({"output": {"log": {"level": level, "message": msg}}})
    print(out, flush=True)


def progress(p):
    """Report progress (0.0 – 1.0)."""
    out = json.dumps({"output": {"progress": p}})
    print(out, flush=True)


# --- Settings ---

def get_settings():
    """Retrieve plugin settings from Stash."""
    query = """
    query Configuration {
        configuration {
            plugins
        }
    }
    """
    try:
        result = graphql_request(query)
        all_plugins = result.get("data", {}).get("configuration", {}).get("plugins", {})
        if isinstance(all_plugins, dict):
            return all_plugins.get(PLUGIN_ID, {})
    except Exception:
        pass
    return {}


def get_ffmpeg_path(settings):
    path = settings.get("ffmpegPath", "").strip()
    return path if path else "ffmpeg"


def get_ffprobe_path(settings):
    """Derive ffprobe path from ffmpeg path."""
    ffmpeg = get_ffmpeg_path(settings)
    if ffmpeg == "ffmpeg":
        return "ffprobe"
    # If user set a custom ffmpeg path, try to find ffprobe next to it
    directory = os.path.dirname(ffmpeg)
    if directory:
        return os.path.join(directory, "ffprobe")
    return "ffprobe"


# --- Subtitle extraction ---

def probe_subtitle_streams(video_path, ffprobe_path="ffprobe"):
    """Use ffprobe to find embedded subtitle streams (ASS/SSA)."""
    cmd = [
        ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-select_streams", "s",
        video_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        streams = []
        for s in data.get("streams", []):
            codec = s.get("codec_name", "").lower()
            if codec in ("ass", "ssa"):
                streams.append({
                    "index": s.get("index", 0),
                    "codec": codec,
                    "language": s.get("tags", {}).get("language", "und"),
                    "title": s.get("tags", {}).get("title", ""),
                })
        return streams
    except Exception as e:
        log("warning", f"ffprobe failed for {video_path}: {e}")
        return []


def extract_subtitle(video_path, stream_index, output_path, ffmpeg_path="ffmpeg"):
    """Extract a single subtitle stream to an .ass file."""
    cmd = [
        ffmpeg_path,
        "-y",                       # overwrite
        "-v", "quiet",
        "-i", video_path,
        "-map", f"0:{stream_index}",
        "-c:s", "ass",              # always output ASS format
        output_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log("warning", f"ffmpeg extract failed (stream {stream_index}): {result.stderr[:200]}")
            return False
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception as e:
        log("warning", f"ffmpeg exception: {e}")
        return False


def output_path_for_sub(video_path, stream, subs_dir=None):
    """Build the output .ass file path for a given stream."""
    base = os.path.splitext(video_path)[0]
    lang = stream.get("language", "und")
    idx = stream.get("index", 0)
    suffix = f".{lang}.{idx}.ass"

    if subs_dir:
        os.makedirs(subs_dir, exist_ok=True)
        filename = os.path.basename(base) + suffix
        return os.path.join(subs_dir, filename)
    return base + suffix


def process_scene(scene_id, settings):
    """Extract subtitles for a single scene by its Stash ID."""
    query = """
    query FindScene($id: ID!) {
        findScene(id: $id) {
            id
            files {
                path
            }
        }
    }
    """
    result = graphql_request(query, {"id": str(scene_id)})
    scene = result.get("data", {}).get("findScene")
    if not scene:
        log("warning", f"Scene {scene_id} not found")
        return 0

    ffmpeg = get_ffmpeg_path(settings)
    ffprobe = get_ffprobe_path(settings)
    subs_dir = settings.get("subtitlesDir", "").strip() or None
    extracted = 0

    for f in scene.get("files", []):
        video_path = f.get("path", "")
        if not video_path or not os.path.exists(video_path):
            continue

        streams = probe_subtitle_streams(video_path, ffprobe)
        if not streams:
            continue

        for stream in streams:
            out = output_path_for_sub(video_path, stream, subs_dir)
            if os.path.exists(out):
                log("debug", f"Subtitle already exists: {out}")
                extracted += 1
                continue
            if extract_subtitle(video_path, stream["index"], out, ffmpeg):
                log("info", f"Extracted subtitle: {out}")
                extracted += 1

    return extracted


def process_all_scenes(settings):
    """Extract subtitles for every scene in the library."""
    page = 1
    per_page = 100
    total_extracted = 0
    total_scenes = 0

    # First get count
    count_query = """
    query { findScenes(filter: { per_page: 0 }) { count } }
    """
    count_result = graphql_request(count_query)
    total = count_result.get("data", {}).get("findScenes", {}).get("count", 0)
    if total == 0:
        log("info", "No scenes found.")
        return

    log("info", f"Processing {total} scenes for embedded ASS/SSA subtitles...")

    scenes_query = """
    query FindScenes($page: Int!, $per_page: Int!) {
        findScenes(filter: { page: $page, per_page: $per_page }) {
            scenes {
                id
                files {
                    path
                }
            }
        }
    }
    """

    ffmpeg = get_ffmpeg_path(settings)
    ffprobe = get_ffprobe_path(settings)
    subs_dir = settings.get("subtitlesDir", "").strip() or None
    processed = 0

    while True:
        result = graphql_request(scenes_query, {"page": page, "per_page": per_page})
        scenes = result.get("data", {}).get("findScenes", {}).get("scenes", [])
        if not scenes:
            break

        for scene in scenes:
            processed += 1
            progress(processed / total)

            for f in scene.get("files", []):
                video_path = f.get("path", "")
                if not video_path or not os.path.exists(video_path):
                    continue

                streams = probe_subtitle_streams(video_path, ffprobe)
                if not streams:
                    continue

                total_scenes += 1
                for stream in streams:
                    out = output_path_for_sub(video_path, stream, subs_dir)
                    if os.path.exists(out):
                        total_extracted += 1
                        continue
                    if extract_subtitle(video_path, stream["index"], out, ffmpeg):
                        log("info", f"Extracted: {out}")
                        total_extracted += 1

        page += 1

    log("info", f"Done. {total_scenes} scenes had subtitles, {total_extracted} tracks extracted.")


# --- Entry point ---

def get_subtitle_text_for_scene(scene_id, settings):
    """Extract ASS subtitle text for a scene and return it (no file saving)."""
    query = """
    query FindScene($id: ID!) {
        findScene(id: $id) {
            id
            files { path }
        }
    }
    """
    result = graphql_request(query, {"id": str(scene_id)})
    scene = result.get("data", {}).get("findScene")
    if not scene:
        return None

    ffmpeg = get_ffmpeg_path(settings)
    ffprobe = get_ffprobe_path(settings)
    subs_dir = settings.get("subtitlesDir", "").strip() or None

    tracks = []

    for f in scene.get("files", []):
        video_path = f.get("path", "")
        if not video_path or not os.path.exists(video_path):
            continue

        streams = probe_subtitle_streams(video_path, ffprobe)
        if not streams:
            continue

        for stream in streams:
            # First check if we have a pre-extracted file
            out_path = output_path_for_sub(video_path, stream, subs_dir)
            if os.path.exists(out_path):
                try:
                    with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    if text.strip():
                        lang = stream.get("language", "und")
                        title = stream.get("title", "")
                        label = title if title else f"{lang} (Track {stream['index']})"
                        tracks.append({"label": label, "text": text})
                        continue
                except Exception:
                    pass

            # Extract on the fly to stdout
            cmd = [
                ffmpeg,
                "-v", "quiet",
                "-i", video_path,
                "-map", f"0:{stream['index']}",
                "-c:s", "ass",
                "-f", "ass",
                "pipe:1"
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if proc.returncode == 0 and proc.stdout.strip():
                    lang = stream.get("language", "und")
                    title = stream.get("title", "")
                    label = title if title else f"{lang} (Track {stream['index']})"
                    tracks.append({"label": label, "text": proc.stdout})
            except Exception as e:
                log("warning", f"On-the-fly extraction failed: {e}")

    if not tracks:
        return None

    return {"tracks": tracks}


def main():
    plugin_input = get_input()
    args = plugin_input.get("args", {})
    mode = args.get("mode", "")

    # Try to load settings from Stash; fall back to args
    try:
        stash_settings = get_settings()
    except Exception:
        stash_settings = {}

    # Merge any args into settings
    for k, v in args.items():
        if v:
            stash_settings[k] = v

    # --- Operation mode: return subtitle text for JS frontend ---
    if mode == "get_subtitles":
        scene_id = args.get("scene_id")
        if not scene_id:
            print(json.dumps({"output": None}), flush=True)
            return
        result = get_subtitle_text_for_scene(scene_id, stash_settings)
        # Return the subtitle data as the operation output
        output = json.dumps(result) if result else ""
        print(json.dumps({"output": output}), flush=True)
        return

    # --- Task mode ---
    task_name = args.get("task", "")

    if task_name == "Extract Subtitles for Scene":
        scene_id = args.get("scene_id")
        if not scene_id:
            log("error", "No scene_id provided.")
            return
        count = process_scene(scene_id, stash_settings)
        log("info", f"Extracted {count} subtitle track(s) for scene {scene_id}.")
    else:
        # Default: extract all
        process_all_scenes(stash_settings)


if __name__ == "__main__":
    main()
