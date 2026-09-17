"use strict";

/* ---------- Constants ---------- */

const MODE_COLORS = {
  walk: "#2e7d32",
  bike: "#ef6c00",
  transit: "#1565c0",
  car: "#c62828",
};

// All modes are computed locally (own street graph + RAPTOR) and support up
// to 10 hours. The slider is logarithmic: position 0..100 → 5..600 minutes,
// so short budgets keep fine granularity.
const MIN_MINUTES = 5;
const MAX_MINUTES = 600;
const SLIDER_POSITIONS = 100;

function sliderToMinutes(position) {
  const ratio = MAX_MINUTES / MIN_MINUTES;
  const raw = MIN_MINUTES * Math.pow(ratio, position / SLIDER_POSITIONS);
  const step = raw < 120 ? 5 : 15; // finer snapping under 2 h
  return Math.min(MAX_MINUTES, Math.max(MIN_MINUTES, Math.round(raw / step) * step));
}

const DEFAULT_ORIGIN = { lat: 48.1436, lon: 17.1093 }; // Hlavné námestie, Bratislava
const INITIAL_ZOOM = 13;
const SLIDER_DEBOUNCE_MS = 400;
const TOAST_MS = 3500;
const GEOLOCATION_TIMEOUT_MS = 10000;
const FIT_PADDING = [40, 40];

/* ---------- State ---------- */

const state = {
  mode: "walk",
  minutes: 15,
  origin: { ...DEFAULT_ORIGIN },
  departNow: true,
  lastQuery: null, // query string of the most recent request; reused by "Skúsiť znova"
  abortController: null, // in-flight request, aborted when a newer one starts
  resultLayer: null, // currently displayed isochrone layer
  sliderTimer: null,
  toastTimer: null,
};

/* ---------- DOM ---------- */

const el = {
  modeButtons: document.querySelectorAll("#mode-switch .seg-btn"),
  minutesSlider: document.getElementById("minutes-slider"),
  minutesLabel: document.getElementById("minutes-label"),
  locateBtn: document.getElementById("locate-btn"),
  departRow: document.getElementById("depart-row"),
  departTime: document.getElementById("depart-time"),
  departNow: document.getElementById("depart-now"),
  loading: document.getElementById("loading"),
  info: document.getElementById("info"),
  error: document.getElementById("error"),
  errorText: document.querySelector("#error .error-text"),
  retryBtn: document.getElementById("retry-btn"),
  toast: document.getElementById("toast"),
};

/* ---------- Map ---------- */

const map = L.map("map", { zoomControl: false }).setView(
  [DEFAULT_ORIGIN.lat, DEFAULT_ORIGIN.lon],
  INITIAL_ZOOM
);

L.control.zoom({ position: "topright" }).addTo(map);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">Prispievatelia OpenStreetMap</a>' +
    " | MHD + vlak: GTFS DPB / ZSSK",
}).addTo(map);

// Marker lives in Leaflet's markerPane (z-index 600), which stacks above the
// polygon overlayPane (z-index 400), so the origin stays visible on top.
const originMarker = L.marker([DEFAULT_ORIGIN.lat, DEFAULT_ORIGIN.lon], {
  draggable: true,
}).addTo(map);

/* ---------- Formatting helpers ---------- */

