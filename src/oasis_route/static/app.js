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

const safeLink = (href, text) =>
  typeof href === "string" && href.startsWith("https://")
    ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, text)
    : null;

const OUTCOME_TEXT = {
  connected: "Connected",
  already_connected: "Already connected",
  needs_provider: "Choose provider",
  rejected: "Not allowed",
  invalid: "Key rejected",
  error: "Will retry",
};

// One-step onboarding: paste every key (or a whole .env); providers are detected locally, nothing is guessed.
function quickConnectCard(back) {
  const text = el("textarea", {
    rows: 6,
    autocomplete: "off",
    spellcheck: "false",
    placeholder: "GROQ_API_KEY=gsk_…\nGEMINI_API_KEY=AIza…\nhf_…\n(one key per line, or paste your whole .env)",
  });
  const accept = el("input", { type: "checkbox" });
  const free = el("input", { type: "checkbox" });
  const out = el("div", {});
  let choices = {}; // line number -> provider chosen for keys several providers could have issued
  const providerCell = (r) => {
    if (r.provider_id) return r.provider_id;
    if (!(r.candidates && r.candidates.length) || !r.line) return "unknown";
    const sel = el(
      "select",
      { onchange: (ev) => { if (ev.target.value) choices[r.line] = ev.target.value; else delete choices[r.line]; } },
      el("option", { value: "" }, "Which provider issued it?"),
      r.candidates.map((c) => el("option", { value: c }, c))
    );
    if (choices[r.line]) sel.value = choices[r.line];
    return sel;
  };
  const showResults = (data) => {
    const rows = data.results || [];
    const summary = data.dry_run
      ? `${rows.length} key(s) recognised. Nothing has been stored yet.`
      : `${fmt(data.connected)} connected · ${fmt(data.already_connected)} already connected · ` +
        `${fmt(data.needs_attention)} need attention · ${fmt(data.free_models_available)} free models available`;
    out.replaceChildren(
      el("p", {}, summary),
      data.ignored_lines ? el("p", { class: "muted" }, `${data.ignored_lines} other line(s) ignored (not provider keys).`) : null,
      table(
        [
          { label: "Key", get: (r) => el("code", {}, r.key_hint) },
          { label: "Provider", get: (r) => (data.dry_run ? providerCell(r) : r.provider_id || (r.candidates && r.candidates.length ? `one of: ${r.candidates.join(", ")}` : "unknown")) },
          { label: "Result", get: (r) => (r.outcome ? OUTCOME_TEXT[r.outcome] || r.outcome : `detected by ${r.detected_by}`) },
          { label: "Free models", num: true, get: (r) => (r.outcome === "connected" ? fmt(r.free_models) : "—") },
          {
            label: "Details",
            get: (r) =>
              r.outcome === "needs_provider" || r.detected_by === "ambiguous" || r.detected_by === "unknown"
                ? data.dry_run && r.candidates && r.candidates.length
                  ? "Pick the provider, then press Connect all."
                  : `${r.message || ""} Run Check keys and pick its provider, or write it as MISTRAL_API_KEY=<key>.`
                : r.message || "",
          },
        ],
        rows
      )
    );
  };
  const submit = async (dryRun) => {
    out.replaceChildren(el("p", { class: "muted" }, dryRun ? "Checking…" : "Connecting and validating each key…"));
    try {
      const data = await api(`/api/v1/projects/${pid()}/connections/quick`, {
        method: "POST",
        body: {
          text: text.value,
          accept_terms: accept.checked,
          free_tier: free.checked,
          dry_run: dryRun,
          providers_by_line: choices,
        },
      });
      if (!dryRun) {
        text.value = "";
        choices = {};
      }
      showResults(data);
    } catch (e) {
      out.replaceChildren(errorBox(e));
    }
  };
  return card(
    "Paste your keys",
    el(
      "p",
      { class: "muted" },
      "Paste every provider key you hold. Each key is matched to its provider here, sent only to that provider to be checked, " +
        "then stored encrypted. Models, free tiers, limits and routing are set up automatically."
    ),
    text,
    el("label", { class: "check" }, accept, " These are my own accounts and I accept each provider's terms"),
    el("label", { class: "check" }, free, " No billing is set up on these accounts (free plans or trial keys)"),
    el(
      "p",
      {},
      el("button", { type: "button", class: "secondary", onclick: () => submit(true) }, "Check keys"),
      " ",
      el("button", { type: "button", onclick: async () => { await submit(false); } }, "Connect all")
    ),
    out
  );
}

