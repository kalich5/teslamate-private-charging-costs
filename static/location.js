// Location editor: price lists (add / remove), tariff rows, 24 h bars with
// gap check, and the TeslaMate geo-fence picker.
(function () {
  "use strict";

  const form = document.getElementById("location-form");
  const lists = document.getElementById("price-lists");
  const rowTemplate = document.getElementById("row-template");
  const field = (name) => form.elements.namedItem(name);
  const t = (key, fallback) => (window.I18N && window.I18N[key]) || fallback;

  const toMinutes = (value) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec((value || "").trim());
    if (!m) return null;
    const minutes = Number(m[1]) * 60 + Number(m[2]);
    return minutes <= 1440 ? minutes : null;
  };
  const fmt = (minutes) =>
    String(Math.floor(minutes / 60)).padStart(2, "0") + ":" + String(minutes % 60).padStart(2, "0");

  // ── one price list ("period") ──────────────────────────────────────────
  const pid = (period) => period.dataset.period;
  const tableFor = (period, day) => period.querySelector(`table.tariffs[data-day="${day}"]`);

  function rows(period, day) {
    return Array.from(tableFor(period, day).tBodies[0].rows).map((tr) => {
      const [from, to, price] = tr.querySelectorAll("input");
      return { tr, from, to, price };
    });
  }

  function addRow(period, day, values) {
    const tr = rowTemplate.content.firstElementChild.cloneNode(true);
    const [from, to, price] = tr.querySelectorAll("input");
    from.name = `${pid(period)}_${day}_from`;
    to.name = `${pid(period)}_${day}_to`;
    price.name = `${pid(period)}_${day}_price`;
    from.value = values.from || "";
    to.value = values.to || "";
    price.value = values.price ?? "";
    tableFor(period, day).tBodies[0].appendChild(tr);
    return tr;
  }

  // "Add time block" splits the last block in half, so the day stays covered.
  function onAdd(period, day) {
    const list = rows(period, day);
    const last = list[list.length - 1];
    if (!last) {
      addRow(period, day, { from: "00:00", to: "24:00" });
    } else {
      const start = toMinutes(last.from.value) ?? 0;
      const end = toMinutes(last.to.value) ?? 1440;
      // Split at the middle, rounded down to a whole hour when possible.
      let mid = start + Math.floor((end - start) / 120) * 60;
      if (mid <= start) mid = start + Math.floor((end - start) / 2);
      if (mid > start && mid < end) {
        last.to.value = fmt(mid);
        addRow(period, day, { from: fmt(mid), to: fmt(end) }).querySelector(".price").focus();
      }
    }
    render(period, day);
  }

  function onRemoveRow(period, day, button) {
    const list = rows(period, day);
    const i = list.findIndex((r) => r.tr.contains(button));
    if (list.length > 1) {
      // Give the removed block's time to its neighbour.
      if (i > 0) list[i - 1].to.value = list[i].to.value;
      else list[1].from.value = list[0].from.value;
      list[i].tr.remove();
    }
    render(period, day);
  }

  // Keep blocks contiguous: changing a block's end moves the next block's start.
  function onInput(period, day, target) {
    const list = rows(period, day);
    const i = list.findIndex((r) => r.tr.contains(target));
    if (i >= 0 && target === list[i].to && list[i + 1]) {
      list[i + 1].from.value = list[i].to.value;
    }
    render(period, day);
  }

  const addSeg = (bar, cls, start, end, text, title) => {
    const seg = document.createElement("div");
    seg.className = `seg ${cls}`;
    seg.style.left = `${start / 14.4}%`;
    seg.style.width = `${(end - start) / 14.4}%`;
    seg.textContent = text;
    seg.title = title;
    bar.appendChild(seg);
  };

  // Redraw the 24 h bar and point out gaps or overlaps.
  function render(period, day) {
    const out = period.querySelector(`.coverage[data-day="${day}"]`);
    const bar = period.querySelector(`.tariff-bar[data-day="${day}"]`);
    if (!out || !bar) return;
    const valid = [];
    const covered = new Array(1440).fill(0);
    for (const r of rows(period, day)) {
      const start = toMinutes(r.from.value);
      const end = toMinutes(r.to.value);
      const bad = start === null || end === null || end <= start;
      r.tr.classList.toggle("is-invalid", bad);
      if (bad) continue;
      for (let m = start; m < end; m++) covered[m]++;
      valid.push({ start, end, price: parseFloat(r.price.value.replace(",", ".")), r });
    }

    // Colour by price rank: cheapest green (0) … most expensive red (4).
    const prices = [...new Set(valid.map((v) => v.price).filter((p) => !isNaN(p)))].sort((a, b) => a - b);
    bar.innerHTML = "";
    for (const v of valid) {
      const level = isNaN(v.price) || prices.length < 2 ? 0
        : Math.round((prices.indexOf(v.price) / (prices.length - 1)) * 4);
      addSeg(bar, `lvl${level}`, v.start, v.end, isNaN(v.price) ? "" : String(v.price),
        `${v.r.from.value}–${v.r.to.value}: ${v.r.price.value}`);
    }

    const problems = [];
    for (let m = 0; m < 1440; ) {
      if (covered[m] === 1) { m++; continue; }
      const gap = covered[m] === 0;
      let n = m;
      while (n < 1440 && (gap ? covered[n] === 0 : covered[n] > 1)) n++;
      const text = `${gap ? t("no_price", "No price") : t("overlap", "Overlap")} ${fmt(m)}–${fmt(n)}`;
      if (gap) addSeg(bar, "gap", m, n, "", text);
      problems.push(text);
      m = n;
    }
    out.textContent = problems.join(", ");
  }

  // Weekend "same as weekdays": hide and disable the weekend table.
  function toggleWeekend(period) {
    const same = period.querySelector(".weekend-same");
    const wrap = period.querySelector(".weekend-wrap");
    wrap.hidden = same.checked;
    wrap.querySelectorAll("input, button").forEach((el) => { el.disabled = same.checked; });
  }

  function initPeriod(period) {
    toggleWeekend(period);
    ["weekday", "weekend"].forEach((day) => render(period, day));
  }

  // ── price lists ────────────────────────────────────────────────────────
  let counter = 0;
  const periods = () => Array.from(lists.querySelectorAll(".price-list"));

  // New price list: a copy of the newest one, valid from today, shown on top.
  function addPeriod() {
    const source = periods()[0];
    if (!source) return;
    let id;
    do { id = `n${++counter}`; } while (lists.querySelector(`[data-period="${id}"]`));
    const old = pid(source);
    const copy = source.cloneNode(true);
    copy.dataset.period = id;
    copy.querySelectorAll("[name]").forEach((el) => {
      if (el.name === "period") el.value = id;
      else if (el.name.startsWith(`${old}_`)) el.name = id + el.name.slice(old.length);
    });
    // cloneNode does not copy what the user typed into inputs — copy values.
    const src = source.querySelectorAll("input");
    copy.querySelectorAll("input").forEach((el, i) => {
      if (el.type === "checkbox") el.checked = src[i].checked;
      else if (el.name !== "period") el.value = src[i].value;
    });
    copy.querySelector(".valid-from").value = window.TODAY || "";
    lists.prepend(copy);
    initPeriod(copy);
    copy.querySelector(".valid-from").focus();
  }

  function removePeriod(period) {
    if (periods().length <= 1) {
      alert(t("last_price_list", "A location needs at least one price list."));
      return;
    }
    period.remove();
  }

  // ── events ─────────────────────────────────────────────────────────────
  document.getElementById("add-period").addEventListener("click", addPeriod);

  lists.addEventListener("click", (event) => {
    const period = event.target.closest(".price-list");
    if (!period) return;
    const add = event.target.closest(".add");
    if (add) { onAdd(period, add.dataset.day); return; }
    const remove = event.target.closest(".remove");
    if (remove) { onRemoveRow(period, remove.closest("table").dataset.day, remove); return; }
    if (event.target.closest(".remove-period")) removePeriod(period);
  });
  lists.addEventListener("input", (event) => {
    const period = event.target.closest(".price-list");
    const table = event.target.closest("table.tariffs");
    if (period && table) onInput(period, table.dataset.day, event.target);
  });
  lists.addEventListener("change", (event) => {
    if (event.target.classList.contains("weekend-same")) {
      toggleWeekend(event.target.closest(".price-list"));
    }
  });

  periods().forEach(initPeriod);

  // ── TeslaMate geo-fences ───────────────────────────────────────────────
  const select = document.getElementById("geofence");
  const hint = document.getElementById("geofence-hint");
  fetch(window.GEOFENCES_URL, { credentials: "same-origin" })
    .then((r) => r.json())
    .then((data) => {
      const list = data.geofences || [];
      select.innerHTML = "";
      const empty = data.error ? t("unavailable", "Unavailable") : t("no_geofences", "No geo-fences in TeslaMate");
      select.add(new Option(list.length ? t("choose", "— choose —") : empty, ""));
      list.forEach((g, i) => select.add(new Option(`${g.name} (${g.radius_m} m)`, String(i))));
      select.disabled = !list.length;
      if (data.error) hint.textContent = t("geofences_error", "Could not load geo-fences from TeslaMate.");
      select.addEventListener("change", () => {
        const g = list[Number(select.value)];
        if (!g || select.value === "") return;
        if (!field("name").value) field("name").value = g.name;
        field("lat").value = g.lat;
        field("lon").value = g.lon;
        if (g.radius_m) field("radius_m").value = g.radius_m;
      });
    })
    .catch(() => {
      select.innerHTML = "";
      select.add(new Option(t("unavailable", "Unavailable"), ""));
      select.disabled = true;
      hint.textContent = t("geofences_error", "Could not load geo-fences from TeslaMate.");
    });
})();
