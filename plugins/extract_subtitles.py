import json
import sys
import os
import subprocess

try:
    from stashapi.stashapp import StashInterface
    import stashapi.log as stash_log
except ModuleNotFoundError:
    print("ERROR: stashapp-tools is not installed.", file=sys.stderr)
    print("Install it with: pip install stashapp-tools", file=sys.stderr)
    sys.exit(1)

PLUGIN_ID = "ass-subtitles"


def get_input():
    raw = sys.stdin.read()
    return json.loads(raw)


def log(level, msg):
    out = json.dumps({"output": {"log": {"level": level, "message": msg}}})
    print(out, flush=True)


def progress(p):
    out = json.dumps({"output": {"progress": p}})
    print(out, flush=True)


def get_settings(stash):
    try:
        config = stash.call_GQL("query { configuration { plugins } }")
        all_plugins = config.get("configuration", {}).get("plugins", {})
        if isinstance(all_plugins, dict):
            return all_plugins.get(PLUGIN_ID, {})
    except Exception:
        pass
    return {}


def get_ffmpeg_path(settings):
    path = settings.get("ffmpegPath", "").strip()
    return path if path else "ffmpeg"


def get_ffprobe_path(settings):
    ffmpeg = get_ffmpeg_path(settings)
    if ffmpeg == "ffmpeg":
        return "ffprobe"
    directory = os.path.dirname(ffmpeg)
    return os.path.join(directory, "ffprobe") if directory else "ffprobe"


def probe_subtitle_streams(video_path, ffprobe_path="ffprobe"):
    cmd = [
        ffprobe_path, "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "s", video_path
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
    cmd = [
        ffmpeg_path, "-y", "-v", "quiet", "-i", video_path,
        "-map", f"0:{stream_index}", "-c:s", "ass", output_path
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
    base = os.path.splitext(video_path)[0]
    lang = stream.get("language", "und")
    idx = stream.get("index", 0)
    suffix = f".{lang}.{idx}.ass"
    if subs_dir:
        os.makedirs(subs_dir, exist_ok=True)
        return os.path.join(subs_dir, os.path.basename(base) + suffix)
    return base + suffix


def process_scene(stash, scene_id, settings):
    result = stash.call_GQL("""
        query FindScene($id: ID!) {
            findScene(id: $id) { id, files { path } }
        }
    """, {"id": str(scene_id)})
    scene = result.get("findScene")
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
                extracted += 1
                continue
            if extract_subtitle(video_path, stream["index"], out, ffmpeg):
                log("info", f"Extracted subtitle: {out}")
                extracted += 1
    return extracted


def process_all_scenes(stash, settings):
    count_result = stash.call_GQL("query { findScenes(filter: { per_page: 0 }) { count } }")
    total = count_result.get("findScenes", {}).get("count", 0)
    if total == 0:
        log("info", "No scenes found.")
        return

    log("info", f"Processing {total} scenes for embedded ASS/SSA subtitles...")

    ffmpeg = get_ffmpeg_path(settings)
    ffprobe = get_ffprobe_path(settings)
    subs_dir = settings.get("subtitlesDir", "").strip() or None
    page = 1
    per_page = 100
    processed = 0
    total_scenes = 0
    total_extracted = 0

    while True:
        result = stash.call_GQL("""
            query FindScenes($page: Int!, $per_page: Int!) {
                findScenes(filter: { page: $page, per_page: $per_page }) {
                    scenes { id, files { path } }
                }
            }
        """, {"page": page, "per_page": per_page})
        scenes = result.get("findScenes", {}).get("scenes", [])
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


def get_subtitle_text_for_scene(stash, scene_id, settings):
    result = stash.call_GQL("""
        query FindScene($id: ID!) {
            findScene(id: $id) { id, files { path } }
        }
    """, {"id": str(scene_id)})
    scene = result.get("findScene")
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
            cmd = [
                ffmpeg, "-v", "quiet", "-i", video_path,
                "-map", f"0:{stream['index']}", "-c:s", "ass", "-f", "ass", "pipe:1"
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

    return {"tracks": tracks} if tracks else None


def main():
    plugin_input = get_input()
    stash = StashInterface(plugin_input["server_connection"])

    args = plugin_input.get("args", {})
    mode = args.get("mode", "")

    try:
        stash_settings = get_settings(stash)
    except Exception:
        stash_settings = {}

    for k, v in args.items():
        if v:
            stash_settings[k] = v

    if mode == "get_subtitles":
        scene_id = args.get("scene_id")
        if not scene_id:
            print(json.dumps({"output": None}), flush=True)
            return
        result = get_subtitle_text_for_scene(stash, scene_id, stash_settings)
        output = json.dumps(result) if result else ""
        print(json.dumps({"output": output}), flush=True)
        return

    task_name = args.get("task", "")
    if task_name == "Extract Subtitles for Scene":
        scene_id = args.get("scene_id")
        if not scene_id:
            log("error", "No scene_id provided.")
            return
        count = process_scene(stash, scene_id, stash_settings)
        log("info", f"Extracted {count} subtitle track(s) for scene {scene_id}.")
    else:
        process_all_scenes(stash, stash_settings)


if __name__ == "__main__":
    main()
