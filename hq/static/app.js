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

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2800);
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
const notConnected = (what, how) => h("div", { class: "empty" }, `${what} isn't connected yet. ${how}`);
const loading = () => h("div", { class: "spinner" }, "Loading…");
const errorBox = (e) => h("div", { class: "error small" }, e.message || String(e));

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

function todoItem(t, onChange) {
  const iso = todayISO();
  let due = null;
  if (t.due) {
    const cls = t.due < iso ? "pill bad" : t.due === iso ? "pill warn" : "pill";
    due = h("span", { class: cls }, t.due < iso ? `Overdue · ${dayLabel(t.due)}` : dayLabel(t.due));
  }
  return h("li", {},
    h("button", {
      class: "check" + (t.done ? " done" : ""), "aria-label": t.done ? "Mark not done" : "Mark done",
      onclick: async () => {
        await api(`/api/todos/${t.id}`, { method: "PATCH", body: { done: !t.done } });
        if (!t.done) toast("Done ✓");
        onChange();
      },
    }),
    h("div", { class: "grow" },
      h("div", { class: "title" + (t.done ? " done-text" : "") }, t.priority ? "★ " : "", t.title),
      h("div", { class: "meta" }, due, " ", t.area, t.notes ? ` · ${t.notes}` : ""),
    ),
    h("button", {
      class: "icon-btn small", "aria-label": "Delete",
      onclick: async () => { if (confirm(`Delete "${t.title}"?`)) { await api(`/api/todos/${t.id}`, { method: "DELETE" }); onChange(); } },
    }, "×"),
  );
}

function conversationItem(c) {
  return h("li", { class: "clickable", onclick: () => go("clients", c.id) },
    h("div", { class: "grow" },
      h("div", { class: "title" }, c.name, " ", c.unread ? h("span", { class: "pill" }, `${c.unread} new`) : null),
      h("div", { class: "meta" }, c.last_direction === "outbound" ? "You: " : "", c.last_message || "(no text)"),
    ),
    h("div", { class: "meta" }, c.channel, h("br"), ago(c.last_message_at)),
  );
}

// ---------- views ----------

const views = {};

views.today = async () => {
  const d = await api("/api/today");
  const iso = d.date;
  // The feed covers today and tomorrow; anything that starts on or before today is on today.
  const todayEvents = d.events.events.filter((e) => e.start.slice(0, 10) <= iso);
  const tomorrowEvents = d.events.events.filter((e) => e.start.slice(0, 10) === addDays(iso, 1));
  const unread = d.unread.conversations || [];
  const hour = Number(new Intl.DateTimeFormat("en-AU", { hour: "numeric", hourCycle: "h23", timeZone: me.timezone }).format(new Date()));
  const greeting = hour < 12 ? "Morning" : hour < 17 ? "Afternoon" : "Evening";

  return [
    h("p", { class: "muted", style: "margin:0 0 12px" },
      `${greeting} Alistair. ${new Date().toLocaleDateString("en-AU", { weekday: "long", day: "numeric", month: "long", timeZone: me.timezone })}.`),
    h("div", { class: "stats", style: "margin-bottom:12px" },
      h("div", { class: "stat" }, h("div", { class: "label" }, "On today"), h("div", { class: "value" }, d.events.configured ? todayEvents.filter((e) => !e.all_day).length : "—"), h("div", { class: "sub" }, "events")),
      h("div", { class: "stat" }, h("div", { class: "label" }, "Due / overdue"), h("div", { class: "value" }, d.todos_due.length), h("div", { class: "sub" }, `${d.open_todos} open in total`)),
      h("div", { class: "stat" }, h("div", { class: "label" }, "Unread chats"), h("div", { class: "value" }, d.unread.configured ? unread.length : "—"), h("div", { class: "sub" }, d.drafts.length ? `${d.drafts.length} draft(s) waiting` : "in GoHighLevel")),
      h("div", { class: "stat" }, h("div", { class: "label" }, "Collected this week"), h("div", { class: "value" }, d.money.configured ? money(d.money.this_week) : "—"), h("div", { class: "sub" }, d.money.configured ? `${money(d.money.this_month)} this month` : "Stripe not connected")),
    ),
    h("div", { class: "card" }, h("h2", {}, "Schedule"),
      d.events.configured ? eventList(todayEvents) : notConnected("Your calendar", "Add HQ_CALENDAR_ICS_URLS."),
      d.events.errors ? h("div", { class: "error small" }, "One of your calendars didn't load.") : null,
      tomorrowEvents.length ? h("details", { style: "margin-top:8px" }, h("summary", {}, `Tomorrow · ${tomorrowEvents.length} event(s)`), eventList(tomorrowEvents)) : null,
    ),
    h("div", { class: "card" }, h("h2", {}, "To do today ", h("span", { class: "count" }, d.todos_due.length || "")),
      d.todos_due.length ? h("ul", { class: "list" }, d.todos_due.map((t) => todoItem(t, render))) : h("div", { class: "empty" }, "Nothing due. "),
      d.todos_important.length ? [h("h2", { style: "margin-top:12px" }, "Important, no rush"), h("ul", { class: "list" }, d.todos_important.map((t) => todoItem(t, render)))] : null,
      quickAdd(render),
    ),
    d.drafts.length ? h("div", { class: "card" }, h("h2", {}, "Reply drafts waiting for you"),
      h("ul", { class: "list" }, d.drafts.map((dr) => h("li", { class: "clickable", onclick: () => go("clients", dr.conversation_id) },
        h("div", { class: "grow" }, h("div", { class: "title" }, dr.contact_name), h("div", { class: "meta" }, dr.text)))))) : null,
    h("div", { class: "card" }, h("h2", {}, "Unread messages"),
      !d.unread.configured ? notConnected("Your inbox", "Add GHL_API_TOKEN and GHL_LOCATION_ID.")
        : d.unread.error ? h("div", { class: "error small" }, d.unread.error)
        : unread.length ? h("ul", { class: "list" }, unread.map(conversationItem)) : h("div", { class: "empty" }, "Inbox zero 🙌"),
    ),
    d.money.failed && d.money.failed.length ? h("div", { class: "card" }, h("h2", {}, "Failed payments"),
      h("ul", { class: "list" }, d.money.failed.slice(0, 5).map((f) => h("li", {},
        h("div", { class: "grow" }, h("div", { class: "title" }, f.customer || "Unknown customer"), h("div", { class: "meta" }, `${dayLabel(f.date)} · ${f.reason}`)),
        h("div", { class: "amount out" }, money(f.amount)))))) : null,
  ];
};

