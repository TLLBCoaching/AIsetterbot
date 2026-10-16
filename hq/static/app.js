"use strict";

// ---------- helpers ----------

const $ = (sel) => document.querySelector(sel);
const view = $("#view");
let me = { currency: "AUD", timezone: "Australia/Sydney", connected: {} };

// Builds DOM nodes. Text is always set as text, never as HTML, because inbox content comes from other people.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Line icons (24px grid, stroke-based).
const ICONS = {
  today: ["M12 3v2", "M12 19v2", "M5.6 5.6l1.4 1.4", "M17 17l1.4 1.4", "M3 12h2", "M19 12h2", "M5.6 18.4L7 17", "M17 7l1.4-1.4", "M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8z"],
  calendar: ["M4 6h16v14H4z", "M4 10h16", "M8 3v4", "M16 3v4"],
  todos: ["M4 6h2", "M4 12h2", "M4 18h2", "M9 6h11", "M9 12h11", "M9 18h11"],
  clients: ["M4 5h16v11H8l-4 4z"],
  money: ["M3 7h18v10H3z", "M12 9.5a2.5 2.5 0 1 0 0 5a2.5 2.5 0 1 0 0-5z", "M6 12h.01", "M18 12h.01"],
  ask: ["M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z", "M19 16l.7 1.8L21.5 18.5l-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7z"],
  refresh: ["M20 11a8 8 0 1 0-2.3 5.7", "M20 4v7h-7"],
  star: ["M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"],
  mic: ["M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z", "M5 11a7 7 0 0 0 14 0", "M12 18v3"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  chevron: ["M9 6l6 6-6 6"],
};
function icon(name, cls = "icon") {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  for (const d of ICONS[name] || []) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Please log in"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function toast(msg, undo) {
  const t = $("#toast");
  t.replaceChildren(h("span", {}, msg));
  if (undo) t.append(h("button", { class: "toast-undo", onclick: () => { t.hidden = true; undo(); } }, "Undo"));
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), undo ? 5000 : 2800);
}

const money = (n) => (n === null || n === undefined) ? "—" :
  new Intl.NumberFormat("en-AU", { style: "currency", currency: me.currency, maximumFractionDigits: Math.abs(n) >= 1000 ? 0 : 2 }).format(n);

