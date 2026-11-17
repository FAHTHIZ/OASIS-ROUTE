// Oasis Route console. No framework, no inline scripts; server data is rendered as text nodes only.
"use strict";

const KEY = "oasis.key";
const state = { me: null, providers: [] };

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}

const fmt = (v) => (v === null || v === undefined ? "—" : typeof v === "number" ? v.toLocaleString() : String(v));
const when = (v) => (v ? new Date(v).toLocaleString() : "—");
const pill = (s) => el("span", { class: `pill ${s}` }, s);

async function api(path, opts = {}) {
  const key = sessionStorage.getItem(KEY);
  const headers = { authorization: `Bearer ${key}`, ...(opts.body ? { "content-type": "application/json" } : {}) };
  const res = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = data.error || {};
    throw new Error(`${res.status} ${e.code || ""}: ${e.message || res.statusText}`);
  }
  return data;
}

function table(cols, rows, empty = "Nothing here yet.") {
  if (!rows.length) return el("p", { class: "muted" }, empty);
  return el(
    "table",
    {},
    el("thead", {}, el("tr", {}, cols.map((c) => el("th", { class: c.num ? "num" : "" }, c.label)))),
    el(
      "tbody",
      {},
      rows.map((r) => el("tr", {}, cols.map((c) => el("td", { class: c.num ? "num" : "" }, c.get(r)))))
    )
  );
}

function card(title, ...body) {
  return el("div", { class: "card" }, title ? el("h2", {}, title) : null, ...body);
}

function errorBox(e) {
  return el("p", { class: "error" }, e.message || String(e));
}

const pid = () => state.me.project.id;