function quickAdd(after, defaults = {}) {
  const input = h("input", { type: "text", placeholder: "Add a to-do…", "aria-label": "New to-do" });
  const due = h("input", { type: "date", value: defaults.due || todayISO(), "aria-label": "Due date" });
  return h("form", {
    class: "stack", style: "margin-top:10px",
    onsubmit: async (ev) => {
      ev.preventDefault();
      if (!input.value.trim()) return;
      await api("/api/todos", { method: "POST", body: { title: input.value.trim(), due: due.value || null, area: defaults.area || "business" } });
      input.value = "";
      after();
    },
  }, input, h("div", { class: "row" }, due, h("button", { class: "secondary", type: "submit", style: "flex:0 0 auto" }, "Add")));
}

let calStart = null;
views.calendar = async () => {
  calStart = calStart || todayISO();
  const days = 7;
  const d = await api(`/api/calendar?start=${calStart}&days=${days}`);
  if (!d.configured) return h("div", { class: "card" }, notConnected("Your calendar", "Add one or more private iCal links to HQ_CALENDAR_ICS_URLS."));
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
  return [
    h("div", { class: "segmented" },
      h("button", { onclick: () => { calStart = addDays(calStart, -7); render(); } }, "← Prev"),
      h("button", { class: calStart === todayISO() ? "active" : "", onclick: () => { calStart = todayISO(); render(); } }, "This week"),
      h("button", { onclick: () => { calStart = addDays(calStart, 7); render(); } }, "Next →"),
    ),
    d.errors ? h("div", { class: "error small" }, "One of your calendars didn't load.") : null,
    h("div", { class: "card" }, Object.entries(byDay).map(([day, evs]) => [h("div", { class: "day-head" }, dayLabel(day)), eventList(evs)])),
    h("p", { class: "muted small" }, "Read-only. Add or change events in Google Calendar."),
  ];
};