function todayISO() {
  return new Intl.DateTimeFormat("en-CA", { timeZone: me.timezone }).format(new Date());
}
function addDays(iso, n) {
  const d = new Date(iso + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function nextMonday(iso) {
  const dow = new Date(iso + "T12:00:00Z").getUTCDay(); // 0 = Sunday
  return addDays(iso, ((8 - dow) % 7) || 7);
}
function dayLabel(iso) {
  const t = todayISO();
  if (iso === t) return "Today";
  if (iso === addDays(t, 1)) return "Tomorrow";
  if (iso === addDays(t, -1)) return "Yesterday";
  return new Date(iso + "T12:00:00Z").toLocaleDateString("en-AU", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
}
// Event times come back already in the HQ timezone with an offset; show the wall-clock part as-is.
const clock = (isoDateTime) => {
  const [hh, mm] = isoDateTime.slice(11, 16).split(":").map(Number);
  const ampm = hh >= 12 ? "pm" : "am";
  return `${((hh + 11) % 12) + 1}:${String(mm).padStart(2, "0")}${ampm}`;
};
function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function untilText(ms) {
  const mins = Math.round(ms / 60000);
  if (mins <= 0) return "now";
  if (mins < 60) return `in ${mins} min`;
  const hrs = Math.floor(mins / 60), rest = mins % 60;
  return rest ? `in ${hrs}h ${rest}m` : `in ${hrs}h`;
}
const notConnected = (what, how) => h("div", { class: "empty" }, `${what} isn't connected yet. ${how}`);
const errorBox = (e) => h("div", { class: "error small" }, e.message || String(e));
const skeleton = () => [1, 2, 3].map((n) => h("div", { class: "card skeleton" },
  h("div", { class: "sk-line", style: "width:35%" }), h("div", { class: "sk-line" }), h("div", { class: "sk-line", style: `width:${50 + n * 10}%` })));
const isClientTag = (tags) => (tags || []).some((t) => /client/i.test(t));

// ---------- data cache ----------
// Screens show what they last loaded straight away, then refresh quietly in the background.

const cache = new Map();
function loader(useCache) {
  const state = { usedCache: false };
  state.load = async (path) => {
    if (useCache && cache.has(path)) { state.usedCache = true; return cache.get(path); }
    const data = await api(path);
    cache.set(path, data);
    if (path === "/api/today") setBadge((data.unread.conversations || []).length);
    if (path === "/api/inbox?unread=false") setBadge(data.conversations.filter((c) => c.unread).length);
    return data;
  };
  return state;
}

function setBadge(n) {
  const b = $("#clients-badge");
  b.textContent = n > 9 ? "9+" : String(n);
  b.hidden = !n;
}

// ---------- shared pieces ----------

function eventList(events) {
  if (!events.length) return h("div", { class: "empty" }, "Nothing scheduled.");
  return h("ul", { class: "list" }, events.map((e) => h("li", {},
    h("div", { class: "time" }, e.all_day ? "All day" : clock(e.start)),
    h("div", { class: "grow" },
      h("div", { class: "title" }, e.title),
      (e.location || !e.all_day) ? h("div", { class: "meta" },
        [!e.all_day ? `until ${clock(e.end)}` : "", e.location].filter(Boolean).join(" · ")) : null,
    ),
  )));
}

// Quick date choice: Today / Tomorrow / Next week / Someday, or pick a date.
function datePicker(initial, onChange, { someday = true } = {}) {
  let value = initial || null;
  const iso = todayISO();
  const options = [["Today", iso], ["Tomorrow", addDays(iso, 1)], ["Next week", nextMonday(iso)]];
  if (someday) options.push(["Someday", null]);
  const native = h("input", { type: "date", class: "visually-hidden", tabindex: "-1", "aria-hidden": "true" });
  const wrap = h("div", { class: "chips", role: "group", "aria-label": "Due date" });
  function paint() {
    const picked = value && !options.some(([, v]) => v === value);
    wrap.replaceChildren(
      ...options.map(([label, v]) => h("button", {
        type: "button", class: "chip" + (value === v ? " on" : ""), "aria-pressed": value === v ? "true" : "false",
        onclick: () => { value = v; paint(); onChange && onChange(value); },
      }, label)),
      h("button", {
        type: "button", class: "chip" + (picked ? " on" : ""),
        onclick: () => { native.value = value || iso; native.showPicker ? native.showPicker() : native.click(); },
      }, picked ? dayLabel(value) : "Pick…"),
      native,
    );
  }
  native.addEventListener("change", () => { if (native.value) { value = native.value; paint(); onChange && onChange(value); } });
  paint();
  return { el: wrap, get: () => value };
}

const ASANA = (t) => t.url && t.url.startsWith("https://app.asana.com/");

async function patchTodo(t, body, after) {
  await api(`/api/todos/${t.id}`, { method: "PATCH", body });
  after();
}

async function completeTodo(t, after) {
  await patchTodo(t, { done: true }, after);
  toast("Done ✓", () => patchTodo(t, { done: false }, after));
}

// A to-do row: tap the circle or swipe right to complete, tap the text to open the task.
function todoItem(t, after) {
  const iso = todayISO();
  let due = null;
  if (t.due) {
    const cls = t.due < iso ? "pill bad" : t.due === iso ? "pill warn" : "pill";
    due = h("span", { class: cls }, t.due < iso ? `Overdue · ${dayLabel(t.due)}` : dayLabel(t.due));
  }
  const li = h("li", { class: "todo" });
  const fg = h("div", { class: "swipe-fg" },
    h("button", {
      class: "check" + (t.done ? " done" : ""), "aria-label": t.done ? "Mark not done" : "Mark done",
      onclick: () => (t.done ? patchTodo(t, { done: false }, after) : completeTodo(t, after)),
    }, t.done ? icon("check", "icon sm") : null),
    h("button", { class: "grow todo-body", onclick: () => go("todos", t.id) },
      h("span", { class: "title" + (t.done ? " done-text" : "") }, t.priority ? icon("star", "icon sm star") : null, t.title),
      h("span", { class: "meta" }, due, due ? " " : "", t.area, t.notes ? ` · ${t.notes.split("\n")[0]}` : ""),
    ),
    icon("chevron", "icon sm muted row-chevron"),
  );
  li.append(h("div", { class: "swipe" }, h("div", { class: "swipe-bg" }, icon("check", "icon sm"), " Done"), fg));
  if (!t.done) enableSwipe(fg, () => completeTodo(t, after));
  return li;
}

function enableSwipe(el, onDone) {
  let x0 = null, y0 = 0, dx = 0, dragging = false;
  el.addEventListener("pointerdown", (e) => { if (e.pointerType === "mouse") return; x0 = e.clientX; y0 = e.clientY; dx = 0; dragging = false; });
  el.addEventListener("pointermove", (e) => {
    if (x0 === null) return;
    dx = e.clientX - x0;
    const dy = e.clientY - y0;
    if (!dragging && Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy) * 1.5) { dragging = true; el.setPointerCapture(e.pointerId); }
    if (dragging) el.style.transform = `translateX(${Math.max(0, Math.min(dx, 140))}px)`;
  });
  const end = () => {
    if (x0 === null) return;
    x0 = null;
    el.style.transition = "transform .18s ease";
    el.style.transform = dragging && dx > 90 ? "translateX(100%)" : "";
    setTimeout(() => (el.style.transition = ""), 200);
    if (dragging && dx > 90) setTimeout(onDone, 160);
    if (dragging) el.addEventListener("click", (ev) => { ev.stopPropagation(); ev.preventDefault(); }, { capture: true, once: true });
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

function quickAdd(after) {
  const input = h("input", { type: "text", placeholder: "Add a to-do…", "aria-label": "New to-do", enterkeyhint: "done" });
  const due = datePicker(todayISO());
  return h("form", {
    class: "stack quick-add",
    onsubmit: async (ev) => {
      ev.preventDefault();
      if (!input.value.trim()) return;
      await api("/api/todos", { method: "POST", body: { title: input.value.trim(), due: due.get(), area: "business" } });
      input.value = "";
      toast("Added");
      after();
    },
  }, h("div", { class: "row" }, input, h("button", { class: "secondary", type: "submit", style: "flex:0 0 auto" }, "Add")), due.el);
}

function conversationItem(c) {
  return h("li", { class: "clickable", onclick: () => go("clients", c.id) },
    h("div", { class: "grow" },
      h("div", { class: "title" }, c.name, " ",
        h("span", { class: "pill subtle" }, isClientTag(c.tags) ? "Client" : "Lead"), " ",
        c.unread ? h("span", { class: "pill" }, `${c.unread} new`) : null),
      h("div", { class: "meta" }, c.last_direction === "outbound" ? "You: " : "", c.last_message || "(no text)"),
    ),
    h("div", { class: "meta right" }, c.channel, h("br"), ago(c.last_message_at)),
  );
}

// ---------- the full task screen ----------

// Lists elsewhere are out of date once a task changes.
function forgetTodos() {
  for (const key of [...cache.keys()]) if (key.startsWith("/api/todos") || key === "/api/today") cache.delete(key);
}

function autoGrow(el) {
  const fit = () => { el.style.height = "auto"; el.style.height = el.scrollHeight + 2 + "px"; };
  el.addEventListener("input", fit);
  setTimeout(fit, 0);
  return el;
}

async function taskView(id) {
  const t = await api(`/api/todos/${encodeURIComponent(id)}`); // always fresh: you may edit straight away
  $("#view-title").textContent = "Task";
  const base = `/api/todos/${encodeURIComponent(id)}`;
  const status = h("span", { class: "save-status muted small", "aria-live": "polite" });
  let pending = 0;

  async function save(body, quiet) {
    pending++;
    status.textContent = "Saving…";
    try {
      const updated = await api(base, { method: "PATCH", body });
      Object.assign(t, updated);
      forgetTodos();
      if (--pending === 0) status.textContent = "Saved ✓";
      if (!quiet) toast("Saved");
    } catch (e) {
      pending--;
      status.textContent = "Not saved";
      toast(e.message);
    }
  }

  // Title and done
  const check = h("button", { class: "check big" + (t.done ? " done" : ""), "aria-label": t.done ? "Mark not done" : "Mark done" }, t.done ? icon("check", "icon sm") : null);
  const title = autoGrow(h("textarea", { class: "task-title", rows: 1, "aria-label": "Task name", value: t.title }));
  title.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); title.blur(); } });
  title.addEventListener("blur", () => {
    const v = title.value.trim();
    if (!v) { title.value = t.title; return; }
    if (v !== t.title) save({ title: v }, true);
  });
  check.addEventListener("click", async () => {
    const done = !t.done;
    await save({ done }, true);
    check.classList.toggle("done", t.done);
    check.replaceChildren(t.done ? icon("check", "icon sm") : "");
    title.classList.toggle("done-text", t.done);
    if (t.done) toast("Done ✓", async () => { await save({ done: false }, true); check.classList.remove("done"); check.replaceChildren(); title.classList.remove("done-text"); });
  });
  if (t.done) title.classList.add("done-text");

  // Details
  const due = datePicker(t.due, (v) => save({ due: v || "" }, true));
  const star = h("button", { type: "button", class: "chip" + (t.priority ? " on" : "") }, icon("star", "icon sm"), " Important");
  star.addEventListener("click", () => { const on = !t.priority; star.classList.toggle("on", on); save({ priority: on ? 1 : 0 }, true); });
  const project = h("select", { "aria-label": "Project" },
    me.connected.asana ? h("option", { value: "" }, "No project (My Tasks only)") : null,
    t.projects.map((p) => { const o = h("option", { value: p.id }, p.name); if (p.id === t.project_id) o.selected = true; return o; }));
  project.addEventListener("change", () => save({ project: project.value }, true));

  // Notes save as you type, a moment after you stop.
  const notes = autoGrow(h("textarea", { class: "task-notes", rows: 3, placeholder: "Add notes, links, details…", value: t.notes || "" }));
  let notesTimer = null;
  const saveNotes = () => { clearTimeout(notesTimer); if (notes.value !== (t.notes || "")) save({ notes: notes.value }, true); };
  notes.addEventListener("input", () => { clearTimeout(notesTimer); status.textContent = "Editing…"; notesTimer = setTimeout(saveNotes, 900); });
  notes.addEventListener("blur", saveNotes);

  // Subtasks
  const subList = h("ul", { class: "subtasks" });
  const doneCount = h("span", { class: "count" });
  const paintCount = () => {
    const n = subList.querySelectorAll(".subtask").length, d = subList.querySelectorAll(".subtask .check.done").length;
    doneCount.textContent = n ? ` ${d}/${n}` : "";
  };
  function subRow(sub) {
    const box = h("button", { class: "check small-check" + (sub.done ? " done" : ""), "aria-label": sub.done ? "Mark not done" : "Mark done" }, sub.done ? icon("check", "icon sm") : null);
    const name = h("input", { type: "text", value: sub.title, "aria-label": "Subtask", class: "subtask-title" + (sub.done ? " done-text" : "") });
    const row = h("li", { class: "subtask" }, box, name,
      h("button", { class: "icon-btn small", "aria-label": "Delete subtask", onclick: async () => {
        row.remove(); paintCount();
        try { await api(`${base}/subtasks/${sub.id}`, { method: "DELETE" }); } catch (e) { toast(e.message); }
      } }, "×"));
    box.addEventListener("click", async () => {
      sub.done = !sub.done;
      box.classList.toggle("done", sub.done); box.replaceChildren(sub.done ? icon("check", "icon sm") : "");
      name.classList.toggle("done-text", sub.done); paintCount();
      try { await api(`${base}/subtasks/${sub.id}`, { method: "PATCH", body: { done: sub.done } }); } catch (e) { toast(e.message); }
    });
    name.addEventListener("keydown", (e) => { if (e.key === "Enter") name.blur(); });
    name.addEventListener("blur", async () => {
      const v = name.value.trim();
      if (!v) { name.value = sub.title; return; }
      if (v === sub.title) return;
      sub.title = v;
      try { await api(`${base}/subtasks/${sub.id}`, { method: "PATCH", body: { title: v } }); status.textContent = "Saved ✓"; } catch (e) { toast(e.message); }
    });
    return row;
  }
  t.subtasks.forEach((sub) => subList.append(subRow(sub)));
  paintCount();
  const newSub = h("input", { type: "text", placeholder: "Add a subtask…", enterkeyhint: "done", "aria-label": "New subtask" });
  const addSub = h("form", { class: "row add-sub", onsubmit: async (e) => {
    e.preventDefault();
    const v = newSub.value.trim();
    if (!v) return;
    newSub.value = "";
    try { subList.append(subRow(await api(`${base}/subtasks`, { method: "POST", body: { title: v } }))); paintCount(); forgetTodos(); }
    catch (err) { toast(err.message); newSub.value = v; }
    newSub.focus();
  } }, newSub, h("button", { class: "secondary", type: "submit", style: "flex:0 0 auto" }, "Add"));

  // Comments (Asana only)
  let comments = null;
  if (t.supports_comments) {
    const list = h("ul", { class: "comments" }, t.comments.map(commentRow));
    const box = autoGrow(h("textarea", { rows: 2, placeholder: "Write a comment…", "aria-label": "Comment" }));
    const send = h("button", { class: "secondary", type: "submit", style: "flex:0 0 auto" }, "Comment");
    comments = h("div", { class: "card" }, h("h2", {}, "Comments ", h("span", { class: "count" }, t.comments.length || "")),
      t.comments.length ? null : h("div", { class: "empty small no-comments" }, "No comments yet."),
      list,
      h("form", { class: "row comment-form", onsubmit: async (e) => {
        e.preventDefault();
        const v = box.value.trim();
        if (!v) return;
        send.disabled = true;
        try { list.append(commentRow(await api(`${base}/comments`, { method: "POST", body: { text: v } }))); box.value = ""; comments.querySelector(".no-comments")?.remove(); }
        catch (err) { toast(err.message); }
        send.disabled = false;
      } }, box, send));
  }

  return [
    h("div", { class: "row task-head" },
      h("button", { class: "link-btn", style: "flex:0 0 auto", onclick: () => history.length > 1 ? history.back() : go("todos") }, "← To-dos"),
      h("span", { class: "row", style: "flex:0 0 auto;gap:12px;align-items:center" }, status,
        ASANA(t) ? h("a", { href: t.url, target: "_blank", rel: "noopener", class: "link-btn small" }, "Asana ↗") : null)),
    h("div", { class: "card task-card" },
      h("div", { class: "task-top" }, check, title),
      h("div", { class: "field" }, h("label", {}, "Due"), due.el),
      h("div", { class: "field" }, h("label", {}, "Project"), project),
      h("div", { class: "field" }, h("label", {}, "Priority"), h("div", { class: "chips" }, star)),
      h("div", { class: "field" }, h("label", {}, "Notes"), notes),
    ),
    h("div", { class: "card" }, h("h2", {}, "Subtasks", doneCount), subList, addSub),
    comments,
    h("button", { class: "ghost danger delete-task", onclick: async () => {
      if (!confirm(`Delete "${t.title}"${ASANA(t) ? " from Asana" : ""}?`)) return;
      try { await api(base, { method: "DELETE" }); forgetTodos(); toast("Deleted"); go("todos"); } catch (e) { toast(e.message); }
    } }, "Delete task"),
  ];
}