// Starts an official provider sign-in, opens the provider's page and waits for the flow to finish.
async function startOAuth(provider, acceptTerms, msg, back) {
  const pollFlow = async (flowId, deadline) => {
    if (Date.now() > deadline) return msg.replaceChildren("Sign-in timed out. Try again.");
    try {
      const f = await api(`/api/v1/projects/${pid()}/connections/oauth/${flowId}`);
      if (f.status === "completed") return render(back);
      if (f.status === "failed" || f.status === "expired")
        return msg.replaceChildren(errorBox(new Error(f.error || `Sign-in ${f.status}. Try again.`)));
    } catch (e) {
      return msg.replaceChildren(errorBox(e));
    }
    setTimeout(() => pollFlow(flowId, deadline), 2000);
  };
  try {
    const f = await api(`/api/v1/projects/${pid()}/connections/oauth/start`, {
      method: "POST",
      body: { provider_id: provider.id, accept_terms: acceptTerms },
    });
    if (!String(f.authorization_url).startsWith("https://")) throw new Error("unexpected authorization URL");
    window.open(f.authorization_url, "_blank", "noopener");
    msg.replaceChildren(
      `Waiting for you to approve access on ${provider.name}... `,
      safeLink(f.authorization_url, `Open ${provider.name} again`)
    );
    pollFlow(f.flow_id, Date.now() + 10 * 60 * 1000);
  } catch (e) {
    msg.replaceChildren(errorBox(e));
  }
}

// Device-code sign-in: show a short code the user enters on the provider's site from any device.
async function startDevice(provider, acceptTerms, msg, back) {
  const poll = async (flowId, interval, deadline) => {
    if (Date.now() > deadline) return msg.replaceChildren("The code expired. Try again.");
    try {
      const f = await api(`/api/v1/projects/${pid()}/connections/oauth/${flowId}/poll`, { method: "POST" });
      if (f.status === "completed") return render(back);
      if (f.status !== "pending") return msg.replaceChildren(errorBox(new Error(f.error || `Sign-in ${f.status}. Try again.`)));
      interval = f.interval || interval;
    } catch (e) {
      return msg.replaceChildren(errorBox(e));
    }
    setTimeout(() => poll(flowId, interval, deadline), interval * 1000);
  };
  try {
    const f = await api(`/api/v1/projects/${pid()}/connections/oauth/start`, {
      method: "POST",
      body: { provider_id: provider.id, accept_terms: acceptTerms, mode: "device" },
    });
    msg.replaceChildren(
      el("p", {}, `On any device signed in to ${provider.name}, open `, safeLink(f.verification_uri, f.verification_uri), " and enter:"),
      el("p", {}, el("code", { style: "font-size:1.6em;letter-spacing:.15em" }, f.user_code)),
      el("p", { class: "muted" }, "This page connects automatically once you approve.")
    );
    poll(f.flow_id, f.interval || 5, new Date(f.expires_at).getTime());
  } catch (e) {
    msg.replaceChildren(errorBox(e));
  }
}