let todoFilter = "open";
views.todos = async () => {
  const todos = await api(`/api/todos?include_done=${todoFilter === "done"}`);
  const iso = todayISO();
  const shown = todoFilter === "done" ? todos.filter((t) => t.done) : todos;
  const groups = todoFilter === "done" ? [["Done", shown]] : [
    ["Overdue", shown.filter((t) => t.due && t.due < iso)],
    ["Today", shown.filter((t) => t.due === iso)],
    ["Upcoming", shown.filter((t) => t.due && t.due > iso)],
    ["Someday", shown.filter((t) => !t.due)],
  ];
  const area = h("select", { "aria-label": "Area" }, ["business", "clients", "content", "personal", "health", "home"].map((a) => h("option", { value: a }, a)));
  const title = h("input", { type: "text", placeholder: "What needs doing?", required: true });
  const due = h("input", { type: "date", "aria-label": "Due date" });
  const important = h("input", { type: "checkbox", id: "imp" });
  return [
    h("form", {
      class: "card stack",
      onsubmit: async (ev) => {
        ev.preventDefault();
        await api("/api/todos", { method: "POST", body: { title: title.value.trim(), due: due.value || null, area: area.value, priority: important.checked ? 1 : 0 } });
        toast("Added");
        render();
      },
    }, title, h("div", { class: "row" }, due, area), h("div", { class: "row", style: "align-items:center" },
      h("label", { for: "imp", class: "small", style: "display:flex;gap:6px;align-items:center" }, important, "Important"),
      h("button", { class: "primary", type: "submit", style: "flex:0 0 auto" }, "Add to-do"))),
    h("div", { class: "segmented" },
      ["open", "done"].map((f) => h("button", { class: todoFilter === f ? "active" : "", onclick: () => { todoFilter = f; render(); } }, f === "open" ? "Open" : "Done"))),
    groups.filter(([, items]) => items.length).map(([name, items]) =>
      h("div", { class: "card" }, h("h2", {}, name, " ", h("span", { class: "count" }, items.length)), h("ul", { class: "list" }, items.map((t) => todoItem(t, render))))),
    shown.length ? null : h("div", { class: "empty" }, todoFilter === "done" ? "Nothing completed yet." : "All clear."),
  ];
};

let inboxFilter = "all";
views.clients = async (conversationId) => {
  if (conversationId) return threadView(conversationId);
  if (!me.connected.inbox) return h("div", { class: "card" }, notConnected("Your GoHighLevel inbox", "Add GHL_API_TOKEN and GHL_LOCATION_ID."));
  const d = await api(`/api/inbox?unread=${inboxFilter === "unread"}`);
  return [
    h("div", { class: "segmented" },
      ["all", "unread"].map((f) => h("button", { class: inboxFilter === f ? "active" : "", onclick: () => { inboxFilter = f; render(); } }, f === "all" ? "All" : "Unread"))),
    h("div", { class: "card" }, d.conversations.length ? h("ul", { class: "list" }, d.conversations.map(conversationItem)) : h("div", { class: "empty" }, "No conversations.")),
  ];
};

async function threadView(id) {
  const c = await api(`/api/inbox/${encodeURIComponent(id)}`);
  $("#view-title").textContent = c.name;
  const box = h("textarea", { placeholder: c.send_type ? `Reply on ${c.channel}…` : "", value: c.draft ? c.draft.text : "" });
  const instructions = h("input", { type: "text", placeholder: "Optional: what should the reply say?" });
  let draftId = c.draft ? c.draft.id : null;

  const draftBtn = h("button", {
    class: "secondary", type: "button",
    onclick: async () => {
      draftBtn.disabled = true; draftBtn.textContent = "Drafting…";
      try {
        const dr = await api(`/api/inbox/${encodeURIComponent(id)}/draft`, { method: "POST", body: { instructions: instructions.value } });
        box.value = dr.text; draftId = dr.id;
      } catch (e) { toast(e.message); }
      draftBtn.disabled = false; draftBtn.textContent = "✦ Draft with Claude";
    },
  }, "✦ Draft with Claude");

  const sendBtn = h("button", {
    class: "primary", type: "button",
    onclick: async () => {
      const text = box.value.trim();
      if (!text) return;
      if (!confirm(`Send this to ${c.name} on ${c.channel}?\n\n${text}`)) return;
      sendBtn.disabled = true;
      try {
        await api(`/api/inbox/${encodeURIComponent(id)}/send`, { method: "POST", body: { text, draft_id: draftId } });
        toast("Sent");
        render();
      } catch (e) { toast(e.message); sendBtn.disabled = false; }
    },
  }, "Send");

  return [
    h("button", { class: "link-btn", style: "margin-bottom:10px", onclick: () => go("clients") }, "← All conversations"),
    h("div", { class: "muted small", style: "margin-bottom:8px" }, c.channel, c.tags.length ? ` · ${c.tags.join(", ")}` : ""),
    h("div", { class: "thread" }, c.messages.length ? c.messages.map((m) =>
      h("div", { class: `bubble ${m.from}` }, m.text, h("span", { class: "at" }, ago(m.at)))) : h("div", { class: "empty" }, "No messages.")),
    c.send_type ? h("div", { class: "card stack" },
      box,
      instructions,
      h("div", { class: "row" }, draftBtn, sendBtn),
      draftId ? h("button", { class: "link-btn small", onclick: async () => { await api(`/api/drafts/${draftId}/discard`, { method: "POST" }); render(); } }, "Discard draft") : null,
      h("div", { class: "muted small" }, "Nothing is sent until you press Send and confirm."),
    ) : h("div", { class: "card muted small" }, `Replying on ${c.channel} isn't supported here yet. Reply in GoHighLevel.`),
  ];
}

