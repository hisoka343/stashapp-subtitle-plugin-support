# ASS Subtitles Plugin for Stashapp

Pixel-perfect rendering of embedded ASS/SSA subtitles during video playback in Stash's web player, powered by JASSUB (WebAssembly libass).

## How it works

1. **Backend (Python)** — Uses `ffprobe` to detect embedded ASS/SSA subtitle streams in your video files, then `ffmpeg` to extract the subtitle data on-the-fly.
2. **Frontend (JavaScript + WASM)** — Uses [JASSUB](https://github.com/ThaUnknown/jassub), a WebAssembly port of the industry-standard `libass` subtitle renderer (the same engine used by mpv and VLC). Subtitles are rendered onto a `<canvas>` overlay that tracks the video player position, including fullscreen mode.

## Requirements

- **Stash** ≥ 0.25 (for `runPluginOperation` support)
- **ffmpeg** and **ffprobe** installed and available in `$PATH` (or configured in plugin settings)
- **Python 3** (used by Stash for plugin execution)

## Installation

1. Copy the `ass-subtitles` folder into your Stash plugins directory:
   ```
   ~/.stash/plugins/ass-subtitles/
   ```
   On Windows: `%USERPROFILE%\.stash\plugins\ass-subtitles\`

   The folder should contain:
   ```
   ass-subtitles/
   ├── ass-subtitles.yml
   ├── ass-subtitles.js
   ├── ass-subtitles.css
   ├── extract_subtitles.py
   ├── README.md
   └── jassub/
       ├── jassub.umd.js
       ├── jassub-worker.js
       ├── jassub-worker.wasm
       └── default.woff2
   ```

2. Go to **Settings → Plugins → Reload Plugins**.

3. The plugin should appear in the plugins list as "ASS Subtitles". Make sure it is enabled.

## Usage

### Automatic (on-the-fly)
Navigate to any scene that has embedded ASS/SSA subtitles. The plugin will automatically:
- Detect subtitle streams via `ffprobe`
- Extract the ASS text on-the-fly via `ffmpeg`
- Display a **CC** button in the video player toolbar
- Render subtitles pixel-perfectly on a canvas overlay

### Pre-extraction (recommended for large libraries)
For faster loading, pre-extract all subtitles:

1. Go to **Settings → Tasks** (or the plugin's task menu)
2. Run **"Extract All Subtitles"**
3. This creates `.ass` files alongside each video file (or in the configured subtitles directory)
4. Pre-extracted files load instantly without re-running ffmpeg

### Controls
- **Click the CC button** — Toggle subtitles on/off
- **Fullscreen** — Subtitles automatically follow the video into and out of fullscreen mode

## Settings

| Setting | Description |
|---------|-------------|
| **Subtitles directory** | Optional folder for extracted `.ass` files. If blank, files are saved next to the video. |
| **Show subtitles by default** | Auto-enable subtitles when available. |
| **Font size override (px)** | Override the subtitle font size. `0` = use the ASS file's sizes. |
| **FFmpeg path** | Custom path to `ffmpeg`. Leave blank for system default. |

## Supported ASS Features

Since this plugin uses the full libass engine via WebAssembly, it supports virtually all ASS/SSA features:

- ✅ All timed dialogue with centisecond precision
- ✅ Multiple named styles (font, size, color, bold/italic/underline)
- ✅ Primary, secondary, outline, and shadow colors
- ✅ Text outlines and drop shadows
- ✅ All 9 alignment positions (numpad-style)
- ✅ Per-style and per-line margins
- ✅ Override tags: `\b`, `\i`, `\u`, `\s`, `\c`, `\1c`–`\4c`, `\fs`, `\fn`, `\an`, `\a`, `\pos`, `\move`, `\fad`, `\fade`, `\t`, `\r`, `\fe`, `\q`, etc.
- ✅ Line breaks (`\N`, `\n`)
- ✅ Border styles (outline, opaque box)
- ✅ Resolution scaling (`PlayResX` / `PlayResY`)
- ✅ Karaoke effects (`\k`, `\K`, `\kf`, `\ko`)
- ✅ Rotation and shearing (`\frx`, `\fry`, `\frz`, `\fax`, `\fay`)
- ✅ Clip/mask regions (`\clip`, `\iclip`)
- ✅ Drawing commands (`\p`)
- ✅ Animated transforms (`\t`)
- ✅ Complex fansub typesetting

### Limitations
- Embedded fonts in the video file are not extracted; the plugin uses a Liberation Sans fallback font. Subtitles that rely on specific fonts may look slightly different.
- Drawing-heavy karaoke or typesetting effects may have minor rendering differences compared to a desktop player.

## Architecture

The plugin uses an unconventional architecture to work around Stash's React-based UI:

- **Canvas on `document.body`** — JASSUB's rendering canvas lives outside React's DOM tree (`document.body`), so React's re-renders cannot destroy it.
- **`requestAnimationFrame` positioning** — Every frame, the overlay's position is synced to the video element's bounding rect, excluding letterbox bars. This handles resize, scroll, and layout changes.
- **Fullscreen reparenting** — When the browser enters fullscreen, the overlay is moved into the fullscreen element (which creates a top-level stacking context), then moved back to `document.body` on exit.
- **Blob URL worker** — Stash's CSP only allows `worker-src blob:`, so the JASSUB web worker script is fetched, wrapped in a Blob, and loaded via `URL.createObjectURL`.

## Troubleshooting

**No CC button appears:**
- Check that the scene file has embedded subtitle streams: `ffprobe -v quiet -print_format json -show_streams -select_streams s your_file.mkv`
- Verify ffmpeg/ffprobe are accessible: `which ffmpeg` / `which ffprobe`
- Check the Stash log (Settings → Logs) for errors
- Open browser DevTools console (F12) and look for `[ASS-Sub]` messages

**Subtitles disappear or flicker:**
- Open DevTools console and check for `[ASS-Sub]` messages — the plugin logs all lifecycle events
- Try a hard refresh: `Shift+F5`
- Make sure no other plugins are interfering with the video player DOM

**Subtitles are misaligned:**
- The plugin calculates the video content area (excluding letterbox bars) to position subtitles. If the video has unusual dimensions this calculation may be slightly off.
- Check if the ASS file has unusual `PlayResX`/`PlayResY` values

**Subtitles are slow to appear on first load:**
- On-the-fly extraction runs ffprobe + ffmpeg each time. Run the **"Extract All Subtitles"** task to pre-extract and cache them.

**CSP or Worker errors in console:**
- Make sure the `jassub/` folder is present with all four files
- The plugin YAML includes CSP overrides, but if Stash has been customized this may conflict

## License

The default license is set to [AGPL-3.0](/LICENCE). Before publishing any plugins you can change it.