function formatNumber(value) {
  if (!Number.isFinite(value)) return "?";
  return value.toLocaleString("sk-SK", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
}

function formatMinutesLabel(minutes) {
  if (minutes < 60) return `Čas: ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `Čas: ${hours} h` : `Čas: ${hours} h ${rest} min`;
}

function currentTimeHHMM() {
  const now = new Date();
  const hours = String(now.getHours()).padStart(2, "0");
  const minutes = String(now.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

/* ---------- Status UI (loading / info / error are mutually exclusive) ---------- */

function showLoading() {
  el.loading.classList.remove("hidden");
  el.info.classList.add("hidden");
  el.error.classList.add("hidden");
}

function showInfo(properties) {
  const area = formatNumber(properties.area_km2);
  const seconds = formatNumber(properties.compute_ms / 1000);
  el.info.textContent = `Plocha: ${area} km² · vypočítané za ${seconds} s`;
  el.loading.classList.add("hidden");
  el.info.classList.remove("hidden");
  el.error.classList.add("hidden");
}

const DEFAULT_ERROR_TEXT = "Nepodarilo sa vypočítať dosah. Skús znova.";

function showError(detail) {
  el.errorText.textContent = detail || DEFAULT_ERROR_TEXT;
  el.loading.classList.add("hidden");
  el.info.classList.add("hidden");
  el.error.classList.remove("hidden");
}

function showToast(message) {
  el.toast.textContent = message;
  el.toast.classList.remove("hidden");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => el.toast.classList.add("hidden"), TOAST_MS);
}

/* ---------- Isochrone request ---------- */

function buildQuery() {
  const params = new URLSearchParams({
    lat: state.origin.lat.toFixed(6),
    lon: state.origin.lon.toFixed(6),
    minutes: String(state.minutes),
    mode: state.mode,
  });
  if (state.mode === "transit" && !state.departNow && el.departTime.value) {
    params.set("depart", el.departTime.value);
  }
  return params.toString();
}

function requestCompute() {
  clearTimeout(state.sliderTimer); // avoid a duplicate request from a pending debounce
  state.lastQuery = buildQuery();
  runRequest(state.lastQuery);
}

function retryLastRequest() {
  if (state.lastQuery) runRequest(state.lastQuery);
}

async function readErrorDetail(response) {
  try {
    const data = await response.json();
    return typeof data.detail === "string" ? data.detail : null;
  } catch (_err) {
    return null; // non-JSON error body; the generic banner is shown either way
  }
}

async function runRequest(query) {
  if (state.abortController) state.abortController.abort();
  const controller = new AbortController();
  state.abortController = controller;
  showLoading();

  let payload;
  try {
    const response = await fetch(`/api/isochrone?${query}`, {
      signal: controller.signal,
    });
    if (!response.ok) {
      const detail = await readErrorDetail(response);
      console.error(`Isochrone API ${response.status}:`, detail ?? "(no detail)");
      // 4xx carries an actionable Slovak message (bad origin, off-network…);
      // 5xx keeps the generic retry text.
      const showDetail = response.status < 500 ? detail : null;
      if (controller === state.abortController) showError(showDetail);
      return;
    }
    payload = await response.json();
  } catch (err) {
    if (err.name === "AbortError") return; // superseded by a newer request
    console.error("Isochrone request failed:", err);
    if (controller === state.abortController) showError();
    return;
  }

  if (controller !== state.abortController) return; // stale response, ignore
  renderResult(payload);
}

/* ---------- Rendering ---------- */

function renderResult(geojson) {
  const feature =
    geojson && Array.isArray(geojson.features) ? geojson.features[0] : null;
  if (!feature || !feature.geometry) {
    console.error("Isochrone response has no feature:", geojson);
    showError();
    return;
  }

  const properties = feature.properties || {};
  const color = MODE_COLORS[properties.mode] || MODE_COLORS[state.mode];

  if (state.resultLayer) {
    map.removeLayer(state.resultLayer);
    state.resultLayer = null;
  }

  state.resultLayer = L.geoJSON(geojson, {
    interactive: false, // clicks fall through to the map (= move the start marker)
    style: { color, weight: 2, fillColor: color, fillOpacity: 0.35 },
  }).addTo(map);

  const bounds = state.resultLayer.getBounds();
  if (bounds.isValid()) map.fitBounds(bounds, { padding: FIT_PADDING });

  showInfo(properties);
}

/* ---------- Event handlers ---------- */

function setOrigin(lat, lon) {
  state.origin = { lat, lon };
  originMarker.setLatLng([lat, lon]);
  requestCompute();
}

function onModeClick(event) {
  const button = event.currentTarget;
  const mode = button.dataset.mode;
  if (mode === state.mode) return;
  state.mode = mode;
  el.modeButtons.forEach((b) => b.classList.toggle("active", b === button));
  el.departRow.classList.toggle("hidden", mode !== "transit");
  requestCompute();
}

function onSliderInput() {
  state.minutes = sliderToMinutes(Number(el.minutesSlider.value));
  el.minutesLabel.textContent = formatMinutesLabel(state.minutes);
  clearTimeout(state.sliderTimer);
  state.sliderTimer = setTimeout(requestCompute, SLIDER_DEBOUNCE_MS);
}

function onDepartNowChange() {
  state.departNow = el.departNow.checked;
  el.departTime.disabled = state.departNow;
  requestCompute();
}

function onDepartTimeChange() {
  if (!state.departNow) requestCompute();
}

function onLocateClick() {
  if (!navigator.geolocation) {
    showToast("Poloha nedostupná, klikni na mapu.");
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (position) => setOrigin(position.coords.latitude, position.coords.longitude),
    () => showToast("Poloha nedostupná, klikni na mapu."),
    { timeout: GEOLOCATION_TIMEOUT_MS, maximumAge: 60000 }
  );
}

/* ---------- Wiring & startup ---------- */

function init() {
  el.departTime.value = currentTimeHHMM();

  el.modeButtons.forEach((button) => button.addEventListener("click", onModeClick));
  el.minutesSlider.addEventListener("input", onSliderInput);
  el.locateBtn.addEventListener("click", onLocateClick);
  el.departNow.addEventListener("change", onDepartNowChange);
  el.departTime.addEventListener("change", onDepartTimeChange);
  el.retryBtn.addEventListener("click", retryLastRequest);

  map.on("click", (event) => setOrigin(event.latlng.lat, event.latlng.lng));
  originMarker.on("dragend", () => {
    const position = originMarker.getLatLng();
    setOrigin(position.lat, position.lng);
  });

  requestCompute(); // default isochrone (Pešo, 15 min) right after load
}

init();