function commentRow(c) {
  return h("li", {}, h("div", { class: "meta" }, c.author || "You", c.at ? ` · ${ago(c.at)}` : ""), h("div", { class: "comment-text" }, c.text));
}

const statChip = (value, label, tab) => h("button", { class: "stat-chip", onclick: () => go(tab) }, h("strong", {}, value), h("span", {}, label));

// ---------- views ----------

const views = {};

function upNext(events) {
  const now = Date.now();
  const timed = events.filter((e) => !e.all_day && Date.parse(e.end) > now).sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
  const e = timed[0];
  if (!e) return h("div", { class: "hero-event" }, h("div", { class: "eyebrow" }, "Up next"), h("div", { class: "hero-title" }, "Nothing else today"), h("div", { class: "meta" }, "Your calendar is clear."));
  const live = Date.parse(e.start) <= now;
  const when = h("span", { class: "countdown", "data-start": e.start, "data-end": e.end });
  updateCountdown(when);
  return h("div", { class: "hero-event" },
    h("div", { class: "eyebrow" + (live ? " live" : "") }, live ? "Happening now" : "Up next"),
    h("div", { class: "hero-title" }, e.title),
    h("div", { class: "meta" }, `${clock(e.start)}–${clock(e.end)}`, " · ", when, e.location ? ` · ${e.location}` : ""),
  );
}

