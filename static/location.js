// Location editor: tariff rows (+ / ✕), live 24 h preview, geofence picker.
(function () {
  "use strict";

  const form = document.getElementById("location-form");
  const field = (name) => form.elements.namedItem(name);
  const template = document.getElementById("row-template");

  const toMinutes = (value) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec((value || "").trim());
    if (!m) return null;
    const minutes = Number(m[1]) * 60 + Number(m[2]);
    return minutes <= 1440 ? minutes : null;
  };
  const fmt = (minutes) =>
    String(Math.floor(minutes / 60)).padStart(2, "0") + ":" + String(minutes % 60).padStart(2, "0");

  function tableFor(day) {
    return form.querySelector(`table.tariffs[data-day="${day}"]`);
  }

  function rows(day) {
    return Array.from(tableFor(day).tBodies[0].rows).map((tr) => {
      const [from, to, price] = tr.querySelectorAll("input");
      return { tr, from, to, price };
    });
  }

  function addRow(day, values) {
    const tr = template.content.firstElementChild.cloneNode(true);
    const [from, to, price] = tr.querySelectorAll("input");
    from.name = `${day}_from`;
    to.name = `${day}_to`;
    price.name = `${day}_price`;
    from.value = values.from || "";
    to.value = values.to || "";
    price.value = values.price ?? "";
    tableFor(day).tBodies[0].appendChild(tr);
    return tr;
  }

  // "+" splits the last block in half, so the day stays fully covered.
  function onAdd(day) {
    const list = rows(day);
    const last = list[list.length - 1];
    if (!last) {
      addRow(day, { from: "00:00", to: "24:00" });
    } else {
      const start = toMinutes(last.from.value) ?? 0;
      const end = toMinutes(last.to.value) ?? 1440;
      // Split at the middle, rounded down to a whole hour when possible.
      let mid = start + Math.floor((end - start) / 120) * 60;
      if (mid <= start) mid = start + Math.floor((end - start) / 2);
      if (mid > start && mid < end) {
        last.to.value = fmt(mid);
        addRow(day, { from: fmt(mid), to: fmt(end) }).querySelector(".price").focus();
      }
    }
    render(day);
  }

  // Keep blocks contiguous: changing a block's end moves the next block's start.
  function onChange(day, event) {
    const list = rows(day);
    const i = list.findIndex((r) => r.tr.contains(event.target));
    if (i >= 0 && event.target === list[i].to && list[i + 1]) {
      list[i + 1].from.value = list[i].to.value;
    }
    render(day);
  }

  function render(day) {
    const bar = form.querySelector(`.timeline.preview[data-day="${day}"]`);
    if (!bar) return;
    const list = rows(day);
    const prices = [...new Set(list.map((r) => parseFloat(r.price.value.replace(",", "."))).filter((p) => !isNaN(p)))]
      .sort((a, b) => a - b);
    const covered = new Array(1440).fill(0);
    bar.innerHTML = "";
    let problem = false;
    for (const r of list) {
      const start = toMinutes(r.from.value);
      const end = toMinutes(r.to.value);
      const price = parseFloat(r.price.value.replace(",", "."));
      const bad = start === null || end === null || end <= start;
      r.tr.classList.toggle("invalid", bad);
      if (bad) { problem = true; continue; }
      for (let m = start; m < end; m++) covered[m]++;
      const seg = document.createElement("div");
      const level = isNaN(price) || prices.length < 2 ? 0 : Math.round((prices.indexOf(price) / (prices.length - 1)) * 4);
      seg.className = `seg lvl${level}`;
      seg.style.left = `${start / 14.4}%`;
      seg.style.width = `${(end - start) / 14.4}%`;
      seg.title = `${r.from.value}–${r.to.value}`;
      seg.innerHTML = `<span>${isNaN(price) ? "?" : price}</span>`;
      bar.appendChild(seg);
    }
    // Mark gaps and overlaps.
    for (let m = 0; m < 1440; ) {
      if (covered[m] === 1) { m++; continue; }
      const kind = covered[m] === 0 ? "gap" : "overlap";
      let n = m;
      while (n < 1440 && (kind === "gap" ? covered[n] === 0 : covered[n] > 1)) n++;
      const seg = document.createElement("div");
      seg.className = `seg ${kind}`;
      seg.style.left = `${m / 14.4}%`;
      seg.style.width = `${(n - m) / 14.4}%`;
      seg.title = `${kind === "gap" ? "No tariff" : "Overlap"} ${fmt(m)}–${fmt(n)}`;
      bar.appendChild(seg);
      problem = true;
      m = n;
    }
    bar.classList.toggle("has-problem", problem);
  }

  form.addEventListener("click", (event) => {
    const add = event.target.closest(".add");
    if (add) { onAdd(add.dataset.day); return; }
    const remove = event.target.closest(".remove");
    if (remove) {
      const table = remove.closest("table");
      const day = table.dataset.day;
      const list = rows(day);
      const i = list.findIndex((r) => r.tr.contains(remove));
      if (list.length > 1) {
        // Give the removed block's time to its neighbour.
        if (i > 0) list[i - 1].to.value = list[i].to.value;
        else list[1].from.value = list[0].from.value;
        list[i].tr.remove();
      }
      render(day);
    }
  });
  form.addEventListener("input", (event) => {
    const table = event.target.closest("table.tariffs");
    if (table) onChange(table.dataset.day, event);
  });

  // Weekend "same as weekdays".
  const same = document.getElementById("weekend-same");
  const wrap = document.getElementById("weekend-wrap");
  function toggleWeekend() {
    wrap.hidden = same.checked;
    wrap.querySelectorAll("input, button").forEach((el) => { el.disabled = same.checked; });
  }
  same.addEventListener("change", toggleWeekend);
  toggleWeekend();

  // TeslaMate geofences.
  const select = document.getElementById("geofence");
  const hint = document.getElementById("geofence-hint");
  fetch(window.GEOFENCES_URL, { credentials: "same-origin" })
    .then((r) => r.json())
    .then((data) => {
      const list = data.geofences || [];
      select.innerHTML = "";
      select.add(new Option(list.length ? "— choose —" : "No geofences in TeslaMate", ""));
      list.forEach((g, i) => select.add(new Option(`${g.name} (${g.radius_m} m)`, String(i))));
      select.disabled = !list.length;
      if (data.error) hint.textContent = "Could not load geofences from TeslaMate.";
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
      select.innerHTML = "<option>Unavailable</option>";
      select.disabled = true;
      hint.textContent = "Could not load geofences from TeslaMate.";
    });

  ["weekday", "weekend"].forEach(render);
})();
