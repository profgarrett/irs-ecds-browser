/* IRS e-File Provider Map — static site, data built by pipeline/build_site.py */
(function () {
  "use strict";

  const BASEMAP = "https://tiles.openfreemap.org/styles/positron";
  const FALLBACK_STYLE = {
    version: 8, sources: {},
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#e9edf0" } }],
  };
  const SIZE_LABELS = { tiny: "Tiny <100", small: "Small 100+", medium: "Medium 1k+", large: "Large 10k+" };
  const css = getComputedStyle(document.documentElement);
  const SIZE_COLORS = ["tiny", "small", "medium", "large"].map((s) => css.getPropertyValue("--size-" + s).trim());

  const $ = (id) => document.getElementById(id);
  const fmt = new Intl.NumberFormat("en-US");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (v) => (v == null ? "–" : (v * 100).toFixed(1) + "%");
  const num = (v) => (v == null ? "–" : fmt.format(v));
  const phone = (p) => (p && p.length === 10 ? `(${p.slice(0, 3)}) ${p.slice(3, 6)}-${p.slice(6)}` : p || "");

  let D = null;            // points.json, decoded (see loadData)
  let names = null;        // names.json: n, sc, c, r — loaded after the map is drawn
  let namesReady = null;   // promise for names
  let searchText = null;   // lazily built lower-case search strings
  let mapReady = false, dataReady = false;
  const detailCache = new Map();

  // ---------------------------------------------------------------- map
  const map = new maplibregl.Map({
    container: "map", style: BASEMAP, center: [-96, 38.5], zoom: 3.6, minZoom: 2, maxZoom: 17,
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.GeolocateControl({ fitBoundsOptions: { maxZoom: 11 } }), "top-right");

  let styleFailed = false;
  map.on("error", (e) => {
    if (!mapReady && !styleFailed && e && e.error && /style|Failed to fetch|NetworkError/i.test(String(e.error.message))) {
      styleFailed = true;
      map.setStyle(FALLBACK_STYLE);
    }
  });
  map.on("load", onMapReady);
  map.on("style.load", () => { if (!mapReady) onMapReady(); });

  function onMapReady() {
    if (mapReady) return;
    mapReady = true;
    const hasGlyphs = !!map.getStyle().glyphs;
    map.addSource("firms", {
      type: "geojson", data: { type: "FeatureCollection", features: [] },
      cluster: true, clusterRadius: 48, clusterMaxZoom: 11,
    });
    map.addLayer({
      id: "clusters", type: "circle", source: "firms", filter: ["has", "point_count"],
      paint: {
        "circle-color": ["step", ["get", "point_count"], "#5b8fb9", 100, "#2f6f9f", 1000, "#1f4e79", 10000, "#14324f"],
        "circle-radius": ["step", ["get", "point_count"], 13, 100, 17, 1000, 22, 10000, 28],
        "circle-opacity": 0.85, "circle-stroke-width": 2, "circle-stroke-color": "rgba(255,255,255,.85)",
      },
    });
    if (hasGlyphs) {
      map.addLayer({
        id: "cluster-count", type: "symbol", source: "firms", filter: ["has", "point_count"],
        layout: { "text-field": ["get", "point_count_abbreviated"], "text-size": 12, "text-font": ["Noto Sans Bold"] },
        paint: { "text-color": "#fff" },
      });
    }
    map.addLayer({
      id: "points", type: "circle", source: "firms", filter: ["!", ["has", "point_count"]],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, ["+", 3, ["get", "z"]], 14, ["+", 6, ["*", 2, ["get", "z"]]]],
        "circle-color": ["case", ["==", ["get", "f"], 1], ["match", ["get", "z"], 0, SIZE_COLORS[0], 1, SIZE_COLORS[1], 2, SIZE_COLORS[2], SIZE_COLORS[3]], "#ffffff"],
        "circle-stroke-color": ["match", ["get", "z"], 0, SIZE_COLORS[0], 1, SIZE_COLORS[1], 2, SIZE_COLORS[2], SIZE_COLORS[3]],
        "circle-stroke-width": ["case", ["==", ["get", "f"], 1], 1, 2],
        "circle-opacity": ["case", ["==", ["get", "p"], 1], 0.55, 0.95],
        "circle-stroke-opacity": ["case", ["==", ["get", "p"], 1], 0.6, 1],
      },
    });

    map.on("click", "clusters", async (e) => {
      const f = e.features[0];
      const src = map.getSource("firms");
      const zoom = await src.getClusterExpansionZoom(f.properties.cluster_id);
      if (zoom > 11 || map.getZoom() >= 11) {
        // Many firms at one spot (same building or ZIP centroid): list them instead.
        const leaves = await src.getClusterLeaves(f.properties.cluster_id, 200, 0);
        showList(f.geometry.coordinates, leaves.map((l) => l.properties.i));
      } else {
        map.easeTo({ center: f.geometry.coordinates, zoom });
      }
    });
    map.on("click", "points", (e) => {
      const feats = e.features;
      if (feats.length > 1) showList(e.lngLat, feats.map((f) => f.properties.i));
      else showFirm(feats[0].geometry.coordinates, feats[0].properties.i);
    });
    for (const l of ["clusters", "points"]) {
      map.on("mouseenter", l, () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", l, () => (map.getCanvas().style.cursor = ""));
    }
    if (dataReady) apply();
  }

  // ---------------------------------------------------------------- data
  // "no-cache" makes the browser check with the server (a quick 304 when unchanged),
  // so a cached file from an older build is never mixed with newer ones.
  async function getJSON(url) {
    const res = await fetch(url, { cache: "no-cache" });
    if (!res.ok) throw new Error(`Could not load ${url} (${res.status})`);
    return res.json();
  }
  const STALE = "The map data was updated after this page loaded. Reload the page to see the latest data.";

  async function loadData() {
    const P = await getJSON("data/points.json");
    const N = P.lat.length;
    // Firms are in map order and each coordinate is stored as the change from the previous firm.
    const x = new Float64Array(N), y = new Float64Array(N);
    const z = new Uint8Array(N), f = new Uint8Array(N), p = new Uint8Array(N);
    let lon = 0, lat = 0;
    for (let k = 0; k < N; k++) {
      lon += P.lon[k]; lat += P.lat[k];
      x[k] = lon / P.scale; y[k] = lat / P.scale;
      const a = P.a[k];   // bits 0-1 size, bit 2 filer, bit 3 approximate (ZIP-center) location
      z[k] = a & 3; f[k] = (a >> 2) & 1; p[k] = (a >> 3) & 1;
    }
    D = { v: P.v, release: P.release, states: P.states, sizes: P.sizes, rowsPerShard: P.rows_per_shard,
          N, x, y, z, f, p, s: Int16Array.from(P.s) };
    $("release").textContent = `ECDS FOIA extract of ${D.release} · ${fmt.format(D.N)} firms mapped`;
    const sel = $("state");
    D.states.forEach((s, i) => sel.add(new Option(s, i)));
    buildSizeChips();
    namesReady = getJSON("data/names.json").then((n) => {
      if (n.v !== D.v) throw new Error(STALE);
      return (names = n);
    });
    namesReady.catch((err) => { $("search").placeholder = "Search unavailable: " + err.message; });
    dataReady = true;
    $("loading").hidden = true;
    if (mapReady) apply();
  }

  function buildSizeChips() {
    const counts = [0, 0, 0, 0];
    for (let k = 0; k < D.N; k++) counts[D.z[k]]++;
    $("sizes").innerHTML = D.sizes.map((s, i) => `
      <label class="size-chip"><input type="checkbox" value="${i}" checked>
        <span class="dot" style="background:${SIZE_COLORS[i]}"></span>${esc(SIZE_LABELS[s] || s)}
      </label>`).join("");
  }

  function currentFilter() {
    const filer = document.querySelector('input[name="filer"]:checked').value;
    const sizes = new Set([...document.querySelectorAll("#sizes input:checked")].map((x) => +x.value));
    const st = $("state").value === "" ? -1 : +$("state").value;
    const approx = $("approx").checked;
    const q = $("search").value.trim().toLowerCase();
    return { filer, sizes, st, approx, q };
  }

  function matches(k, F) {
    if (F.filer !== "all" && D.f[k] !== +F.filer) return false;
    if (!F.sizes.has(D.z[k])) return false;
    if (F.st >= 0 && D.s[k] !== F.st) return false;
    if (!F.approx && D.p[k] === 1) return false;
    if (F.q) {
      if (!searchText) searchText = names.n.map((n, i) => (n + " | " + names.sc[i] + " | " + names.c[i]).toLowerCase());
      if (!F.q.split(/\s+/).every((w) => searchText[k].includes(w))) return false;
    }
    return true;
  }

  let lastStateFit = -1;
  let waitingForNames = false;
  function apply() {
    if (!D || !mapReady) return;
    const F = currentFilter();
    if (F.q && !names) {
      // Search needs names.json, which loads after the map is drawn.
      $("count").textContent = "…";
      if (!waitingForNames) {
        waitingForNames = true;
        namesReady.then(() => { waitingForNames = false; apply(); }, () => {});
      }
      return;
    }
    const feats = [];
    const hits = [];
    let minX = 180, minY = 90, maxX = -180, maxY = -90;
    for (let k = 0; k < D.N; k++) {
      if (!matches(k, F)) continue;
      const x = D.x[k], y = D.y[k];
      feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [x, y] }, properties: { i: k, z: D.z[k], f: D.f[k], p: D.p[k] } });
      if (F.q) hits.push(k);
      if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
    map.getSource("firms").setData({ type: "FeatureCollection", features: feats });
    $("count").textContent = fmt.format(feats.length);
    renderResults(F.q ? hits.sort((a, b) => names.r[b] - names.r[a]).slice(0, 100) : null);
    // Zoom to a newly chosen state, or to search hits when there are few.
    if ((F.st >= 0 && F.st !== lastStateFit) || (F.q && feats.length > 0 && feats.length <= 200)) {
      if (feats.length) map.fitBounds([[minX, minY], [maxX, maxY]], { padding: 60, maxZoom: 13, duration: 600 });
    }
    lastStateFit = F.st;
  }

  function renderResults(hits) {
    const box = $("results");
    if (!hits) { box.hidden = true; return; }
    box.hidden = false;
    $("result-list").innerHTML = hits.length ? hits.map((k) => `
      <li tabindex="0" data-k="${k}"><span class="r-name">${esc(names.n[k])}</span>
      <span class="r-meta">${esc(names.c[k])}${D.s[k] >= 0 ? ", " + D.states[D.s[k]] : ""} · ${num(names.r[k])} returns</span></li>`).join("")
      : `<li>No matches with the current filters.</li>`;
  }
  $("result-list").addEventListener("click", (e) => openFromList(e.target.closest("li")));
  $("result-list").addEventListener("keydown", (e) => { if (e.key === "Enter") openFromList(e.target.closest("li")); });
  function openFromList(li) {
    if (!li || li.dataset.k == null) return;
    const k = +li.dataset.k;
    const ll = [D.x[k], D.y[k]];
    // Put the firm in the lower part of the screen so the popup fits above it.
    map.flyTo({ center: ll, zoom: Math.max(map.getZoom(), 13), offset: [0, Math.round(map.getContainer().clientHeight * 0.3)] });
    showFirm(ll, k);
  }

  // ---------------------------------------------------------------- popups
  let popup = null;
  function openPopup(lngLat, html) {
    if (popup) popup.remove();
    popup = new maplibregl.Popup({ maxWidth: "360px", focusAfterOpen: false, offset: 10 }).setLngLat(lngLat).setHTML(html).addTo(map);
    return popup;
  }

  async function showList(lngLat, ks) {
    ks = [...new Set(ks)];
    const head = `<div class="pop"><h2>${fmt.format(ks.length)} firms here</h2>`;
    if (!names) {
      openPopup(lngLat, `${head}<p class="note">Loading names…</p></div>`);
      try { await namesReady; } catch (err) { popup.setHTML(`${head}<p class="note">${esc(err.message)}</p></div>`); return; }
    }
    ks.sort((a, b) => names.r[b] - names.r[a]);
    const html = `${head}
      <p class="note">Firms at the same address or ZIP-code center.</p>
      <ul class="pop-list">${ks.map((k) => `<li data-k="${k}">${esc(names.n[k])} <span class="note">· ${num(names.r[k])} returns</span></li>`).join("")}</ul></div>`;
    const p = openPopup(lngLat, html);
    p.getElement().querySelector(".pop-list").addEventListener("click", (e) => {
      const li = e.target.closest("li");
      if (li) showFirm(lngLat, +li.dataset.k);
    });
  }

  // Popup details for firm k are row k % rowsPerShard of details/<k / rowsPerShard>.json.
  async function details(k) {
    const shard = Math.floor(k / D.rowsPerShard);
    if (!detailCache.has(shard)) {
      const req = getJSON(`data/details/${shard}.json`);
      detailCache.set(shard, req);
      req.catch(() => detailCache.delete(shard));   // allow a retry on the next click
    }
    const S = await detailCache.get(shard);
    if (S.v !== D.v) throw new Error(STALE);
    return S.rows[k % D.rowsPerShard] || {};
  }

  async function showFirm(lngLat, k) {
    const title = names ? `<h2>${esc(names.n[k])}</h2>` : "";
    openPopup(lngLat, `<div class="pop">${title}<p class="note">Loading details…</p></div>`);
    let d = {};
    try { d = await details(k); } catch (err) {
      if (err.message === STALE) { popup.setHTML(`<div class="pop">${title}<p class="note">${esc(STALE)}</p></div>`); return; }
      d = {};
    }
    const list = (s) => (s ? `<ul>${s.split("; ").map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="note">None listed</p>`);
    const sizeName = D.sizes[D.z[k]];
    const html = `<div class="pop">
      <h2>${esc(d.dba || (names && names.n[k]) || d.legal || "")}</h2>
      ${d.legal && d.legal !== d.dba ? `<p class="legal">${esc(d.legal)}</p>` : ""}
      <div class="badges">
        <span class="badge">${D.f[k] ? "Filer" : "Non-filer"} this year</span>
        <span class="badge">${esc(SIZE_LABELS[sizeName] || sizeName)}</span>
        ${d.sup && d.sup.toLowerCase() !== (d.dba || "").toLowerCase() ? `<span class="badge">${esc(d.sup)}</span>` : ""}
        ${d.prec === "zip" ? `<span class="badge">Approx. location</span>` : ""}
      </div>
      <dl>
        <dt>Address</dt><dd>${esc(d.addr || "")}</dd>
        ${d.county ? `<dt>County</dt><dd>${esc(d.county)}</dd>` : ""}
        ${d.phone ? `<dt>Phone</dt><dd>${esc(phone(d.phone))}</dd>` : ""}
        ${d.ero ? `<dt>ERO since</dt><dd>${esc(d.ero)}</dd>` : ""}
        ${d.roles ? `<dt>Roles</dt><dd>${esc(d.roles)}</dd>` : ""}
        ${d.id ? `<dt>Customer ID</dt><dd>${d.id}</dd>` : ""}
      </dl>
      <table>
        <tr><th>Returns</th><th>This year</th><th>Prior</th><th>2 yrs ago</th></tr>
        <tr><td>Transmitted</td><td>${num(d.ytd)}</td><td>${num(d.prv1)}</td><td>${num(d.prv2)}</td></tr>
        <tr><td>Reject rate</td><td>${pct(d.rr)}</td><td>${pct(d.rr1)}</td><td></td></tr>
      </table>
      <h3>Partners (${num(d.pt || 0)}, ${num(d.pl || 0)} licensed)</h3>${list(d.partners)}
      <h3>Contacts</h3>${list(d.contacts)}
    </div>`;
    if (popup && popup.isOpen()) popup.setHTML(html); else openPopup(lngLat, html);
  }

  // ---------------------------------------------------------------- UI wiring
  let t = null;
  const debounced = () => { clearTimeout(t); t = setTimeout(apply, 250); };
  $("search").addEventListener("input", debounced);
  document.querySelectorAll('input[name="filer"]').forEach((x) => x.addEventListener("change", apply));
  $("sizes").addEventListener("change", apply);
  $("state").addEventListener("change", apply);
  $("approx").addEventListener("change", apply);
  $("toggle").addEventListener("click", () => {
    const body = $("panel-body");
    const open = body.hidden;
    body.hidden = !open;
    $("toggle").setAttribute("aria-expanded", String(open));
  });
  if (window.matchMedia("(max-width: 640px)").matches) { $("panel-body").hidden = true; $("toggle").setAttribute("aria-expanded", "false"); }

  loadData().catch((err) => { $("loading-text").textContent = err.message; });
})();