function updateCountdown(el) {
  const start = Date.parse(el.dataset.start), end = Date.parse(el.dataset.end), now = Date.now();
  el.textContent = start <= now ? `until ${clock(el.dataset.end)} (${untilText(end - now).replace("in ", "")} left)` : untilText(start - now);
}
setInterval(() => document.querySelectorAll(".countdown").forEach(updateCountdown), 30000);

views.today = async (_arg, { load }) => {
  const d = await load("/api/today");
  const iso = d.date;
  // The feed covers today and tomorrow; anything that starts on or before today is on today.
  const todayEvents = d.events.events.filter((e) => e.start.slice(0, 10) <= iso);
  const tomorrowEvents = d.events.events.filter((e) => e.start.slice(0, 10) === addDays(iso, 1));
  const unread = d.unread.conversations || [];
  const hour = Number(new Intl.DateTimeFormat("en-AU", { hour: "numeric", hourCycle: "h23", timeZone: me.timezone }).format(new Date()));
  const greeting = hour < 12 ? "Morning" : hour < 17 ? "Afternoon" : "Evening";
  const top = [...d.todos_due, ...d.todos_important].slice(0, 3);
  const more = d.todos_due.length + d.todos_important.length - top.length;
  const refresh = () => render({ fresh: true });

  return [
    h("p", { class: "greeting" }, `${greeting} Alistair`,
      h("span", {}, new Date().toLocaleDateString("en-AU", { weekday: "long", day: "numeric", month: "long", timeZone: me.timezone }))),
    h("div", { class: "card hero" },
      d.events.configured ? upNext(todayEvents) : notConnected("Your calendar", "Add HQ_CALENDAR_ICS_URLS."),
      h("div", { class: "hero-todos" },
        h("div", { class: "eyebrow" }, "Top priorities"),
        top.length ? h("ul", { class: "list" }, top.map((t) => todoItem(t, refresh))) : h("div", { class: "empty" }, "Nothing urgent. Nice."),
        more > 0 ? h("button", { class: "link-btn small", onclick: () => go("todos") }, `See ${more} more →`) : null,
        quickAdd(refresh),
      ),
    ),
    h("div", { class: "stat-row" },
      d.events.configured ? statChip(todayEvents.filter((e) => !e.all_day).length, "events", "calendar") : null,
      statChip(d.todos_due.length, "due", "todos"),
      d.unread.configured ? statChip(unread.length, "unread", "clients") : null,
      d.money.configured && !d.money.error ? statChip(money(d.money.this_week), "this week", "money") : null,
    ),
    d.drafts.length ? h("div", { class: "card" }, h("h2", {}, "Reply drafts waiting for you"),
      h("ul", { class: "list" }, d.drafts.map((dr) => h("li", { class: "clickable", onclick: () => go("clients", dr.conversation_id) },
        h("div", { class: "grow" }, h("div", { class: "title" }, dr.contact_name), h("div", { class: "meta clamp" }, dr.text)), icon("chevron", "icon sm muted"))))) : null,
    h("div", { class: "card" }, h("h2", {}, "Unread messages"),
      !d.unread.configured ? notConnected("Your inbox", "Add GHL_API_TOKEN and GHL_LOCATION_ID.")
        : d.unread.error ? h("div", { class: "error small" }, d.unread.error)
        : unread.length ? h("ul", { class: "list" }, unread.map(conversationItem)) : h("div", { class: "empty" }, "Inbox zero 🙌"),
    ),
    d.events.configured ? h("div", { class: "card" }, h("h2", {}, "Rest of today"),
      eventList(todayEvents),
      d.events.errors ? h("div", { class: "error small" }, "One of your calendars didn't load.") : null,
      tomorrowEvents.length ? h("details", { style: "margin-top:8px" }, h("summary", {}, `Tomorrow · ${tomorrowEvents.length} event(s)`), eventList(tomorrowEvents)) : null,
    ) : null,
    d.money.failed && d.money.failed.length ? h("div", { class: "card" }, h("h2", {}, "Failed payments"),
      h("ul", { class: "list" }, d.money.failed.slice(0, 5).map((f) => h("li", {},
        h("div", { class: "grow" }, h("div", { class: "title" }, f.customer || "Unknown customer"), h("div", { class: "meta" }, `${dayLabel(f.date)} · ${f.reason}`)),
        h("div", { class: "amount out" }, money(f.amount)))))) : null,
  ];
};

const DURATIONS = [[15, "15 min"], [30, "30 min"], [45, "45 min"], [60, "1 hour"], [90, "1.5 hours"], [120, "2 hours"], [180, "3 hours"]];
const minutesBetween = (a, b) => Math.round((Date.parse(b) - Date.parse(a)) / 60000);