// ---------------------------------------------------------------------------------------------- views
const views = {
  async overview() {
    const st = await api(`/api/v1/projects/${pid()}/status`);
    const tone = { Ready: "ok", Connected: "ok", "Action required": "error", "Temporarily degraded": "warn" };
    const check = (ok, label) => el("li", { class: ok ? "ok" : "muted" }, `${ok ? "Done" : "Pending"} · ${label}`);
    const serving = st.serving.next_request;
    const last = st.serving.last_request;
    const out = [
      card(
        null,
        el("h1", { class: tone[st.status] || "" }, st.status),
        el("p", {}, st.summary),
        el(
          "ul",
          { class: "checks" },
          check(st.setup.credential_validated, "Provider credential validated"),
          check(st.setup.models_synced, "Models and capabilities synchronized"),
          check(st.setup.routing_ready, "Routing ready"),
          check(st.setup.oasis_key_issued, "Oasis Key issued")
        )
      ),
      el(
        "div",
        { class: "kpis" },
        el("div", { class: "kpi" }, el("div", { class: "v" }, serving ? serving.model : "—"),
          el("div", { class: "l" }, serving ? `Next request goes to ${serving.provider}` : "No model can serve right now")),
        el("div", { class: "kpi" }, el("div", { class: "v" }, last ? last.model : "—"),
          el("div", { class: "l" }, last ? `Last served by ${last.provider} · ${when(last.at)}` : "No requests yet")),
        el("div", { class: "kpi" }, el("div", { class: "v" }, st.capacity.status), el("div", { class: "l" }, st.capacity.message)),
        el("div", { class: "kpi" }, el("div", { class: "v" }, st.automation.paused ? "Paused" : st.automation.background),
          el("div", { class: "l" }, "Background maintenance"))
      ),
    ];
    if (st.attention.length)
      out.push(card("Needs your attention", st.attention.map((n) =>
        el("div", { class: "note" }, el("strong", { class: n.severity === "critical" ? "error" : "warn" }, n.title), el("div", { class: "muted" }, n.body)))));
    out.push(
      card(
        "Connections",
        table(
          [
            { label: "Name", get: (c) => c.name },
            { label: "Status", get: (c) => el("span", { class: tone[c.status] || "" }, c.status) },
            { label: "Details", get: (c) => c.message },
            { label: "Last checked", get: (c) => when(c.last_checked_at) },
          ],
          st.connections,
          "No provider connected yet. Open Connections and add a provider API key; everything else is automatic."
        )
      )
    );
    const toggle = el(
      "button",
      {
        class: "secondary",
        onclick: async () => {
          try {
            await api(`/api/v1/projects/${pid()}/automation`, { method: "PUT", body: { paused: !st.automation.paused } });
          } catch (e) {
            alert(e.message);
          }
          render("overview");
        },
      },
      st.automation.paused ? "Resume background maintenance" : "Pause background maintenance"
    );
    out.push(card("Automation", el("p", { class: "muted" },
      "Credential checks, quota refresh and provider health checks run automatically. Pausing them does not stop routing."), toggle));
    return out;
  },

  async capacity() {
    const cap = await api(`/api/v1/projects/${pid()}/capacity`);
    const kpi = (l, v) => el("div", { class: "kpi" }, el("div", { class: "v" }, fmt(v)), el("div", { class: "l" }, l));
    const h = cap.consumed.last_24h;
    const out = [
      el(
        "div",
        { class: "kpis" },
        kpi("Authorized requests / day", cap.authorized.per_day.requests),
        kpi("Usable requests right now", cap.usable.headroom_now.requests),
        kpi("Requests (24h, confirmed)", h.confirmed.requests),
        kpi("Requests (24h, estimated)", h.estimated.requests),
        kpi("Failed attempts (24h)", h.failed_attempts)
      ),
    ];
    out.push(
      card(
        "Allocations",
        table(
          [
            { label: "Provider", get: (a) => a.provider_id },
            { label: "Account", get: (a) => a.account_scope },
            { label: "Models", get: (a) => a.model_scope },
            { label: "Authorized", get: (a) => (a.authorized ? "yes" : "no") },
            { label: "Routable", get: (a) => (a.routable_now ? pill("healthy") : pill("open")) },
            {
              label: "Dimensions",
              get: (a) =>
                a.dimensions.map((d) =>
                  el(
                    "div",
                    {},
                    `${d.unit}: ${fmt(d.headroom_now)} now · ${fmt(d.sustained_per_day)}/day`,
                    d.limit_unknown ? el("span", { class: "warn" }, " (limit unknown)") : null
                  )
                ),
            },
          ],
          cap.allocations,
          "No quota pools yet. Pools are created from provider rate-limit headers or added manually."
        )
      )
    );
    if (cap.unmetered_connections.length)
      out.push(
        card(
          "Unmetered connections",
          el("p", { class: "muted" }, "Connected but without known limits; excluded from capacity totals."),
          table(
            [
              { label: "Provider", get: (u) => u.provider_id },
              { label: "Account", get: (u) => u.account_scope },
            ],
            cap.unmetered_connections
          )
        )
      );
    out.push(
      card(
        "Forecast",
        el("p", { class: "muted" }, `${cap.forecast.method}. Confidence: ${cap.forecast.confidence}.`),
        table(
          [
            { label: "Provider", get: (i) => i.provider_id },
            { label: "Unit", get: (i) => i.unit },
            { label: "Burn / hour", num: true, get: (i) => fmt(i.burn_per_hour) },
            { label: "Projected exhaustion", get: (i) => when(i.projected_exhaustion) },
            { label: "Before reset?", get: (i) => (i.exhausts_before_reset ? el("span", { class: "warn" }, "yes") : "no") },
          ],
          cap.forecast.items,
          "Not enough recent traffic to forecast."
        )
      )
    );
    return out;
  },

  async connections() {
    const [conns, providers] = await Promise.all([
      api(`/api/v1/projects/${pid()}/connections`),
      api("/api/v1/providers"),
    ]);
    const msg = el("p", {});
    const form = el(
      "form",
      {
        onsubmit: async (ev) => {
          ev.preventDefault();
          const f = new FormData(ev.target);
          try {
            await api(`/api/v1/projects/${pid()}/connections`, {
              method: "POST",
              body: {
                provider_id: f.get("provider"),
                api_key: f.get("api_key") || null,
                base_url: f.get("base_url") || null,
                display_name: f.get("display_name") || "",
                upstream_account_ref: f.get("account") || "default",
                accept_terms: f.get("accept") === "on",
              },
            });
            render("connections");
          } catch (e) {
            msg.replaceChildren(errorBox(e));
          }
        },
      },
      el("label", {}, "Provider", el("select", { name: "provider" }, providers.map((p) => el("option", { value: p.id }, p.name)))),
      el("label", {}, "Your API key", el("input", { name: "api_key", type: "password", autocomplete: "off" })),
      el("label", {}, "Base URL (optional)", el("input", { name: "base_url", placeholder: "provider default" })),
      el("label", {}, "Name", el("input", { name: "display_name" })),
      el("label", {}, "Account label", el("input", { name: "account", value: "default" })),
      el("label", {}, el("span", {}, el("input", { type: "checkbox", name: "accept" }), " I own this account and accept the provider's terms")),
      el("button", { type: "submit" }, "Connect")
    );
    const termsList = el(
      "p",
      { class: "muted" },
      "Only connect accounts you hold yourself. Oasis Route never creates accounts, cycles trials, or rotates identities to extend quota."
    );
    return [
      card("Connect a provider", termsList, form, msg),
      card(
        "Connections",
        table(
          [
            { label: "Name", get: (c) => c.display_name || c.provider_id },
            { label: "Provider", get: (c) => c.provider_id },
            { label: "Account", get: (c) => c.upstream_account_ref },
            { label: "Status", get: (c) => pill(c.status) },
            { label: "Validated", get: (c) => when(c.last_validated_at) },
            { label: "Last error", get: (c) => c.last_error || "" },
            {
              label: "",
              get: (c) =>
                el(
                  "span",
                  {},
                  el("button", { class: "secondary", onclick: () => act(`/api/v1/projects/${pid()}/connections/${c.id}/validate`, "POST", "connections") }, "Validate"),
                  " ",
                  el("button", { class: "danger", onclick: () => confirm("Delete this connection?") && act(`/api/v1/projects/${pid()}/connections/${c.id}`, "DELETE", "connections") }, "Delete")
                ),
            },
          ],
          conns,
          "No connections yet."
        )
      ),
    ];
  },

  async keys() {
    const keys = await api(`/api/v1/projects/${pid()}/keys`);
    const shown = el("div", {});
    const form = el(
      "form",
      {
        onsubmit: async (ev) => {
          ev.preventDefault();
          const f = new FormData(ev.target);
          try {
            const r = await api(`/api/v1/projects/${pid()}/keys`, {
              method: "POST",
              body: { name: f.get("name") || "", scopes: f.getAll("scope"), expires_in_days: f.get("days") ? Number(f.get("days")) : null },
            });
            shown.replaceChildren(
              el("p", { class: "warn" }, "Copy this Oasis Key now. It is shown once and cannot be retrieved later."),
              el("div", { class: "secret" }, `OASIS_API_KEY=${r.secret}`)
            );
            const list = await viewsInner.keyTable();
            document.getElementById("key-list").replaceChildren(list);
          } catch (e) {
            shown.replaceChildren(errorBox(e));
          }
        },
      },
      el("label", {}, "Name", el("input", { name: "name" })),
      ...["inference", "read", "manage"].map((s) =>
        el("label", {}, el("span", {}, el("input", { type: "checkbox", name: "scope", value: s, checked: s === "inference" }), ` ${s}`))
      ),
      el("label", {}, "Expires in days", el("input", { name: "days", type: "number", min: 1, max: 3650 })),
      el("button", { type: "submit" }, "Create key")
    );
    viewsInner.keys = keys;
    return [card("Create an Oasis Key", form, shown), card("Keys", el("div", { id: "key-list" }, await viewsInner.keyTable(keys)))];
  },

  async models() {
    const models = await api("/api/v1/models");
    const score = (m) => (m.quality && m.quality.general ? m.quality.general.score : null);
    return [
      card(
        "Model catalogue",
        table(
          [
            { label: "Provider", get: (m) => m.provider_id },
            { label: "Model", get: (m) => m.upstream_id },
            { label: "Context", num: true, get: (m) => fmt(m.context_window) },
            { label: "Quality (general)", num: true, get: (m) => fmt(score(m)) },
            { label: "Capabilities", get: (m) => Object.entries(m.capabilities || {}).filter(([, v]) => v === true).map(([k]) => k).join(", ") },
            { label: "Lifecycle", get: (m) => pill(m.lifecycle) },
          ],
          models
        )
      ),
    ];
  },

  async route() {
    const out = el("div", {});
    const form = el(
      "form",
      {
        onsubmit: async (ev) => {
          ev.preventDefault();
          const f = new FormData(ev.target);
          try {
            const r = await api(`/api/v1/projects/${pid()}/route-preview`, {
              method: "POST",
              body: {
                model: f.get("model") || "auto",
                messages: [{ role: "user", content: f.get("prompt") || "hello" }],
                oasis_route: { mode: f.get("mode"), task: f.get("task") },
              },
            });
            out.replaceChildren(
              el("p", { class: "muted" }, `Mode ${r.mode}, task ${r.task}, best quality ${fmt(r.best_quality)}, max fallback hops ${r.max_fallback_hops}.`),
              el("h2", {}, "Candidates, in order"),
              table(
                [
                  { label: "#", get: (c) => r.candidates.indexOf(c) + 1 },
                  { label: "Provider", get: (c) => c.provider_id },
                  { label: "Model", get: (c) => c.upstream_model },
                  { label: "Quality", num: true, get: (c) => fmt(c.quality) },
                  { label: "Score", num: true, get: (c) => fmt(c.score !== undefined ? Math.round(c.score * 1000) / 1000 : null) },
                  { label: "Downgrade", get: (c) => (r.downgrade_flags[r.candidates.indexOf(c)] ? el("span", { class: "warn" }, "yes") : "no") },
                ],
                r.candidates,
                "No eligible candidate."
              ),
              el("h2", {}, "Rejected"),
              table(
                [
                  { label: "Provider", get: (x) => x.provider_id },
                  { label: "Model", get: (x) => x.upstream_model },
                  { label: "Reason", get: (x) => x.reason },
                  { label: "Retry in (s)", num: true, get: (x) => fmt(x.retry_in_s) },
                ],
                r.rejected,
                "None."
              )
            );
          } catch (e) {
            out.replaceChildren(errorBox(e));
          }
        },
      },
      el("label", {}, "Model", el("input", { name: "model", value: "auto" })),
      el("label", {}, "Mode", el("select", { name: "mode" }, ["strict_quality", "balanced"].map((m) => el("option", {}, m)))),
      el("label", {}, "Task", el("select", { name: "task" }, ["general", "coding", "reasoning", "creative", "extraction", "vision"].map((m) => el("option", {}, m)))),
      el("label", {}, "Prompt", el("input", { name: "prompt", value: "hello" })),
      el("button", { type: "submit" }, "Preview")
    );
    return [card("Route preview", el("p", { class: "muted" }, "Explains routing without calling a provider or reserving quota."), form, out)];
  },

  async usage() {
    const [usage, decisions] = await Promise.all([
      api(`/api/v1/projects/${pid()}/usage?limit=100`),
      api(`/api/v1/projects/${pid()}/route-decisions?limit=50`),
    ]);
    return [
      card(
        "Recent routing decisions",
        table(
          [
            { label: "Time", get: (d) => when(d.created_at) },
            { label: "Requested", get: (d) => d.requested_model },
            { label: "Served", get: (d) => (d.served_provider ? `${d.served_provider}/${d.served_model}` : "—") },
            { label: "Attempts", num: true, get: (d) => d.attempts.length },
            { label: "Fallback", get: (d) => (d.fallback_used ? "yes" : "no") },
            { label: "Outcome", get: (d) => pill(d.outcome) },
          ],
          decisions
        )
      ),
      card(
        "Usage records",
        table(
          [
            { label: "Time", get: (u) => when(u.created_at) },
            { label: "Provider", get: (u) => u.provider_id },
            { label: "Model", get: (u) => u.upstream_model },
            { label: "In", num: true, get: (u) => fmt(u.input_tokens) },
            { label: "Out", num: true, get: (u) => fmt(u.output_tokens) },
            { label: "Source", get: (u) => u.usage_source },
            { label: "Outcome", get: (u) => pill(u.outcome) },
            { label: "Latency", num: true, get: (u) => (u.latency_ms !== null ? `${u.latency_ms} ms` : "—") },
          ],
          usage
        )
      ),
    ];
  },

  async offers() {
    const offers = await api("/api/v1/offers");
    return [
      card(
        "Provider offers",
        el("p", { class: "muted" }, "Offers are candidates until reviewed by an administrator; only connectable and active offers can be used."),
        table(
          [
            { label: "Provider", get: (o) => o.provider_id },
            { label: "Title", get: (o) => o.title },
            { label: "State", get: (o) => pill(o.state) },
            { label: "Credit", get: (o) => o.credit_type || "—" },
            { label: "Expires", get: (o) => when(o.expires_at) },
            { label: "Terms", get: (o) => (o.terms_url ? el("a", { href: o.terms_url, rel: "noopener noreferrer", target: "_blank" }, "terms") : "—") },
          ],
          offers
        )
      ),
    ];
  },

  async background() {
    const st = await api("/api/v1/admin/jobs");
    return [
      card(
        `Background workers: ${st.state}`,
        el("p", { class: "muted" }, `Mode: ${st.mode}. Live workers: ${st.workers.length}.`),
        table(
          [
            { label: "Job", get: (j) => el("div", {}, el("strong", {}, j.name), el("div", { class: "muted" }, j.description)) },
            { label: "Last success", get: (j) => when(j.last_success_at) },
            { label: "Next run", get: (j) => (j.paused ? "paused" : when(j.next_run_at)) },
            { label: "Duration", num: true, get: (j) => (j.last_duration_ms !== null ? `${j.last_duration_ms} ms` : "—") },
            { label: "Failures", num: true, get: (j) => fmt(j.consecutive_failures) },
            { label: "Last error", get: (j) => j.last_error || "" },
            {
              label: "",
              get: (j) =>
                el(
                  "span",
                  {},
                  el("button", { class: "secondary", onclick: () => act(`/api/v1/admin/jobs/${j.name}/run`, "POST", "background") }, "Run now"),
                  " ",
                  el("button", { class: "secondary", onclick: () => act(`/api/v1/admin/jobs/${j.name}/${j.paused ? "resume" : "pause"}`, "POST", "background") }, j.paused ? "Resume" : "Pause")
                ),
            },
          ],
          st.jobs
        )
      ),
    ];
  },

  async notices() {
    const notes = await api("/api/v1/notifications?limit=200");
    return [
      card(
        "Notifications",
        table(
          [
            { label: "Time", get: (n) => when(n.created_at) },
            { label: "Severity", get: (n) => pill(n.severity) },
            { label: "Title", get: (n) => el("div", {}, el("strong", {}, n.title), el("div", { class: "muted" }, n.body)) },
            {
              label: "",
              get: (n) =>
                el(
                  "span",
                  {},
                  n.read_at ? null : el("button", { class: "secondary", onclick: () => act(`/api/v1/notifications/${n.id}/read`, "POST", "notices") }, "Mark read"),
                  " ",
                  el("button", { class: "danger", onclick: () => act(`/api/v1/notifications/${n.id}`, "DELETE", "notices") }, "Delete")
                ),
            },
          ],
          notes
        )
      ),
    ];
  },
};