views.money = async () => {
  const d = await api("/api/finance");
  const s = d.stripe;
  const log = d.log;
  const weeks = (s.weekly || []).slice(-12);
  const max = Math.max(1, ...weeks.map((w) => w.amount));
  const amount = h("input", { type: "number", step: "0.01", placeholder: "Amount", required: true });
  const kind = h("select", { "aria-label": "In or out" }, h("option", { value: "out" }, "Money out"), h("option", { value: "in" }, "Money in"));
  const category = h("input", { type: "text", placeholder: "Category (e.g. software)", list: "cats" });
  const note = h("input", { type: "text", placeholder: "Note" });
  const cats = h("datalist", { id: "cats" }, ["software", "ads", "contractors", "food", "rent", "transport", "health", "fun", "income"].map((c) => h("option", { value: c })));

  return [
    h("div", { class: "card" }, h("h2", {}, "Business · Stripe"),
      !s.configured ? notConnected("Stripe", "Add a read-only STRIPE_API_KEY.")
        : s.error ? h("div", { class: "error small" }, s.error)
        : [
          h("div", { class: "stats" },
            h("div", { class: "stat" }, h("div", { class: "label" }, "This week"), h("div", { class: "value" }, money(s.this_week)), h("div", { class: "sub" }, "so far")),
            h("div", { class: "stat" }, h("div", { class: "label" }, "This month"), h("div", { class: "value" }, money(s.this_month)), h("div", { class: "sub" }, `last month ${money(s.last_month)}`)),
            h("div", { class: "stat" }, h("div", { class: "label" }, "Balance"), h("div", { class: "value" }, money(s.balance_available)), h("div", { class: "sub" }, `${money(s.balance_pending)} pending`)),
            h("div", { class: "stat" }, h("div", { class: "label" }, "Active subscriptions"), h("div", { class: "value" }, s.active_subscriptions), h("div", { class: "sub" }, "in Stripe")),
          ),
          weeks.length ? [
            h("div", { class: "bars", role: "img", "aria-label": "Cash collected per week" }, weeks.map((w, i) =>
              h("div", { class: "bar" + (i === weeks.length - 1 ? " partial" : ""), style: `height:${(w.amount / max) * 100}%`, title: `Week of ${w.week_of}: ${money(w.amount)}` }))),
            h("div", { class: "bar-labels" }, weeks.map((w) => h("span", {}, `${Number(w.week_of.slice(8, 10))}/${Number(w.week_of.slice(5, 7))}`))),
            h("div", { class: "muted small", style: "margin-top:6px" }, "Cash collected per week, net of refunds. The current week is partial."),
          ] : null,
          s.failed && s.failed.length ? [h("h2", { style: "margin-top:14px" }, "Failed payments"),
            h("ul", { class: "list" }, s.failed.map((f) => h("li", {},
              h("div", { class: "grow" }, h("div", { class: "title" }, f.customer || "Unknown"), h("div", { class: "meta" }, `${dayLabel(f.date)} · ${f.reason}`)),
              h("div", { class: "amount out" }, money(f.amount)))))] : null,
          h("div", { class: "muted small", style: "margin-top:8px" }, `As of ${s.as_of ? s.as_of.slice(11, 16) : ""}, refreshed every 10 minutes.`),
        ],
    ),
    h("div", { class: "card" }, h("h2", {}, "Money log · this month"),
      h("div", { class: "stats", style: "margin-bottom:10px" },
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
          render();
        },
      }, h("div", { class: "row" }, amount, kind), h("div", { class: "row" }, category, note), cats, h("button", { class: "primary", type: "submit" }, "Log it")),
    ),
    log.recent.length ? h("div", { class: "card" }, h("h2", {}, "Recent entries"),
      h("ul", { class: "list" }, log.recent.map((e) => h("li", {},
        h("div", { class: "grow" }, h("div", { class: "title" }, e.category), h("div", { class: "meta" }, `${dayLabel(e.date)}${e.note ? " · " + e.note : ""}`)),
        h("div", { class: "amount " + (e.amount < 0 ? "out" : "in") }, money(e.amount)),
        h("button", { class: "icon-btn small", "aria-label": "Delete", onclick: async () => { if (confirm("Delete this entry?")) { await api(`/api/money/${e.id}`, { method: "DELETE" }); render(); } } }, "×"))))) : null,
  ];
};

