(function () {
  "use strict";

  const PLUGIN_ID = "ass-subtitles";
  const JASSUB_BASE = "/plugin/ass-subtitles/assets/jassub";

  let currentSceneId = null;
  let jassubInstance = null;
  let videoElement = null;
  let isEnabled = true;
  let btnEl = null;
  let jassubLoaded = false;
  let workerBlobUrl = null;
  let cachedAssText = null;

  // Our own overlay elements — lives on document.body, outside React
  let overlayDiv = null;
  let overlayCanvas = null;
  let overlayParent = null;
  let positionRAF = null;

  function log(msg) { console.log(`[ASS-Sub] ${msg}`); }
  function warn(msg) { console.warn(`[ASS-Sub] ${msg}`); }

  // =========================================================================
  //  GRAPHQL
  // =========================================================================

  async function callGQL(query, variables) {
    const resp = await fetch("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables }),
    });
    return resp.json();
  }

  async function runPluginOp(args) {
    const q = `mutation RunPluginOperation($plugin_id: ID!, $args: Map!) {
      runPluginOperation(plugin_id: $plugin_id, args: $args)
    }`;
    const r = await callGQL(q, { plugin_id: PLUGIN_ID, args });
    if (r.errors) { warn("GQL errors: " + JSON.stringify(r.errors)); return null; }
    return r?.data?.runPluginOperation ?? null;
  }

  async function fetchSubtitleText(sceneId) {
    try {
      const raw = await runPluginOp({ mode: "get_subtitles", scene_id: String(sceneId) });
      if (!raw) return null;
      if (typeof raw === "string") {
        try { return JSON.parse(raw); } catch { return { tracks: [{ label: "Track 1", text: raw }] }; }
      }
      return raw;
    } catch (e) { warn("fetchSubtitleText failed: " + e); return null; }
  }

  // =========================================================================
  //  JASSUB LOADER
  // =========================================================================

  async function loadJASSUB() {
    if (jassubLoaded || window.JASSUB) { jassubLoaded = true; return true; }
    return new Promise((resolve) => {
      const script = document.createElement("script");
      script.src = `${JASSUB_BASE}/jassub.umd.js`;
      script.onload = () => { jassubLoaded = true; log("JASSUB library loaded."); resolve(true); };
      script.onerror = (e) => { warn("Failed to load JASSUB: " + e); resolve(false); };
      document.head.appendChild(script);
    });
  }

  async function getWorkerBlobUrl() {
    if (workerBlobUrl) return workerBlobUrl;
    const resp = await fetch(`${JASSUB_BASE}/jassub-worker.js`);
    if (!resp.ok) throw new Error(`Failed to fetch worker: ${resp.status}`);
    const text = await resp.text();
    const blob = new Blob([text], { type: "application/javascript" });
    workerBlobUrl = URL.createObjectURL(blob);
    return workerBlobUrl;
  }

  // =========================================================================
  //  OVERLAY — our own canvas container, on document.body, outside React
  //
  //  React re-renders the player component and destroys any foreign DOM nodes
  //  inside it. By placing our canvas on document.body and positioning it
  //  with position:fixed over the video, React never touches it.
  //
  //  A requestAnimationFrame loop keeps the overlay aligned with the video.
  // =========================================================================

  function createOverlay() {
    destroyOverlay();

    overlayDiv = document.createElement("div");
    overlayDiv.id = "ass-subtitle-overlay";
    overlayDiv.style.cssText =
      "position:fixed; top:0; left:0; width:0; height:0; pointer-events:none; z-index:9999; overflow:hidden;";

    overlayCanvas = document.createElement("canvas");
    overlayCanvas.style.cssText =
      "display:block; position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none;";

    overlayDiv.appendChild(overlayCanvas);
    document.body.appendChild(overlayDiv);
    overlayParent = document.body;

    log("Overlay created on document.body (outside React).");
    return overlayCanvas;
  }

  function destroyOverlay() {
    stopPositionLoop();
    if (overlayDiv) {
      overlayDiv.remove();
      overlayDiv = null;
      overlayCanvas = null;
      overlayParent = null;
    }
  }

  /**
   * Calculate the actual video content rectangle within the <video> element.
   * The video element may be larger than the content due to letterboxing
   * (object-fit: contain). We need to position our canvas over just the
   * content area so JASSUB's coordinate system matches correctly.
   */
  function getVideoContentRect(video) {
    const elemRect = video.getBoundingClientRect();
    const elemW = elemRect.width;
    const elemH = elemRect.height;
    const vidW = video.videoWidth;
    const vidH = video.videoHeight;

    // If we don't know the native resolution yet, use the full element
    if (!vidW || !vidH) {
      return elemRect;
    }

    const elemAR = elemW / elemH;
    const vidAR = vidW / vidH;
    let renderW, renderH, offsetX, offsetY;

    if (vidAR > elemAR) {
      // Video is wider than element — black bars top/bottom
      renderW = elemW;
      renderH = elemW / vidAR;
      offsetX = 0;
      offsetY = (elemH - renderH) / 2;
    } else {
      // Video is taller than element — black bars left/right
      renderH = elemH;
      renderW = elemH * vidAR;
      offsetX = (elemW - renderW) / 2;
      offsetY = 0;
    }

    return {
      left: elemRect.left + offsetX,
      top: elemRect.top + offsetY,
      width: renderW,
      height: renderH,
    };
  }

  /**
   * RAF loop: every frame, position our overlay exactly over the video
   * CONTENT area (excluding letterbox bars).
   *
   * Also handles fullscreen: when the browser fullscreens an element,
   * it creates a top-level stacking context that sits above document.body.
   * Our overlay must be INSIDE that element to be visible.
   * When fullscreen exits, we move it back to document.body.
   */
  function reparentOverlay(newParent) {
    if (!overlayDiv || overlayParent === newParent) return;
    newParent.appendChild(overlayDiv);
    overlayParent = newParent;
    log("Overlay reparented to " + (newParent === document.body ? "document.body" : "fullscreen element"));
  }

  function startPositionLoop() {
    stopPositionLoop();

    function update() {
      if (!videoElement || !overlayDiv) return;

      // Handle fullscreen: move overlay into/out of the fullscreen element
      const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
      if (fsEl) {
        // Fullscreen active — overlay must be inside the fullscreen element
        if (overlayParent !== fsEl) {
          reparentOverlay(fsEl);
        }
      } else {
        // Not fullscreen — overlay lives on document.body
        if (overlayParent !== document.body) {
          reparentOverlay(document.body);
        }
      }

      // If video element is gone from DOM, hide overlay but keep running
      if (!document.body.contains(videoElement) && !(fsEl && fsEl.contains(videoElement))) {
        overlayDiv.style.display = "none";

        const newVid =
          document.querySelector(".video-js video") ||
          document.querySelector("video");
        if (newVid && newVid !== videoElement && (newVid.src || newVid.currentSrc)) {
          log("Position loop: video element replaced, updating reference.");
          videoElement = newVid;
          if (jassubInstance) {
            try { jassubInstance.setVideo(newVid); } catch (e) {
              log("setVideo failed, will re-create JASSUB: " + e);
              recreateJassub(newVid);
            }
          }
        }

        positionRAF = requestAnimationFrame(update);
        return;
      }

      overlayDiv.style.display = isEnabled ? "" : "none";

      // Position over the actual video content, not the full element
      const rect = getVideoContentRect(videoElement);
      overlayDiv.style.left = rect.left + "px";
      overlayDiv.style.top = rect.top + "px";
      overlayDiv.style.width = rect.width + "px";
      overlayDiv.style.height = rect.height + "px";

      positionRAF = requestAnimationFrame(update);
    }

    positionRAF = requestAnimationFrame(update);
    log("Position loop started.");
  }

  function stopPositionLoop() {
    if (positionRAF) {
      cancelAnimationFrame(positionRAF);
      positionRAF = null;
    }
  }

  // =========================================================================
  //  JASSUB INSTANCE
  // =========================================================================

  function destroyJassub() {
    if (jassubInstance) {
      log("Destroying JASSUB instance.");
      try { jassubInstance.destroy(); } catch (e) { warn("Error destroying: " + e); }
      jassubInstance = null;
    }
  }

  async function createJassub(video, assText, canvas) {
    destroyJassub();
    if (!window.JASSUB) { warn("JASSUB not available."); return false; }

    try {
      const blobUrl = await getWorkerBlobUrl();
      const origin = window.location.origin;

      jassubInstance = new window.JASSUB({
        video: video,
        canvas: canvas,
        subContent: assText,
        workerUrl: blobUrl,
        wasmUrl: `${origin}${JASSUB_BASE}/jassub-worker.wasm`,
        availableFonts: { "liberation sans": `${origin}${JASSUB_BASE}/default.woff2` },
        fallbackFont: "liberation sans",
        prescaleFactor: 0.8,
        prescaleHeightLimit: 1080,
      });

      log("JASSUB instance created.");
      return true;
    } catch (e) {
      warn("Failed to create JASSUB: " + e);
      return false;
    }
  }

  /** Re-create JASSUB when video element changes but we keep our canvas */
  async function recreateJassub(newVideo) {
    if (!cachedAssText || !overlayCanvas) return;
    log("Re-creating JASSUB for new video element.");
    await createJassub(newVideo, cachedAssText, overlayCanvas);
  }

  // =========================================================================
  //  UI — TOOLBAR BUTTON
  // =========================================================================

  function createButton() {
    if (btnEl) return;
    const waitForToolbar = setInterval(() => {
      const toolbar =
        document.querySelector(".vjs-control-bar") ||
        document.querySelector(".video-js .vjs-control-bar");
      if (!toolbar) return;
      clearInterval(waitForToolbar);

      btnEl = document.createElement("button");
      btnEl.className = "vjs-control vjs-button ass-sub-btn";
      btnEl.title = "ASS Subtitles";
      btnEl.innerHTML = `<span class="ass-sub-icon">CC</span>`;
      btnEl.classList.add("ass-sub-active");
      btnEl.addEventListener("click", toggleSubtitles);

      const fsBtn = toolbar.querySelector(".vjs-fullscreen-control");
      if (fsBtn) toolbar.insertBefore(btnEl, fsBtn);
      else toolbar.appendChild(btnEl);
    }, 500);
    setTimeout(() => clearInterval(waitForToolbar), 15000);
  }

  function removeButton() {
    if (btnEl) { btnEl.remove(); btnEl = null; }
  }

  function toggleSubtitles() {
    isEnabled = !isEnabled;
    if (btnEl) {
      btnEl.querySelector(".ass-sub-icon").textContent = isEnabled ? "CC" : "cc";
      btnEl.classList.toggle("ass-sub-active", isEnabled);
    }
    // Overlay visibility is handled by the position loop
  }

  // =========================================================================
  //  SCENE INIT
  // =========================================================================

  function getSceneIdFromURL() {
    const m = window.location.pathname.match(/\/scenes\/(\d+)/);
    return m ? m[1] : null;
  }

  let videoPollTimer = null;

  async function initForScene(sceneId) {
    cleanup();
    currentSceneId = sceneId;
    if (!sceneId) return;

    log(`Scene ${sceneId}: loading subtitles...`);

    const loaded = await loadJASSUB();
    if (!loaded) { warn("Cannot proceed without JASSUB."); return; }
    if (currentSceneId !== sceneId) return;

    const result = await fetchSubtitleText(sceneId);
    if (!result) { log("No subtitles found."); return; }
    if (currentSceneId !== sceneId) return;

    let assText = null;
    if (result.tracks && Array.isArray(result.tracks) && result.tracks.length > 0) {
      assText = result.tracks[0].text;
    } else if (typeof result === "string") {
      assText = result;
    } else if (result.text) {
      assText = result.text;
    }

    if (!assText) { log("No usable ASS text."); return; }

    log(`Got ASS data (${assText.length} chars). Waiting for video...`);
    isEnabled = true;
    cachedAssText = assText;

    startVideoPoll(sceneId);
  }

  function startVideoPoll(sceneId) {
    stopVideoPoll();
    let attempts = 0;

    videoPollTimer = setInterval(async () => {
      attempts++;
      if (currentSceneId !== sceneId) { stopVideoPoll(); return; }

      const vid =
        document.querySelector(".video-js video") ||
        document.querySelector("video");
      if (!vid) return;
      if (!vid.src && !vid.currentSrc && !vid.querySelector("source")) return;

      stopVideoPoll();
      log(`Found video after ${attempts} attempts.`);

      videoElement = vid;

      // Create our overlay canvas outside React's DOM
      const canvas = createOverlay();

      // Create JASSUB with our own canvas
      const ok = await createJassub(vid, cachedAssText, canvas);
      if (ok) {
        startPositionLoop();
        createButton();
      }
    }, 500);

    setTimeout(() => {
      if (videoPollTimer) { log("Timed out waiting for video."); stopVideoPoll(); }
    }, 20000);
  }

  function stopVideoPoll() {
    if (videoPollTimer) { clearInterval(videoPollTimer); videoPollTimer = null; }
  }

  function cleanup() {
    stopVideoPoll();
    removeButton();
    destroyJassub();
    destroyOverlay();
    currentSceneId = null;
    cachedAssText = null;
    videoElement = null;
  }

  // =========================================================================
  //  NAVIGATION
  // =========================================================================

  function onLocationChange() {
    const sceneId = getSceneIdFromURL();
    if (sceneId) initForScene(sceneId);
    else cleanup();
  }

  if (window.PluginApi && window.PluginApi.Event) {
    window.PluginApi.Event.addEventListener("stash:location", () => setTimeout(onLocationChange, 500));
    log("Using PluginApi.Event for navigation.");
  } else {
    let lastURL = window.location.href;
    function check() {
      const url = window.location.href;
      if (url !== lastURL) { lastURL = url; onLocationChange(); }
    }
    new MutationObserver(check).observe(document.body, { childList: true, subtree: true });
    setInterval(check, 1000);
    log("Using MutationObserver fallback.");
  }

  const initialScene = getSceneIdFromURL();
  if (initialScene) setTimeout(() => initForScene(initialScene), 1500);

  log("Plugin loaded (JASSUB + external canvas).");
})();