// "console.groq.com/…/rate-limits": host plus the last path segment, so two pages on one site differ.
function evidenceLabel(u) {
  const parts = u.replace(/^https:\/\//, "").split("?")[0].split("/").filter(Boolean);
  return parts.length > 1 ? `${parts[0]}/…/${parts[parts.length - 1]}` : parts[0];
}

const PERIOD = { minute: "min", hour: "hour", day: "day", week: "week", month: "month" };
const UNIT = { total_tokens: "tokens", input_tokens: "input tokens", output_tokens: "output tokens" };
function quotaText(q) {
  const scope = q.model_scope && q.model_scope !== "*" ? ` · ${q.model_scope}` : "";
  if (q.unit === "credits_usd_micros")
    return `$${(q.limit / 1e6).toLocaleString(undefined, { maximumFractionDigits: 2 })} credit${q.period === "one_time" ? " (one-time)" : ""}${scope}`;
  const per = q.period && q.period !== "one_time" ? ` / ${PERIOD[q.period] || q.period}` : "";
  return `${fmt(q.limit)} ${UNIT[q.unit] || q.unit || ""}${per}${scope}`;
}

function ago(v) {
  if (!v) return null;
  const s = Math.round((Date.now() - new Date(v).getTime()) / 1000);
  if (s < 0) return `in ${Math.ceil(-s / 60)} min`;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

const ACCESS_LABEL = {
  official_oauth: "Sign-in available",
  api_key: "API key",
  anonymous_public: "No account needed",
};
const BILLING_LABEL = { free_tier: "Free tier", paid: "Paid account", unknown: "Billing unknown", local: "Local" };

function providerCard(p) {
  const msg = el("div", { class: "msg" });
  const tags = [
    el("span", { class: `tag ${p.access_path}` }, ACCESS_LABEL[p.access_path] || p.access_path),
    p.card_required === false
      ? el("span", { class: "tag good", title: `Source: ${p.card_required_source}` }, "No card required")
      : p.card_required === true
        ? el("span", { class: "tag caution", title: `Source: ${p.card_required_source}` }, "Card required")
        : el("span", { class: "tag", title: "Not documented and not yet extracted" }, "Card: unknown"),
    p.models.free ? el("span", { class: "tag good" }, `${fmt(p.models.free)} free models`) : null,
    p.pending_review ? el("span", { class: "tag", title: "Waiting for an administrator to verify" }, `${p.pending_review} unverified`) : null,
  ];
  const offers = p.offers.length
    ? p.offers.map((o) =>
        el(
          "div",
          { class: "offer" },
          el("div", { class: "offer-head" }, el("strong", {}, o.title), " ", pill(o.state)),
          o.quotas.length
            ? el("ul", { class: "quotas" }, o.quotas.map((q) => el("li", {}, quotaText(q))))
            : el("p", { class: "muted" }, "Limits: unknown"),
          o.models.free.length
            ? el("p", { class: "muted small" }, `Free models: ${o.models.free.slice(0, 5).join(", ")}`,
                o.models.free.length > 5 ? ` and ${o.models.free.length - 5} more` : "")
            : null,
          el(
            "p",
            { class: "muted small" },
            o.expires_at ? `Expires ${when(o.expires_at)} · ` : "",
            `Checked ${when(o.last_observed_at)}`,
            o.evidence.urls.length ? " · Evidence: " : "",
            o.evidence.urls.flatMap((u, i) => [i ? ", " : "", safeLink(u, evidenceLabel(u))])
          )
        )
      )
    : [el("p", { class: "muted" }, p.pending_review ? "Offers found, waiting for verification." : "No verified offer yet.")];

  const na = p.next_action;
  let action;
  if (na.kind === "connect_oauth") {
    const accept = el("input", { type: "checkbox" });
    action = el(
      "div",
      { class: "action" },
      el("label", { class: "check" }, accept, " I hold this account and accept the provider's terms"),
      el("button", { type: "button", onclick: () => startOAuth(p, accept.checked, msg, "discover") }, na.label)
    );
  } else if (na.kind === "connect_api_key") {
    const form = el(
      "form",
      {
        class: "keyform",
        hidden: true,
        onsubmit: async (ev) => {
          ev.preventDefault();
          const f = new FormData(ev.target);
          try {
            await api(`/api/v1/projects/${pid()}/connections`, {
              method: "POST",
              body: { provider_id: p.id, api_key: f.get("api_key"), accept_terms: f.get("accept") === "on" },
            });
            render("discover");
          } catch (e) {
            msg.replaceChildren(errorBox(e));
          }
        },
      },
      el("input", { name: "api_key", type: "password", autocomplete: "off", placeholder: `${p.name} API key`, required: true }),
      el("label", { class: "check" }, el("input", { type: "checkbox", name: "accept" }), " I hold this account and accept the provider's terms"),
      el("button", { type: "submit" }, "Connect")
    );
    action = el(
      "div",
      { class: "action" },
      el("p", { class: "muted small" }, na.label, " ", safeLink(na.url, "Get a key")),
      el("button", { type: "button", class: "secondary", onclick: (ev) => { form.hidden = false; ev.target.hidden = true; } }, "Add API key"),
      form
    );
  } else {
    const tone = { connected: "ok", confirm_billing: "warn", fix_connection: "error" }[na.kind] || "muted";
    action = el(
      "div",
      { class: "action" },
      el("p", { class: tone }, na.label),
      na.kind === "confirm_billing"
        ? el("button", {
            type: "button",
            class: "secondary",
            onclick: async () => {
              if (!confirm(`Confirm ${p.name} has no billing attached? Free-only routing will then use it.`)) return;
              try {
                await api(`/api/v1/projects/${pid()}/connections/${na.connection_id}`, { method: "PATCH", body: { billing_mode: "free_tier" } });
                render("discover");
              } catch (e) {
                msg.replaceChildren(errorBox(e));
              }
            },
          }, "It's a free-tier account")
        : safeLink(na.url, "Read the docs")
    );
  }
  const links = Object.entries(p.links)
    .filter(([k]) => ["signup", "pricing", "rate_limits", "docs", "terms"].includes(k))
    .map(([k, v]) => safeLink(v, k.replace("_", " ")));
  return el(
    "article",
    { class: `pcard${p.connection ? " connected" : ""}` },
    el("div", { class: "pcard-head" }, el("h3", {}, p.name), p.connection ? pill(p.connection.status) : null),
    el("div", { class: "tags" }, tags),
    p.free_tier_hint ? el("p", { class: "hint" }, p.free_tier_hint) : null,
    el("div", { class: "offers" }, offers),
    el(
      "p",
      { class: "muted small" },
      p.models.total ? `${fmt(p.models.total)} models in the catalogue` : "Models sync automatically once connected",
      p.connection ? ` · ${BILLING_LABEL[p.connection.billing_mode] || p.connection.billing_mode}` : ""
    ),
    el("div", { class: "links" }, links),
    action,
    msg
  );
}

// ---------------------------------------------------------------------------------------------- views
const MODES = ["strict_quality", "balanced", "lowest_cost", "most_quota", "lowest_latency", "reliability", "context_fit", "reset_aware", "diversity"];
const usd = (v) => (v === null || v === undefined ? "—" : `$${Number(v).toFixed(6).replace(/0+$/, "").replace(/\.$/, "")}`);

// Explanation of a route decision or preview: selected route and why, cost and its evidence, quota,
// alternatives, rejections grouped by gate, and the fallback chain with each attempt.
function explainBlock(e) {
  if (!e) return el("p", { class: "muted" }, "No explanation recorded.");
  const rows = [];
  const kv = (k, v) => rows.push(el("tr", {}, el("th", {}, k), el("td", {}, v)));
  const s = e.selected;
  if (s) {
    kv("Selected", `${s.route} (${e.strategy})`);
    kv("Why", s.reason || "—");
    const c = s.cost || {};
    kv("Cost", `${c.status} · estimated ${usd(c.usd)} · basis ${c.cost_basis}${c.price_verified ? " · price verified" : " · price unverified"}`);
  } else kv("Selected", el("span", { class: "warn" }, "none: no route passed every gate"));
  const v = e.verification || {};
  if (v.cost_basis) kv("Free evidence", v.free ? v.free_evidence || v.cost_basis : "not free");
  const q = e.quota || {};
  if ("metered" in q) kv("Quota", q.metered ? `${q.remaining_fraction === null ? "unknown" : Math.round(q.remaining_fraction * 100) + "%"} left${q.reset_in_s ? ` · resets in ${Math.round(q.reset_in_s)}s` : ""}` : "unmetered");
  const sp = e.spend || {};
  if (sp.mode) kv("Spend", `${sp.mode}${sp.paid ? " · PAID" : " · free"}${sp.label ? ` · ${sp.label}` : ""}`);
  if (e.actual_cost) kv("Actual cost", `${usd(e.actual_cost.usd)} (${e.actual_cost.status}, ${e.actual_cost.source})`);
  const fc = e.fallback_chain || {};
  if (fc.planned && fc.planned.length) kv("Fallback chain", fc.planned.join(" → "));
  const parts = [el("table", { class: "kv" }, el("tbody", {}, rows))];
  if (fc.attempts && fc.attempts.length)
    parts.push(
      el("h3", {}, "Attempts"),
      table(
        [
          { label: "Route", get: (a) => `${a.provider}/${a.model}` },
          { label: "Outcome", get: (a) => pill(a.outcome) },
          { label: "Category", get: (a) => a.category || "" },
          { label: "Retry in", get: (a) => (a.retry_in_s ? `${Math.round(a.retry_in_s)}s` : "") },
        ],
        fc.attempts
      )
    );
  if (e.alternatives && e.alternatives.length)
    parts.push(
      el("h3", {}, "Alternatives"),
      table(
        [
          { label: "Route", get: (a) => a.route },
          { label: "Quality", num: true, get: (a) => fmt(a.quality ?? a.prior) },
          { label: "Cost", get: (a) => (a.cost || {}).status },
        ],
        e.alternatives.slice(0, 10)
      )
    );
  if (e.rejected && e.rejected.length)
    parts.push(
      el("h3", {}, "Rejected"),
      table(
        [
          { label: "Gate", get: (g) => g.gate },
          { label: "Reason", get: (g) => g.reason },
          { label: "Routes", num: true, get: (g) => g.count },
          { label: "Examples", get: (g) => g.routes.slice(0, 3).map((r) => r.route).join(", ") },
        ],
        e.rejected
      )
    );
  return el("div", {}, parts);
}

function cooldownsOf(cap, conns) {
  const out = [];
  for (const a of cap.allocations || [])
    for (const d of a.dimensions || [])
      for (const p of d.pools || []) if (p.exhausted_until) out.push({ kind: "quota", scope: `${a.provider_id}/${a.model_scope || "*"}`, detail: d.unit, until: p.exhausted_until });
  for (const c of conns || [])
    for (const h of c.health || []) if (h.open_until) out.push({ kind: "circuit", scope: `${c.provider_id}/${h.model_scope}`, detail: h.last_error_category || "", until: h.open_until });
  return out;
}

async function explainRequest(requestId) {
  const box = document.getElementById("explain-box");
  if (!box) return;
  box.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  try {
    const d = await api(`/api/v1/projects/${pid()}/route-decisions/${encodeURIComponent(requestId)}`);
    const receipt = await api(`/api/v1/projects/${pid()}/receipts/${encodeURIComponent(requestId)}`).catch(() => null);
    box.replaceChildren(
      card(
        `Request ${requestId}`,
        el("p", { class: "muted" }, `Asked for ${d.requested_model} · task ${d.task} · ${d.outcome} · served by ${d.served_provider ? `${d.served_provider}/${d.served_model}` : "nothing"}${d.fallback_used ? " · fallback used" : ""}`),
        explainBlock(d.explanation),
        receipt
          ? el(
              "p",
              { class: "muted spaced" },
              `Receipt: ${fmt(receipt.input_tokens)} in / ${fmt(receipt.output_tokens)} out (${receipt.usage_source}) · estimated ${usd(receipt.estimated_usd)} · reserved ${usd(receipt.reserved_usd)} · actual ${usd(receipt.actual_usd)} · digest ${receipt.digest_valid ? "valid" : "INVALID"}`
            )
          : el("p", { class: "muted spaced" }, "No receipt: the request was refused before any reservation.")
      )
    );
    box.scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    box.replaceChildren(card(null, errorBox(e)));
  }
}

const views = {
  // First screen: what works right now, and the fewest steps to real, free models.
  async start() {
    const d = await api(`/api/v1/projects/${pid()}/setup`);
    const rs = d.routes.summary;
    const real = rs.eligible_free > 0;
    const demoOnly = !real && rs.eligible_demo > 0;
    const status = card(
      null,
      el("h1", { class: real ? "ok" : "warn" }, real ? "Ready: free models are connected" : demoOnly ? "Demo mode: replies are echoes" : "Connect a model to start"),
      el(
        "p",
        {},
        real
          ? `${fmt(rs.eligible_free)} free route${rs.eligible_free === 1 ? "" : "s"} on real models. Requests with model "auto" go to the best one with quota left.`
          : demoOnly
            ? "Only the built-in demo provider is connected, so answers are canned echoes. Add a free key or a local model below: it takes about a minute."
            : "No model can answer yet. Add a free key or a local model below."
      ),
      el("p", { class: "muted" }, `Spend policy ${rs.spend_policy}: nothing paid is used without your explicit approval.`)
    );

    const testOut = el("div", {});
    const testCard = card(
      "Send a test message",
      el("p", { class: "muted" }, 'Sends "Say hello in five words." with model auto and shows who answered.'),
      el(
        "button",
        {
          type: "button",
          onclick: async () => {
            testOut.replaceChildren(el("p", { class: "muted" }, "Sending…"));
            try {
              const r = await api("/v1/chat/completions", {
                method: "POST",
                body: { model: "auto", messages: [{ role: "user", content: "Say hello in five words." }], max_tokens: 64 },
              });
              const m = r.oasis_route || {};
              const sp = m.spend || {};
              const parts = [
                el("p", {}, el("strong", {}, `${m.provider || "?"} / ${m.model || r.model}`), sp.paid ? " · PAID" : " · free", m.fallback ? " · after fallback" : ""),
                el("blockquote", {}, ((r.choices || [])[0] || {}).message ? r.choices[0].message.content : "(empty reply)"),
                ["mock", "mock_alt"].includes(m.provider) ? el("p", { class: "warn" }, "That was the demo provider. Connect a real one below.") : null,
              ];
              testOut.replaceChildren(...parts.filter(Boolean));
            } catch (e) {
              testOut.replaceChildren(errorBox(e));
            }
          },
        },
        "Send test message"
      ),
      testOut
    );

    const missing = d.free_keys.filter((k) => !k.connected);
    const keysCard = card(
      "1 · Get free keys (no card)",
      el("p", { class: "muted" }, "Open each link, sign in, create a key, and paste it in step 2. Each has its own free daily limits; connecting several gives you more."),
      missing.length
        ? table(
            [
              { label: "Provider", get: (k) => k.name },
              { label: "Free tier", get: (k) => k.free_tier_hint || "" },
              { label: "", get: (k) => safeLink(k.get_key_url, "Create key ↗") },
            ],
            missing
          )
        : el("p", { class: "ok" }, "All listed providers are connected."),
      d.free_keys.some((k) => k.connected)
        ? el("p", { class: "muted small" }, `Connected: ${d.free_keys.filter((k) => k.connected).map((k) => k.name).join(", ")}`)
        : null
    );

    const qc = quickConnectCard("start");
    qc.querySelector("h2").textContent = "2 · Paste your keys";
    qc.append(el("p", {}, el("button", { type: "button", class: "secondary", onclick: () => render("start") }, "Refresh status")));

    const attention = d.billing_attention.length
      ? card(
          "Confirm free-plan accounts",
          el("p", { class: "muted" }, "These accounts are connected, but their routes are held back until you say whether billing is set up."),
          d.billing_attention.map((a) =>
            el(
              "p",
              {},
              el("strong", {}, a.name), ` · ${a.blocked_routes} routes waiting `,
              el("button", {
                type: "button",
                onclick: async () => {
                  if (!confirm(`No billing (card or credits) is set up on ${a.name}? Free-only routing will then use it.`)) return;
                  await api(`/api/v1/projects/${pid()}/connections/${a.connection_id}`, { method: "PATCH", body: { billing_mode: "free_tier" } });
                  render("start");
                },
              }, "No billing: use it free")
            )
          )
        )
      : null;

    const local = d.local_servers;
    const found = (local.servers || []).filter((x) => x.reachable);
    const localMsg = el("div", {});
    const localCard = card(
      "Or use a local model",
      !local.enabled
        ? el("p", { class: "muted" }, `Detection is off: ${local.reason}`)
        : found.length
          ? [
              table(
                [
                  { label: "Server", get: (x) => x.provider_id },
                  { label: "Address", get: (x) => el("code", {}, x.base_url) },
                  { label: "Models", num: true, get: (x) => fmt(x.models.length) },
                  { label: "", get: (x) => (x.connected ? pill("active") : "found") },
                ],
                found
              ),
              found.some((x) => !x.connected)
                ? el("button", {
                    type: "button",
                    onclick: async () => {
                      localMsg.replaceChildren(el("p", { class: "muted" }, "Connecting…"));
                      try {
                        await api(`/api/v1/projects/${pid()}/setup`, { method: "POST", body: { connect_local: true } });
                        render("start");
                      } catch (e) {
                        localMsg.replaceChildren(errorBox(e));
                      }
                    },
                  }, "Connect local models")
                : null,
            ]
          : el("p", { class: "muted" }, "None running. Install ", safeLink("https://ollama.com", "Ollama"), ", run ", el("code", {}, "ollama pull qwen2.5"), ", then refresh. Free and unlimited, but runs on your own hardware."),
      localMsg
    );

    const ep = d.endpoint;
    const useCard = card(
      "3 · Use it from your apps",
      el("p", { class: "muted" }, "Any OpenAI-compatible client works. Your Oasis Key is the API key."),
      el("pre", {}, `OPENAI_BASE_URL=${ep.base_url}\nOPENAI_API_KEY=<your Oasis Key>\nmodel: auto`),
      el("p", {}, el("button", { type: "button", class: "secondary", onclick: () => render("integrations") }, "Settings for Claude Code, Cursor, Aider, …"))
    );
    return [status, testCard, attention, keysCard, qc, localCard, useCard];
  },

  async discover() {
    const d = await api(`/api/v1/discover?project_id=${encodeURIComponent(pid())}`);
    const st = d.status;
    const kpi = (l, v, sub) => el("div", { class: "kpi" }, el("div", { class: "v" }, fmt(v)), el("div", { class: "l" }, l), sub ? el("div", { class: "l" }, sub) : null);
    const filters = { q: "", free: false, oauth: false, nocard: false, connected: false, all: false };
    const grid = el("div", { class: "pgrid" });
    const count = el("span", { class: "muted" });
    const draw = () => {
      const q = filters.q.toLowerCase();
      const rows = d.providers.filter(
        (p) =>
          (!q || p.name.toLowerCase().includes(q) || p.id.includes(q) || (p.free_tier_hint || "").toLowerCase().includes(q)) &&
          (!filters.free || p.models.free > 0 || p.offers.some((o) => o.verified)) &&
          (!filters.oauth || p.oauth_available) &&
          (!filters.nocard || p.card_required === false) &&
          (!filters.connected || p.connection)
      );
      // Connected and connectable providers first; blocked/unsupported ones only on request.
      const usable = (p) => p.connection || ["connect_api_key", "connect_oauth", "connected", "confirm_billing", "fix_connection"].includes(p.next_action.kind);
      rows.sort((a, b) => Number(!!b.connection) - Number(!!a.connection) || Number(usable(b)) - Number(usable(a)));
      const searching = q || filters.free || filters.oauth || filters.nocard || filters.connected;
      const shown = filters.all || searching ? rows : rows.filter(usable).slice(0, 24);
      count.replaceChildren(`${shown.length} of ${rows.length} providers`);
      grid.replaceChildren(
        ...(shown.length ? shown.map(providerCard) : [el("p", { class: "muted" }, "No provider matches these filters.")]),
        shown.length < rows.length
          ? el("p", {}, el("button", { type: "button", class: "secondary", onclick: () => { filters.all = true; draw(); } }, `Show all ${rows.length} providers`))
          : null
      );
    };
    const chip = (key, label) =>
      el("label", { class: "chip" }, el("input", { type: "checkbox", onchange: (ev) => { filters[key] = ev.target.checked; draw(); } }), ` ${label}`);
    const bar = el(
      "div",
      { class: "filters" },
      el("input", { type: "search", placeholder: "Search providers", "aria-label": "Search providers", oninput: (ev) => { filters.q = ev.target.value; draw(); } }),
      chip("free", "Free capacity"),
      chip("oauth", "Sign-in available"),
      chip("nocard", "No card"),
      chip("connected", "Connected"),
      count
    );
    draw();
    const proj = st.project || {};
    return [
      card(
        null,
        el("h1", {}, "Discover free AI capacity"),
        el(
          "p",
          { class: "muted" },
          "Providers, what they really require, and offers verified from official sources. Unknown means not yet confirmed, never assumed. ",
          proj.spend_policy === "free_only" ? "Your project is in free-only mode, so nothing here can bill you without your confirmation." : "Your project allows paid routes."
        )
      ),
      el(
        "div",
        { class: "kpis" },
        kpi("Verified offers", st.offers.verified, `${fmt(st.offers.pending_review)} waiting for review`),
        kpi("Your free-tier connections", proj.free_tier_connections, `${fmt(proj.connected)} connected in total`),
        kpi("Sources monitored", st.sources.approved, st.sources.failing ? `${st.sources.failing} failing, retrying with backoff` : "all healthy"),
        kpi("Last scan", ago(st.last_run_at) || "not yet", st.next_scan_at ? (new Date(st.next_scan_at) <= new Date() ? "next scan due now" : `next scan ${ago(st.next_scan_at)}`) : null)
      ),
      bar,
      grid,
    ];
  },

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
    const [cap, conns, budgets] = await Promise.all([
      api(`/api/v1/projects/${pid()}/capacity`),
      api(`/api/v1/projects/${pid()}/connections`),
      api(`/api/v1/projects/${pid()}/budgets`),
    ]);
    const cds = cooldownsOf(cap, conns);
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
        "Active cooldowns",
        el("p", { class: "muted" }, "Routes resting after a rate limit or repeated errors. They are skipped, not retried, until the time shown."),
        table(
          [
            { label: "Kind", get: (x) => x.kind },
            { label: "Scope", get: (x) => x.scope },
            { label: "Detail", get: (x) => x.detail },
            { label: "Until", get: (x) => when(x.until) },
          ],
          cds,
          "None."
        )
      ),
      card(
        `Budgets (spend policy ${budgets.spend_policy})`,
        table(
          [
            { label: "Period", get: (b) => b.period },
            { label: "Limit", num: true, get: (b) => usd(b.limit_usd) },
            { label: "Used", num: true, get: (b) => usd(b.used_usd) },
            { label: "Reserved", num: true, get: (b) => usd(b.reserved_usd) },
            { label: "Remaining", num: true, get: (b) => usd(b.remaining_usd) },
            { label: "Resets", get: (b) => when(b.resets_at) },
            { label: "Exhausts", get: (b) => when((b.forecast || {}).exhausts_at || b.exhausts_at) },
          ],
          budgets.budgets,
          "No USD caps set. Zero-budget projects never spend; caps bound bounded-autonomy and continuity modes."
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
    // Only providers that can actually be connected; hand-written adapters first, then by name.
    const isCurated = (p) => !(p.catalog || {}).source || (p.catalog || {}).curated_override;
    const connectable = providers
      .filter((p) => p.status === "active" && !["mock", "mock_alt"].includes(p.id))
      .sort((a, b) => a.name.localeCompare(b.name));
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
      el(
        "label",
        {},
        "Provider",
        el(
          "select",
          { name: "provider", required: true },
          el("option", { value: "" }, "Choose a provider"),
          el("optgroup", { label: "Reviewed adapters" }, connectable.filter(isCurated).map((p) => el("option", { value: p.id }, p.name))),
          el("optgroup", { label: "Catalogue (advertised, unverified)" }, connectable.filter((p) => !isCurated(p)).map((p) => el("option", { value: p.id }, p.name)))
        )
      ),
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
    const oauthMsg = el("p", {});
    const oauthAccept = el("input", { type: "checkbox" });
    const oauthProviders = providers.filter((p) => p.oauth_available);
    const oauthCard = oauthProviders.length
      ? card(
          "Sign in with a provider",
          el("p", { class: "muted" }, "No key to copy: approve access on the provider's own site and come back. The connection is checked and ready automatically."),
          el("label", {}, el("span", {}, oauthAccept, " I own this account and accept the provider's terms")),
          el(
            "p",
            {},
            oauthProviders.map((p) =>
              el(
                "button",
                {
                  type: "button",
                  onclick: () => startOAuth(p, oauthAccept.checked, oauthMsg, "connections"),
                },
                `Sign in with ${p.name}`
              )
            ),
            oauthProviders
              .filter((p) => p.oauth_device)
              .map((p) =>
                el(
                  "button",
                  { type: "button", class: "secondary", onclick: () => startDevice(p, oauthAccept.checked, oauthMsg, "connections") },
                  `${p.name} with a code`
                )
              )
          ),
          oauthMsg
        )
      : null;
    return [
      quickConnectCard("connections"),
      oauthCard,
      card("Connect one provider", termsList, form, msg),
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
              el("p", { class: "muted" }, `Mode ${r.mode}, task ${r.task}, spend mode ${r.spend_mode}, best quality ${fmt(r.best_quality)}, max fallback hops ${r.max_fallback_hops}.`),
              explainBlock(r.explanation),
              el("h2", {}, "Candidates, in order"),
              table(
                [
                  { label: "#", get: (c) => r.candidates.indexOf(c) + 1 },
                  { label: "Provider", get: (c) => c.provider_id },
                  { label: "Model", get: (c) => c.upstream_model },
                  { label: "Quality", num: true, get: (c) => fmt(c.quality) },
                  { label: "Cost", get: (c) => `${c.cost_status}${c.cost_status === "free" ? "" : ` · ${usd((c.est_cost_usd_micros || 0) / 1e6)}`}` },
                  { label: "Downgrade", get: (c) => (r.downgrade_flags[r.candidates.indexOf(c)] ? el("span", { class: "warn" }, "yes") : "no") },
                ],
                r.candidates,
                "No eligible candidate."
              ),
              el("h2", { class: "spaced" }, "Rejected"),
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
      el("label", {}, "Mode", el("select", { name: "mode" }, MODES.map((m) => el("option", {}, m)))),
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
            { label: "", get: (d) => el("button", { class: "secondary", onclick: () => explainRequest(d.request_id) }, "Explain") },
          ],
          decisions
        )
      ),
      el("div", { id: "explain-box" }),
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

  async routes() {
    const data = await api(`/api/v1/projects/${pid()}/routes`);
    const s = data.summary;
    return [
      card(
        "Routes",
        el("p", { class: "muted" }, `${s.total} routes · ${s.eligible_free} eligible free · spend policy ${s.spend_policy}. A route is free only with evidence (local server, verified price list, or a declared free-tier account); paid and unknown routes are never used under zero-budget.`),
        s.unconfirmed_catalog_models ? el("p", { class: "muted" }, `${s.unconfirmed_catalog_models} catalogue models await confirmation by a connected account's model list.`) : null,
        table(
          [
            { label: "Route", get: (r) => r.route },
            { label: "Cost", get: (r) => pill(r.cost_class) },
            { label: "Evidence", get: (r) => r.free_evidence || r.cost_basis || "—" },
            { label: "State", get: (r) => pill(r.state) },
            { label: "Reason", get: (r) => (r.reason || "") + (r.retry_in_s ? ` (retry in ${Math.round(r.retry_in_s)}s)` : "") },
          ],
          data.routes,
          "No routes yet. Connect a provider first."
        )
      ),
    ];
  },

  async spend() {
    const [budgets, receipts] = await Promise.all([
      api(`/api/v1/projects/${pid()}/budgets`),
      api(`/api/v1/projects/${pid()}/receipts`),
    ]);
    const list = Array.isArray(receipts) ? receipts : receipts.receipts || [];
    return [
      card(
        `Spend mode: ${budgets.spend_policy}`,
        el("p", { class: "muted" }, "Every request holds its worst-case cost before it is sent and settles to the actual cost once. Receipts carry a tamper-evident digest."),
        table(
          [
            { label: "Period", get: (b) => b.period },
            { label: "Limit", num: true, get: (b) => usd(b.limit_usd) },
            { label: "Used", num: true, get: (b) => usd(b.used_usd) },
            { label: "Reserved", num: true, get: (b) => usd(b.reserved_usd) },
            { label: "Remaining", num: true, get: (b) => usd(b.remaining_usd) },
            { label: "Resets", get: (b) => when(b.resets_at) },
          ],
          budgets.budgets,
          "No USD caps set."
        )
      ),
      card(
        "Receipts",
        table(
          [
            { label: "Time", get: (r) => when(r.created_at) },
            { label: "Route", get: (r) => (r.provider_id ? `${r.provider_id}/${r.upstream_model}` : "—") },
            { label: "Outcome", get: (r) => pill(r.outcome) },
            { label: "Cost", get: (r) => r.cost_status },
            { label: "Estimated", num: true, get: (r) => usd(r.estimated_usd) },
            { label: "Actual", num: true, get: (r) => usd(r.actual_usd) },
            { label: "Tokens", num: true, get: (r) => `${fmt(r.input_tokens)} / ${fmt(r.output_tokens)}` },
            { label: "Digest", get: (r) => (r.digest_valid ? "valid" : el("span", { class: "warn" }, "invalid")) },
            { label: "", get: (r) => el("button", { class: "secondary", onclick: () => explainRequest(r.request_id) }, "Explain") },
          ],
          list,
          "No requests yet."
        )
      ),
      el("div", { id: "explain-box" }),
    ];
  },

  async integrations() {
    const data = await api("/api/v1/integrations");
    const out = el("div", {});
    const show = async (id) => {
      try {
        const r = await api(`/api/v1/integrations/${id}`);
        out.replaceChildren(
          card(
            r.name,
            el("p", { class: "muted" }, `Protocol ${r.protocol} · endpoint ${r.endpoint} · model ${r.model}`),
            el("pre", {}, r.text),
            r.docs_url ? el("p", {}, el("a", { href: r.docs_url, rel: "noopener noreferrer", target: "_blank" }, "Official setup docs")) : null
          )
        );
      } catch (e) {
        out.replaceChildren(errorBox(e));
      }
    };
    return [
      card(
        "Connect a coding tool",
        el("p", { class: "muted" }, `Everything points at ${data.base_url}. Settings use $OASIS_API_KEY; create a key under Oasis Keys. CLI: oasis-route integrate <tool> --check.`),
        el("div", { class: "row" }, data.integrations.map((r) => el("button", { class: "secondary", onclick: () => show(r.id) }, r.name)))
      ),
      out,
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
    view.replaceChildren(...[await views[name]()].flat().filter(Boolean));
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
  render("start");
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
  // One-click sign-in: `oasis-route dev` prints /console/#key=<key>. The fragment never reaches the server;
  // it is moved to sessionStorage and removed from the address bar and history entry at once.
  const m = location.hash.match(/(?:^#|&)key=([^&]+)/);
  if (m) {
    sessionStorage.setItem(KEY, decodeURIComponent(m[1]).trim());
    history.replaceState(null, "", location.pathname + location.search);
  }
  if (sessionStorage.getItem(KEY)) start();
});