// Add or edit a Google Calendar event. `ev` is null for a new event.
function eventEditor(ev, after, onCancel) {
  const title = h("input", { type: "text", value: ev ? ev.title : "", placeholder: "What's on?", "aria-label": "Event title", required: true });
  const day = datePicker(ev ? ev.start.slice(0, 10) : todayISO(), null, { someday: false });
  let allDay = ev ? ev.all_day : false;
  const time = h("input", { type: "time", value: ev && !ev.all_day ? ev.start.slice(11, 16) : "09:00", "aria-label": "Start time", step: "300" });
  const currentLen = ev && !ev.all_day ? minutesBetween(ev.start, ev.end) : 60;
  const duration = h("select", { "aria-label": "Length" },
    [...DURATIONS.some(([m]) => m === currentLen) ? [] : [[currentLen, `${currentLen} min`]], ...DURATIONS]
      .map(([m, label]) => { const o = h("option", { value: m }, label); if (m === currentLen) o.selected = true; return o; }));
  const location = h("input", { type: "text", value: ev ? ev.location : "", placeholder: "Location (optional)" });
  const allDayBtn = h("button", { type: "button", class: "chip" + (allDay ? " on" : ""), onclick: () => { allDay = !allDay; paintTime(); } }, "All day");
  const timeRow = h("div", { class: "row" }, time, duration);
  const paintTime = () => { allDayBtn.classList.toggle("on", allDay); timeRow.hidden = allDay; };
  paintTime();
  const save = h("button", { type: "submit", class: "primary", style: "flex:0 0 auto" }, ev ? "Save" : "Add to calendar");

  return h("form", {
    class: "editor event-editor stack",
    onsubmit: async (e) => {
      e.preventDefault();
      if (!title.value.trim()) return;
      save.disabled = true;
      const body = { title: title.value.trim(), date: day.get(), start_time: allDay ? "" : time.value, duration_minutes: Number(duration.value), location: location.value };
      try {
        if (ev) await api(`/api/calendar/events/${encodeURIComponent(ev.id)}?calendar_id=${encodeURIComponent(ev.calendar)}`, { method: "PATCH", body });
        else await api("/api/calendar/events", { method: "POST", body });
        toast(ev ? "Event updated" : "Added to your calendar");
        after();
      } catch (err) { toast(err.message); save.disabled = false; }
    },
  },
    title, day.el, h("div", { class: "chips" }, allDayBtn), timeRow, location,
    h("div", { class: "row", style: "align-items:center" },
      ev && ev.url ? h("a", { href: ev.url, target: "_blank", rel: "noopener", class: "link-btn small", style: "flex:0 0 auto" }, "Open in Google ↗") : null,
      h("span", { style: "flex:1" }),
      ev ? h("button", {
        type: "button", class: "ghost danger", style: "flex:0 0 auto",
        onclick: async () => {
          if (!confirm(`Delete "${ev.title}" from your Google Calendar?`)) return;
          try {
            await api(`/api/calendar/events/${encodeURIComponent(ev.id)}?calendar_id=${encodeURIComponent(ev.calendar)}`, { method: "DELETE" });
            toast("Deleted");
            after();
          } catch (err) { toast(err.message); }
        },
      }, "Delete") : h("button", { type: "button", class: "ghost", style: "flex:0 0 auto", onclick: onCancel }, "Cancel"),
      save),
  );
}

function editableEventList(events, after) {
  if (!events.length) return h("div", { class: "empty" }, "Nothing scheduled.");
  return h("ul", { class: "list" }, events.map((e) => {
    const li = h("li", { class: "event" });
    li.append(h("button", {
      class: "event-row", "aria-expanded": "false",
      onclick: () => {
        const open = li.querySelector(".editor");
        if (open) { open.remove(); return; }
        li.append(eventEditor(e, after));
      },
    },
      h("span", { class: "time" }, e.all_day ? "All day" : clock(e.start)),
      h("span", { class: "grow" },
        h("span", { class: "title" }, e.title),
        (e.location || !e.all_day) ? h("span", { class: "meta" }, [!e.all_day ? `until ${clock(e.end)}` : "", e.location].filter(Boolean).join(" · ")) : null),
    ));
    return li;
  }));
}

function connectGoogleCard(reconnect) {
  const g = me.connected.google || {};
  if (!g.configured) {
    return h("div", { class: "card" }, h("h2", {}, "Google Calendar"),
      h("p", { class: "muted small", style: "margin:0" }, "Not set up yet. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in Railway, then come back here to connect."));
  }
  return h("div", { class: "card connect" },
    h("h2", {}, reconnect ? "Reconnect Google Calendar" : "Google Calendar"),
    h("p", { class: "small", style: "margin:0 0 10px" }, reconnect
      ? "Google stopped accepting HQ's access, so your events can't load. Connect again to fix it."
      : "Connect your Google account so HQ can show your events and add or move them for you."),
    h("a", { class: "primary button-link", href: "/api/google/connect" }, "Connect Google Calendar"),
    h("p", { class: "muted small", style: "margin:10px 0 0" }, "Do this on your computer the first time. If Google warns the app is unverified, choose Advanced → Go to HQ."),
  );
}

let calStart = null;
let addingEvent = false;
views.calendar = async (_arg, { load }) => {
  calStart = calStart || todayISO();
  const days = 7;
  const d = await load(`/api/calendar?start=${calStart}&days=${days}`);
  const g = me.connected.google || {};
  if (!d.configured) return connectGoogleCard(false);
  const byDay = {};
  for (let i = 0; i < days; i++) byDay[addDays(calStart, i)] = [];
  for (const e of d.events) {
    // Multi-day and all-day events show on every day they cover.
    for (let i = 0; i < days; i++) {
      const day = addDays(calStart, i);
      const endDay = e.end.slice(0, 10);
      if (e.start.slice(0, 10) <= day && (endDay > day || (endDay === day && !e.all_day) || e.start.slice(0, 10) === day)) byDay[day].push(e);
    }
  }
  const refresh = () => { addingEvent = false; render({ fresh: true }); };
  const list = (evs) => (d.editable ? editableEventList(evs, refresh) : eventList(evs));
  return [
    d.needs_reconnect ? connectGoogleCard(true) : null,
    d.editable ? (addingEvent
      ? h("div", { class: "card" }, h("h2", {}, "New event"), eventEditor(null, refresh, () => { addingEvent = false; render(); }))
      : h("button", { class: "primary add-event", onclick: () => { addingEvent = true; render(); } }, "+ Add event")) : null,
    h("div", { class: "segmented" },
      h("button", { onclick: () => { calStart = addDays(calStart, -7); render(); } }, "← Prev"),
      h("button", { class: calStart === todayISO() ? "active" : "", onclick: () => { calStart = todayISO(); render(); } }, "This week"),
      h("button", { onclick: () => { calStart = addDays(calStart, 7); render(); } }, "Next →"),
    ),
    d.errors && !d.needs_reconnect ? h("div", { class: "error small" }, "Your calendar didn't fully load. Pull down to try again.") : null,
    h("div", { class: "card" }, Object.entries(byDay).map(([day, evs]) => [h("div", { class: "day-head" + (day === todayISO() ? " now" : "") }, dayLabel(day)), list(evs)])),
    d.source === "google"
      ? h("p", { class: "muted small" }, `Google Calendar${g.account ? ` · ${g.account}` : ""}. Tap an event to move or delete it. `,
        h("button", { class: "link-btn small", onclick: async () => {
          if (!confirm("Disconnect Google Calendar from HQ?")) return;
          await api("/api/google/disconnect", { method: "POST" });
          me.connected.google = { ...g, connected: false, account: "" };
          me.connected.calendar = false;
          cache.clear();
          render({ fresh: true });
        } }, "Disconnect"))
      : [g.configured && !g.connected ? connectGoogleCard(false) : null, h("p", { class: "muted small" }, "Read-only calendar link.")],
  ];
};