views.ask = async () => {
  const d = await api("/api/chat");
  const thread = h("div", { class: "chat" }, d.messages.map((m) => h("div", { class: `bubble ${m.role}` }, m.text)));
  const input = h("textarea", { placeholder: "Ask anything, or tell me what to do…", rows: 2 });
  const send = h("button", { class: "primary", type: "submit", style: "flex:0 0 auto" }, "Send");

  async function ask(text) {
    if (!text.trim()) return;
    thread.append(h("div", { class: "bubble user" }, text));
    const thinking = h("div", { class: "bubble assistant muted" }, "Thinking…");
    thread.append(thinking);
    thinking.scrollIntoView({ behavior: "smooth", block: "end" });
    input.value = ""; send.disabled = true;
    try {
      const r = await api("/api/chat", { method: "POST", body: { message: text } });
      thinking.className = "bubble assistant";
      thinking.textContent = r.reply || "(no reply)";
      if (r.tools && r.tools.length) thinking.append(h("span", { class: "at" }, `used: ${[...new Set(r.tools)].join(", ").replaceAll("_", " ")}`));
    } catch (e) {
      thinking.className = "bubble assistant error";
      thinking.textContent = e.message;
    }
    send.disabled = false;
    thinking.scrollIntoView({ behavior: "smooth", block: "end" });
  }

  const suggestions = ["What's my day look like?", "Who do I need to reply to?", "How's revenue this month?", "Plan my week"];
  return [
    h("div", { class: "row", style: "justify-content:flex-end;margin-bottom:8px" },
      h("button", { class: "ghost", style: "flex:0 0 auto", onclick: async () => { await api("/api/chat/new", { method: "POST" }); render(); } }, "New chat")),
    d.messages.length ? null : h("div", { class: "suggestions" }, suggestions.map((s) => h("button", { onclick: () => ask(s) }, s))),
    thread,
    h("form", { class: "chat-input row", onsubmit: (ev) => { ev.preventDefault(); ask(input.value); } }, input, send),
  ];
};

// ---------- routing ----------

const titles = { today: "Today", calendar: "Calendar", todos: "To-dos", clients: "Clients", money: "Money", ask: "Ask" };
let current = { tab: "today", arg: null };

function go(tab, arg = null) {
  current = { tab, arg };
  const hash = arg ? `#${tab}/${encodeURIComponent(arg)}` : `#${tab}`;
  if (location.hash !== hash) history.pushState(null, "", hash);
  render();
}

async function render() {
  const { tab, arg } = current;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("#view-title").textContent = titles[tab];
  view.replaceChildren(loading());
  try {
    const content = await views[tab](arg);
    if (current.tab !== tab || current.arg !== arg) return; // the user moved on while this loaded
    view.replaceChildren(...[content].flat(3).filter(Boolean));
  } catch (e) {
    view.replaceChildren(h("div", { class: "card" }, errorBox(e)));
  }
}

function fromHash() {
  const [tab, arg] = location.hash.slice(1).split("/");
  current = { tab: titles[tab] ? tab : "today", arg: arg ? decodeURIComponent(arg) : null };
}

document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => go(b.dataset.tab)));
$("#refresh").addEventListener("click", render);
window.addEventListener("popstate", () => { fromHash(); render(); });

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
  fromHash();
  render();
}

start();