const viewsInner = {
  keys: [],
  async keyTable(keys) {
    const ks = keys || (await api(`/api/v1/projects/${pid()}/keys`));
    return table(
      [
        { label: "Name", get: (k) => k.name || "—" },
        { label: "Prefix", get: (k) => k.prefix },
        { label: "Scopes", get: (k) => k.scopes.join(", ") },
        { label: "Last used", get: (k) => when(k.last_used_at) },
        { label: "Expires", get: (k) => when(k.expires_at) },
        { label: "Status", get: (k) => (k.revoked_at ? pill("revoked") : pill("active")) },
        {
          label: "",
          get: (k) =>
            k.revoked_at || k.id === state.me.key.id
              ? ""
              : el("button", { class: "danger", onclick: () => confirm("Revoke this key?") && act(`/api/v1/projects/${pid()}/keys/${k.id}`, "DELETE", "keys") }, "Revoke"),
        },
      ],
      ks
    );
  },
};

async function act(path, method, back) {
  try {
    await api(path, { method });
  } catch (e) {
    alert(e.message);
  }
  render(back);
}

async function render(name) {
  const view = document.getElementById("view");
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  view.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try {
    view.replaceChildren(...(await views[name]()));
  } catch (e) {
    view.replaceChildren(card(null, errorBox(e)));
  }
}

async function start() {
  try {
    state.me = await api("/api/v1/me");
  } catch (e) {
    sessionStorage.removeItem(KEY);
    document.getElementById("login-error").textContent = e.message;
    return;
  }
  document.getElementById("login").hidden = true;
  document.getElementById("nav").hidden = false;
  document.getElementById("view").hidden = false;
  const who = document.getElementById("who");
  who.replaceChildren(
    `${state.me.user.email} · ${state.me.project.name} `,
    el("button", { class: "secondary", onclick: () => { sessionStorage.removeItem(KEY); location.reload(); } }, "Sign out")
  );
  if (state.me.user.role === "admin") document.querySelector('nav button[data-view="background"]').hidden = false;
  render("overview");
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("login-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    sessionStorage.setItem(KEY, document.getElementById("key-input").value.trim());
    start();
  });
  document.getElementById("nav").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-view]");
    if (b) render(b.dataset.view);
  });
  if (sessionStorage.getItem(KEY)) start();
});