let todoFilter = "open";
views.todos = async (taskId, { load }) => {
  if (taskId) return taskView(taskId);
  const todos = await load(`/api/todos?include_done=${todoFilter === "done"}`);
  const iso = todayISO();
  const shown = todoFilter === "done" ? todos.filter((t) => t.done) : todos;
  const groups = todoFilter === "done" ? [["Done", shown]] : [
    ["Overdue", shown.filter((t) => t.due && t.due < iso)],
    ["Today", shown.filter((t) => t.due === iso)],
    ["Upcoming", shown.filter((t) => t.due && t.due > iso)],
    ["Someday", shown.filter((t) => !t.due)],
  ];
  const refresh = () => render({ fresh: true });
  return [
    h("div", { class: "card" }, quickAdd(refresh)),
    h("div", { class: "segmented" },
      ["open", "done"].map((f) => h("button", { class: todoFilter === f ? "active" : "", onclick: () => { todoFilter = f; render(); } }, f === "open" ? "Open" : "Done"))),
    h("p", { class: "muted small hint" },
      todoFilter === "done" ? (me.connected.asana ? "Completed in Asana in the last 14 days." : "")
        : `Swipe right or tap the circle to complete. Tap a to-do to open it.${me.connected.asana ? " Synced with your Asana My Tasks." : ""}`),
    groups.filter(([, items]) => items.length).map(([name, items]) =>
      h("div", { class: "card" }, h("h2", {}, name, " ", h("span", { class: "count" }, items.length)), h("ul", { class: "list" }, items.map((t) => todoItem(t, refresh))))),
    shown.length ? null : h("div", { class: "empty" }, todoFilter === "done" ? "Nothing completed yet." : "All clear."),
  ];
};

let inboxFilter = "all";
views.clients = async (conversationId, { load }) => {
  if (conversationId) return threadView(conversationId, load);
  if (!me.connected.inbox) return h("div", { class: "card" }, notConnected("Your GoHighLevel inbox", "Add GHL_API_TOKEN and GHL_LOCATION_ID."));
  const d = await load(`/api/inbox?unread=${inboxFilter === "unread"}`);
  return [
    h("div", { class: "segmented" },
      ["all", "unread"].map((f) => h("button", { class: inboxFilter === f ? "active" : "", onclick: () => { inboxFilter = f; render(); } }, f === "all" ? "All" : "Unread"))),
    h("div", { class: "card" }, d.conversations.length ? h("ul", { class: "list" }, d.conversations.map(conversationItem)) : h("div", { class: "empty" }, "No conversations.")),
  ];
};

const QUICK_PROMPTS = [
  ["Check in", "Friendly check-in on how they're going"],
  ["Encourage", "Encourage them and keep them motivated"],
  ["Reschedule", "Agree to reschedule and offer a time"],
  ["Book a call", "Suggest a quick call and ask what time suits"],
];

async function threadView(id, load) {
  const c = await load(`/api/inbox/${encodeURIComponent(id)}`);
  $("#view-title").textContent = c.name;
  const box = h("textarea", { placeholder: c.send_type ? `Reply on ${c.channel}…` : "", value: c.draft ? c.draft.text : "", rows: 3 });
  const instructions = h("input", { type: "text", placeholder: "Or tell Claude what to say…" });
  let draftId = c.draft ? c.draft.id : null;

  async function draft(text) {
    if (text !== undefined) instructions.value = text;
    draftBtn.disabled = true; draftBtn.textContent = "Drafting…";
    box.classList.add("pulse");
    try {
      const dr = await api(`/api/inbox/${encodeURIComponent(id)}/draft`, { method: "POST", body: { instructions: instructions.value } });
      box.value = dr.text; draftId = dr.id;
    } catch (e) { toast(e.message); }
    box.classList.remove("pulse");
    draftBtn.disabled = false; draftBtn.textContent = "✦ Draft";
  }
  const draftBtn = h("button", { class: "secondary", type: "button", style: "flex:0 0 auto", onclick: () => draft() }, "✦ Draft");

  const sendBtn = h("button", {
    class: "primary send", type: "button",
    onclick: async () => {
      const text = box.value.trim();
      if (!text) { toast("Write or draft a reply first"); return; }
      if (!confirm(`Send this to ${c.name} on ${c.channel}?\n\n${text}`)) return;
      sendBtn.disabled = true;
      try {
        await api(`/api/inbox/${encodeURIComponent(id)}/send`, { method: "POST", body: { text, draft_id: draftId } });
        toast("Sent");
        render({ fresh: true });
      } catch (e) { toast(e.message); sendBtn.disabled = false; }
    },
  }, `Send on ${c.channel}`);

  const thread = h("div", { class: "thread" }, c.messages.length ? c.messages.map((m) =>
    h("div", { class: `bubble ${m.from}` }, m.text, h("span", { class: "at" }, ago(m.at)))) : h("div", { class: "empty" }, "No messages."));
  setTimeout(() => thread.lastElementChild && thread.lastElementChild.scrollIntoView({ block: "nearest" }), 0);

  return [
    h("div", { class: "row thread-head" },
      h("button", { class: "link-btn", style: "flex:0 0 auto", onclick: () => go("clients") }, "← All"),
      h("span", { class: "muted small", style: "text-align:right" },
        h("span", { class: "pill subtle" }, isClientTag(c.tags) ? "Client" : "Lead"), " ", c.channel)),
    thread,
    c.send_type ? h("div", { class: "card stack composer" },
      h("div", { class: "chips" }, QUICK_PROMPTS.map(([label, text]) => h("button", { type: "button", class: "chip", onclick: () => draft(text) }, label))),
      h("div", { class: "row" }, instructions, draftBtn),
      box,
      sendBtn,
      h("div", { class: "row", style: "align-items:center" },
        h("span", { class: "muted small" }, "Nothing is sent until you press Send and confirm."),
        draftId ? h("button", { class: "link-btn small", style: "flex:0 0 auto", onclick: async () => { await api(`/api/drafts/${draftId}/discard`, { method: "POST" }); render({ fresh: true }); } }, "Discard draft") : null),
    ) : h("div", { class: "card muted small" }, `Replying on ${c.channel} isn't supported here yet. Reply in GoHighLevel.`),
  ];
}

function weeklyChart(weeks) {
  const max = Math.max(1, ...weeks.map((w) => w.amount));
  const readout = h("div", { class: "chart-readout" });
  const bars = h("div", { class: "bars", role: "group", "aria-label": "Cash collected per week" });
  const select = (i) => {
    const w = weeks[i];
    readout.replaceChildren(h("strong", {}, money(w.amount)), h("span", {}, ` week of ${dayLabel(w.week_of)}${i === weeks.length - 1 ? " (so far)" : ""}`));
    bars.querySelectorAll(".bar").forEach((b, j) => b.classList.toggle("sel", j === i));
  };
  weeks.forEach((w, i) => bars.append(h("button", {
    class: "bar" + (i === weeks.length - 1 ? " partial" : ""), style: `height:${Math.max(2, (w.amount / max) * 100)}%`,
    "aria-label": `Week of ${w.week_of}: ${money(w.amount)}`, onclick: () => select(i),
  })));
  select(weeks.length - 1);
  return h("div", { class: "chart" },
    readout,
    h("div", { class: "chart-body" },
      h("div", { class: "axis" }, h("span", {}, money(max)), h("span", {}, money(max / 2)), h("span", {}, "$0")),
      h("div", { class: "chart-plot" }, h("div", { class: "gridlines" }, h("i"), h("i"), h("i")), bars)),
    h("div", { class: "bar-labels" }, weeks.map((w, i) => h("span", {}, (weeks.length - 1 - i) % 2 ? "" : `${Number(w.week_of.slice(8, 10))}/${Number(w.week_of.slice(5, 7))}`))),
    h("div", { class: "muted small", style: "margin-top:6px" }, "Tap a bar for its total. Net of refunds; the latest week is partial."),
  );
}

