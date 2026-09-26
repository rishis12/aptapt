// Progressive enhancements. Every page works without this file.

document.addEventListener("change", (e) => {
  const form = e.target.closest("form[data-autosubmit]");
  if (form && e.target.value) form.submit();
});

document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

document.addEventListener("click", async (e) => {
  const confirmBtn = e.target.closest("[data-confirm-click]");
  if (confirmBtn && !window.confirm(confirmBtn.dataset.confirmClick)) {
    e.preventDefault();
    return;
  }

  const copy = e.target.closest("[data-copy]");
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      copy.textContent = "Copied";
    } catch {
      copy.previousElementSibling?.select();
      copy.textContent = "Press Ctrl+C";
    }
    setTimeout(() => (copy.textContent = "Copy"), 1600);
  }

  const thumb = e.target.closest("[data-gallery-src]");
  if (thumb) {
    document.querySelector("[data-gallery-main]").src = thumb.dataset.gallerySrc;
    document.querySelector("[data-gallery-credit]").textContent = thumb.dataset.galleryCaption;
    thumb.parentElement.querySelectorAll("button").forEach((b) => b.removeAttribute("aria-current"));
    thumb.setAttribute("aria-current", "true");
  }
});

// Don't swap a live-updating panel out from under someone who is typing or has opened a form in it.
document.addEventListener("toggle", (e) => { if (e.target.matches("details[data-keep]")) e.target.dataset.touched = "1"; }, true);
document.body.addEventListener("htmx:beforeRequest", (e) => {
  const panel = e.detail.elt;
  if (panel.id !== "group-status") return;
  const typing = panel.contains(document.activeElement) && document.activeElement.matches("input, select, textarea");
  const busy = panel.querySelector("details[data-keep][data-touched][open]");
  if (typing || busy) e.preventDefault();
});

// Building page: the "add to shortlist" form posts to whichever of your groups is selected.
document.querySelector("[data-shortlist-target]")?.addEventListener("change", (e) => {
  e.target.closest("form").action = e.target.value;
});

// ---- checkout: card formatting, brand, test fill, simulated processing ----
const cardForm = document.querySelector("[data-card-form]");
if (cardForm) {
  const number = cardForm.querySelector("[data-card-number]");
  const brand = cardForm.querySelector("[data-card-brand]");
  const exp = cardForm.querySelector("[data-card-exp]");

  const formatNumber = () => {
    const digits = number.value.replace(/\D/g, "").slice(0, 16);
    number.value = digits.replace(/(\d{4})(?=\d)/g, "$1 ");
    brand.textContent = digits.startsWith("4") ? "VISA" : digits.startsWith("5") ? "MASTERCARD" : "";
  };
  number.addEventListener("input", formatNumber);
  exp.addEventListener("input", (e) => {
    const d = exp.value.replace(/\D/g, "").slice(0, 4);
    exp.value = d.length > 2 ? `${d.slice(0, 2)}/${d.slice(2)}` : d;
    if (e.inputType === "deleteContentBackward" && d.length === 2) exp.value = d;
  });

  cardForm.querySelector("[data-fill-test]")?.addEventListener("click", () => {
    const yy = String((new Date().getFullYear() + 3) % 100).padStart(2, "0");
    number.value = "4242424242424242";
    formatNumber();
    exp.value = `12/${yy}`;
    cardForm.querySelector("[name=cvc]").value = "123";
    cardForm.querySelector("[name=postal]").value = "53703";
  });

  // A short "processing" beat so the payment feels like a real one.
  cardForm.addEventListener("submit", (e) => {
    if (cardForm.dataset.sent) return;
    e.preventDefault();
    const btn = cardForm.querySelector("[data-pay]");
    btn.classList.add("is-processing");
    btn.textContent = "Processing";
    cardForm.dataset.sent = "1";
    setTimeout(() => cardForm.submit(), 1100);
  });
}

// ---- lease signature preview ----
const sig = document.querySelector("[data-signature]");
if (sig) {
  const preview = document.querySelector("[data-signature-preview]");
  sig.addEventListener("input", () => (preview.textContent = sig.value));
}

// ---- browse map ----
window.addEventListener("load", () => {
  const el = document.getElementById("map");
  if (!el || !window.L) return;
  const center = JSON.parse(el.dataset.center);
  const markers = JSON.parse(el.dataset.markers);
  const radiusMeters = parseFloat(el.dataset.radius) * 1609.34;

  const map = L.map(el, { scrollWheelZoom: true, zoomControl: true });
  const esri = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas";
  L.tileLayer(`${esri}/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`, {
    maxZoom: 16,
    attribution: "Tiles &copy; Esri, HERE, Garmin, &copy; OpenStreetMap contributors",
  }).addTo(map);
  L.tileLayer(`${esri}/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}`, { maxZoom: 16 }).addTo(map);

  const centerLatLng = L.latLng(center.lat, center.lng);
  L.circle(centerLatLng, { radius: radiusMeters, color: "#0E2A3B", weight: 1.5, dashArray: "4 6", fillOpacity: 0.04 }).addTo(map);

  const points = [];
  for (const m of markers) {
    const count = m.committed ? `<b>${m.committed}${m.target ? "/" + m.target : ""}</b>` : "";
    const pin = document.createElement("div");
    pin.className = "map-pin";
    pin.textContent = m.name;
    pin.insertAdjacentHTML("beforeend", count);
    const icon = L.divIcon({ className: "", html: pin.outerHTML, iconSize: null });
    const marker = L.marker([m.lat, m.lng], { icon, riseOnHover: true }).addTo(map);
    points.push([m.lat, m.lng]);
    marker.on("click", () => (window.location = m.url));
    const card = document.querySelector(`[data-marker="${m.slug}"]`);
    const setHot = (on) => {
      card?.classList.toggle("is-hot", on);
      marker.getElement()?.querySelector(".map-pin")?.classList.toggle("is-hot", on);
    };
    marker.on("mouseover", () => setHot(true));
    marker.on("mouseout", () => setHot(false));
    card?.addEventListener("mouseenter", () => setHot(true));
    card?.addEventListener("mouseleave", () => setHot(false));
  }
  const frame = () => {
    if (points.length) map.fitBounds(L.latLngBounds(points).pad(0.35), { maxZoom: 16 });
    else map.fitBounds(centerLatLng.toBounds(radiusMeters * 2));
  };
  frame();
  // The panel's final size is only known after layout settles; re-measure so tiles cover all of it.
  let framed = false;
  new ResizeObserver(() => {
    map.invalidateSize();
    if (!framed) { frame(); framed = true; }
  }).observe(el);
});