function versus(now, before) {
  if (!before) return "";
  const pct = Math.round(((now - before) / before) * 100);
  return `${pct >= 0 ? "▲" : "▼"} ${Math.abs(pct)}%`;
}

views.money = async (_arg, { load }) => {
  const d = await load("/api/finance");
  const s = d.stripe;
  const log = d.log;
  const weeks = (s.weekly || []).slice(-12);
  const amount = h("input", { type: "number", step: "0.01", placeholder: "Amount", required: true, inputmode: "decimal" });
  const kind = h("select", { "aria-label": "In or out" }, h("option", { value: "out" }, "Money out"), h("option", { value: "in" }, "Money in"));
  const category = h("input", { type: "text", placeholder: "Category (e.g. software)", list: "cats" });
  const note = h("input", { type: "text", placeholder: "Note" });
  const cats = h("datalist", { id: "cats" }, ["software", "ads", "contractors", "food", "rent", "transport", "health", "fun", "income"].map((c) => h("option", { value: c })));
  const mtd = s.last_month_to_date;

  return [
    h("div", { class: "card" }, h("h2", {}, "Business · Stripe"),
      !s.configured ? notConnected("Stripe", "Add a read-only STRIPE_API_KEY.")
        : s.error ? h("div", { class: "error small" }, s.error)
        : [
          h("div", { class: "stats" },
            h("div", { class: "stat" }, h("div", { class: "label" }, "This month so far"), h("div", { class: "value" }, money(s.this_month)),
              h("div", { class: "sub" }, mtd !== undefined && mtd !== null ? [`${money(mtd)} by now last month `, h("span", { class: s.this_month >= mtd ? "up" : "down" }, versus(s.this_month, mtd))] : `last month ${money(s.last_month)}`)),
            h("div", { class: "stat" }, h("div", { class: "label" }, "This week"), h("div", { class: "value" }, money(s.this_week)), h("div", { class: "sub" }, `last month in full ${money(s.last_month)}`)),
            h("div", { class: "stat" }, h("div", { class: "label" }, "Balance"), h("div", { class: "value" }, money(s.balance_available)), h("div", { class: "sub" }, `${money(s.balance_pending)} pending`)),
            h("div", { class: "stat" }, h("div", { class: "label" }, "Active subscriptions"), h("div", { class: "value" }, s.active_subscriptions), h("div", { class: "sub" }, "in Stripe")),
          ),
          weeks.length ? weeklyChart(weeks) : null,
          s.failed && s.failed.length ? [h("h2", { style: "margin-top:14px" }, "Failed payments"),
            h("ul", { class: "list" }, s.failed.map((f) => h("li", {},
              h("div", { class: "grow" }, h("div", { class: "title" }, f.customer || "Unknown"), h("div", { class: "meta" }, `${dayLabel(f.date)} · ${f.reason}`)),
              h("div", { class: "amount out" }, money(f.amount)))))] : null,
          h("div", { class: "muted small", style: "margin-top:8px" }, `As of ${s.as_of ? s.as_of.slice(11, 16) : ""}, refreshed every 10 minutes.`),
        ],
    ),
    h("div", { class: "card" }, h("h2", {}, "Money log · this month"),
      h("div", { class: "stats three", style: "margin-bottom:10px" },
        h("div", { class: "stat" }, h("div", { class: "label" }, "In"), h("div", { class: "value amount in" }, money(log.month_in))),
        h("div", { class: "stat" }, h("div", { class: "label" }, "Out"), h("div", { class: "value amount out" }, money(Math.abs(log.month_out)))),
        h("div", { class: "stat" }, h("div", { class: "label" }, "Net"), h("div", { class: "value" }, money(log.month_net))),
      ),
      Object.keys(log.by_category).length ? h("ul", { class: "list" }, Object.entries(log.by_category).map(([cat, v]) =>
        h("li", {}, h("div", { class: "grow" }, cat), h("div", { class: "amount " + (v < 0 ? "out" : "in") }, money(v))))) : null,
      h("form", {
        class: "stack", style: "margin-top:12px",
        onsubmit: async (ev) => {
          ev.preventDefault();
          const value = Math.abs(parseFloat(amount.value)) * (kind.value === "out" ? -1 : 1);
          await api("/api/money", { method: "POST", body: { amount: value, category: category.value || "other", note: note.value } });
          toast("Logged");
          render({ fresh: true });
        },
      }, h("div", { class: "row" }, amount, kind), h("div", { class: "row" }, category, note), cats, h("button", { class: "primary", type: "submit" }, "Log it")),
    ),
    log.recent.length ? h("div", { class: "card" }, h("h2", {}, "Recent entries"),
      h("ul", { class: "list" }, log.recent.map((e) => h("li", {},
        h("div", { class: "grow" }, h("div", { class: "title" }, e.category), h("div", { class: "meta" }, `${dayLabel(e.date)}${e.note ? " · " + e.note : ""}`)),
        h("div", { class: "amount " + (e.amount < 0 ? "out" : "in") }, money(e.amount)),
        h("button", { class: "icon-btn small", "aria-label": "Delete", onclick: async () => { if (confirm("Delete this entry?")) { await api(`/api/money/${e.id}`, { method: "DELETE" }); render({ fresh: true }); } } }, "×"))))) : null,
  ];
};

// Reads the assistant's reply as it's written: one JSON event per line.
async function streamChat(message, onEvent) {
  const res = await fetch("/api/chat/stream", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }), credentials: "same-origin",
  });
  if (res.status === 401) { showLogin(); throw new Error("Please log in"); }
  if (!res.ok || !res.body) throw new Error(`Request failed (${res.status})`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}

const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;

views.ask = async (_arg, { load }) => {
  const d = await load("/api/chat");
  const thread = h("div", { class: "chat" }, d.messages.map((m) => h("div", { class: `bubble ${m.role}` }, m.text)));
  const input = h("textarea", { placeholder: "Ask or tell me what to do…", rows: 1 });
  const send = h("button", { class: "primary", type: "submit", style: "flex:0 0 auto" }, "Send");
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.matchMedia("(pointer:fine)").matches) { e.preventDefault(); ask(input.value); } });

  async function ask(text) {
    if (!text.trim() || send.disabled) return;
    thread.querySelector(".suggestions")?.remove();
    thread.append(h("div", { class: "bubble user" }, text));
    const reply = h("div", { class: "bubble assistant" });
    const body = h("span", {});
    const status = h("span", { class: "status" }, h("i", { class: "dots" }), "Thinking…");
    reply.append(body, status);
    thread.append(reply);
    reply.scrollIntoView({ behavior: "smooth", block: "end" });
    input.value = ""; input.style.height = ""; send.disabled = true;
    const used = new Set();
    try {
      await streamChat(text, (ev) => {
        if (ev.type === "text") { body.textContent += ev.delta; status.hidden = true; }
        else if (ev.type === "status") { status.hidden = false; status.replaceChildren(h("i", { class: "dots" }), ev.text); }
        else if (ev.type === "done") (ev.tools || []).forEach((t) => used.add(t));
        reply.scrollIntoView({ block: "end" });
      });
      status.remove();
      if (!body.textContent.trim()) body.textContent = "(no reply)";
      if (used.size) reply.append(h("span", { class: "at" }, `used: ${[...used].join(", ").replaceAll("_", " ")}`));
      cache.delete("/api/chat");
      ["/api/today", "/api/todos?include_done=false", "/api/finance"].forEach((p) => cache.delete(p)); // it may have changed things
    } catch (e) {
      status.remove();
      reply.classList.add("error");
      body.textContent = e.message;
    }
    send.disabled = false;
  }

  const suggestions = ["What's my day look like?", "Who do I need to reply to?", "How's revenue this month?", "Plan my week"];
  let mic = null;
  if (Speech) {
    mic = h("button", { type: "button", class: "icon-btn mic", "aria-label": "Speak", style: "flex:0 0 auto" }, icon("mic"));
    mic.addEventListener("click", () => {
      const rec = new Speech();
      rec.lang = "en-AU";
      rec.interimResults = true;
      mic.classList.add("on");
      rec.onresult = (e) => { input.value = [...e.results].map((r) => r[0].transcript).join(""); };
      rec.onend = () => { mic.classList.remove("on"); input.focus(); };
      rec.onerror = () => { mic.classList.remove("on"); toast("Couldn't hear that"); };
      rec.start();
    });
  }
  if (!d.messages.length) thread.append(h("div", { class: "suggestions" }, suggestions.map((s) => h("button", { onclick: () => ask(s) }, s))));
  return [
    h("div", { class: "row", style: "justify-content:flex-end;margin-bottom:8px" },
      h("button", { class: "ghost", style: "flex:0 0 auto", onclick: async () => { await api("/api/chat/new", { method: "POST" }); cache.delete("/api/chat"); render({ fresh: true }); } }, "New chat")),
    thread,
    h("form", { class: "chat-input row", onsubmit: (ev) => { ev.preventDefault(); ask(input.value); } }, mic, input, send),
  ];
};

// ---------- routing ----------

const titles = { today: "Today", calendar: "Calendar", todos: "To-dos", clients: "Clients", money: "Money", ask: "Ask" };
let current = { tab: "today", arg: null };
let renderSeq = 0;

function go(tab, arg = null) {
  current = { tab, arg };
  const hash = arg ? `#${tab}/${encodeURIComponent(arg)}` : `#${tab}`;
  if (location.hash !== hash) history.pushState(null, "", hash);
  window.scrollTo(0, 0);
  render();
}

const busyTyping = () => {
  const a = document.activeElement;
  return a && view.contains(a) && (a.tagName === "TEXTAREA" || (a.tagName === "INPUT" && a.value)) || view.querySelector(".editor");
};

// Shows cached data first (if any), then quietly swaps in fresh data, unless you're mid-edit.
async function render({ fresh = false } = {}) {
  const { tab, arg } = current;
  const seq = ++renderSeq;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#view-title").textContent = titles[tab];
  const stillHere = () => seq === renderSeq;

  if (!fresh) {
    const cached = loader(true);
    let content = null;
    const timer = setTimeout(() => stillHere() && !content && view.replaceChildren(...skeleton()), 120);
    try {
      content = await views[tab](arg, cached);
    } catch (e) {
      clearTimeout(timer);
      if (stillHere()) view.replaceChildren(h("div", { class: "card" }, errorBox(e)));
      return;
    }
    clearTimeout(timer);
    if (!stillHere()) return;
    view.replaceChildren(...[content].flat(3).filter(Boolean));
    if (!cached.usedCache) return; // that was already fresh
  }
  setRefreshing(true);
  try {
    const content = await views[tab](arg, loader(false));
    if (stillHere() && !(busyTyping() && !fresh)) view.replaceChildren(...[content].flat(3).filter(Boolean));
  } catch (e) {
    if (stillHere() && fresh) toast(e.message);
  } finally {
    if (stillHere()) setRefreshing(false);
  }
}

function setRefreshing(on) { $("#refresh").classList.toggle("spin", on); }

function fromHash() {
  const [tab, arg] = location.hash.slice(1).split("/");
  current = { tab: titles[tab] ? tab : "today", arg: arg ? decodeURIComponent(arg) : null };
}

// Pull down at the top of the page to refresh.
(function pullToRefresh() {
  const ind = $("#ptr");
  let y0 = null, pulled = 0;
  window.addEventListener("touchstart", (e) => { y0 = window.scrollY <= 0 ? e.touches[0].clientY : null; pulled = 0; }, { passive: true });
  window.addEventListener("touchmove", (e) => {
    if (y0 === null) return;
    pulled = Math.max(0, e.touches[0].clientY - y0);
    ind.style.height = Math.min(pulled * 0.5, 60) + "px";
    ind.classList.toggle("ready", pulled > 110);
  }, { passive: true });
  window.addEventListener("touchend", () => {
    if (y0 !== null && pulled > 110) render({ fresh: true });
    y0 = null;
    ind.style.height = "0";
    ind.classList.remove("ready");
  });
})();

document.querySelectorAll(".tabs button").forEach((b) => {
  b.prepend(icon(b.dataset.tab));
  b.addEventListener("click", () => go(b.dataset.tab));
});
$("#refresh").prepend(icon("refresh"));
$("#refresh").addEventListener("click", () => render({ fresh: true }));
window.addEventListener("popstate", () => { fromHash(); render(); });
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && !$("#app").hidden) render(); });

// ---------- login ----------

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#password").focus();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { password: $("#password").value } });
    $("#password").value = "";
    start();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

async function start() {
  try {
    me = await api("/api/me");
  } catch (e) {
    if (e.message !== "Please log in") {
      $("#login").hidden = false;
      $("#login-error").textContent = e.message;
    }
    return;
  }
  $("#login").hidden = true;
  $("#app").hidden = false;
  // Coming back from Google's sign-in page.
  const google = new URLSearchParams(location.search).get("google");
  if (google) {
    history.replaceState(null, "", location.pathname + location.hash);
    toast({ connected: "Google Calendar connected ✓", cancelled: "Google connection cancelled" }[google] || "Couldn't connect Google Calendar. Try again.");
  }
  fromHash();
  render();
}

start();
