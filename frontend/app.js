window.BANKFLOW_CONFIG = {
  apiBase: "http://localhost:8000",
  mlflowUrl: "http://localhost:5000",
  grafanaUrl: "http://localhost:3000",
};

const cfg = window.BANKFLOW_CONFIG;
const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const money = (p) =>
  new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(
    p / 100,
  );
const names = {
  deposit: "Deposit",
  withdrawal: "Withdrawal",
  card_purchase: "Card purchase",
  card_repayment: "Card repayment",
  reversal: "Reversal",
};
let categories = [],
  currentView = "account",
  pendingTransaction = null;
const reverseKeys = new Map();
function notice(message, error = false, view = currentView) {
  if (view !== currentView) return;
  const banner = $("notice");
  banner.textContent = message;
  banner.className = error ? "error" : "";
  banner.hidden = !message;
}
async function api(path, method = "GET", body) {
  let response;
  try {
    response = await fetch(cfg.apiBase + path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error(
      "Cannot reach the backend. Check the API address and that the backend is running.",
    );
  }
  const data = await response.json();
  if (!response.ok) {
    const msg =
      typeof data.detail === "string"
        ? data.detail
        : "Check the entered values.";
    const err = new Error(msg);
    err.definitive = response.status < 500;
    throw err;
  }
  return data;
}
function paise(raw) {
  if (!/^\d+(\.\d{1,2})?$/.test(raw))
    throw new Error("Enter a positive amount with at most two decimal places.");
  const [a, b = ""] = raw.split(".");
  const value = Number(a) * 100 + Number(b.padEnd(2, "0"));
  if (!Number.isSafeInteger(value) || value <= 0 || value > 100000000)
    throw new Error("Amount must be between ₹0.01 and ₹10,00,000.");
  return value;
}
async function refreshAccount() {
  const a = await api("/api/account");
  $("balance").textContent = money(a.balance_paise);
  $("card-due").textContent = money(a.card_due_paise);
  $("card-available").textContent =
    money(a.card_available_paise) +
    " available of " +
    money(a.card_limit_paise);
  $("card-meter").style.width =
    (100 * a.card_due_paise) / a.card_limit_paise + "%";
  $("transaction-count").textContent = a.transactions.length;
  $("ledger-body").innerHTML = a.transactions.length
    ? a.transactions
        .map(
          (t) =>
            `<tr><td><strong>${esc(names[t.kind])}</strong><small>${esc(t.memo || "#" + t.id)} ${t.reversed ? "· Reversed" : ""}${t.reverses ? "· Original #" + t.reverses : ""}</small></td><td class="amount">${money(t.amount_paise)}</td><td>${esc(new Date(t.created_at).toLocaleString())}</td><td>${t.kind !== "reversal" && !t.reversed ? `<button class="secondary reverse" data-id="${t.id}">Reverse</button>` : "—"}</td></tr>`,
        )
        .join("")
    : '<tr><td colspan="4" class="empty">No transactions yet. Make a deposit to begin.</td></tr>';
  document.querySelectorAll(".reverse").forEach(
    (btn) =>
      (btn.onclick = async () => {
        if (
          !confirm(
            "Reverse this transaction? Its original entry will stay in history.",
          )
        )
          return;
        btn.disabled = true;
        const id = btn.dataset.id;
        if (!reverseKeys.has(id)) reverseKeys.set(id, crypto.randomUUID());
        try {
          await api("/api/transactions/" + id + "/reverse", "POST", {
            request_key: reverseKeys.get(id),
          });
          notice("Transaction reversed.", false, "account");
          await refreshAccount();
        } catch (e) {
          notice(e.message, true, "account");
          btn.disabled = false;
          if (e.definitive) reverseKeys.delete(id);
        }
      }),
  );
}
$("transaction-form").onsubmit = async (e) => {
  e.preventDefault();
  const btn = $("transaction-submit");
  btn.disabled = true;
  try {
    const payload = {
      kind: $("kind").value,
      amount_paise: paise($("amount").value.trim()),
      memo: $("memo").value,
    };
    const fingerprint = JSON.stringify(payload);
    if (!pendingTransaction || pendingTransaction.fingerprint !== fingerprint)
      pendingTransaction = { fingerprint, key: crypto.randomUUID() };
    const result = await api("/api/transactions", "POST", {
      ...payload,
      request_key: pendingTransaction.key,
    });
    pendingTransaction = null;
    notice(
      result.replayed
        ? "Existing transaction confirmed; no duplicate entry."
        : "Transaction completed.",
      false,
      "account",
    );
    $("amount").value = "";
    $("memo").value = "";
    await refreshAccount();
  } catch (err) {
    notice(err.message, true, "account");
    if (err.definitive) pendingTransaction = null;
  } finally {
    btn.disabled = false;
  }
};
async function refreshTickets() {
  const tickets = await api("/api/tickets");
  $("tickets").innerHTML = tickets.length
    ? tickets
        .map(
          (t) =>
            `<div class="ticket"><div class="ticket-top"><span class="tag">#${t.id} · ${esc(t.corrected_queue || t.queue)}</span><span class="tag ${t.needs_review ? "review" : ""}">${t.needs_review ? "Needs review" : "Classified"}</span></div><p>${esc(t.text)}</p><small>${esc(t.corrected_category ? "Reviewed: " + t.corrected_category + " · Original: " + t.category : t.category)} · ${(t.confidence * 100).toFixed(1)}% model score${t.corrected_category ? " · Corrected" : ""}</small><div class="correction"><select aria-label="Correct category for ticket ${t.id}" id="category-${t.id}">${categories.map((c) => `<option ${c === (t.corrected_category || t.category) ? "selected" : ""}>${esc(c)}</option>`).join("")}</select><button class="secondary correct" data-id="${t.id}">Save correction</button></div></div>`,
        )
        .join("")
    : '<p class="empty">No support requests yet.</p>';
  document.querySelectorAll(".correct").forEach(
    (btn) =>
      (btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api("/api/tickets/" + btn.dataset.id, "PATCH", {
            category: $("category-" + btn.dataset.id).value,
          });
          notice(
            "Review saved. Eligible feedback can be used by the monitored retraining pipeline.",
            false,
            "support",
          );
          await refreshTickets();
        } catch (e) {
          notice(e.message, true, "support");
          btn.disabled = false;
        }
      }),
  );
}
$("support-form").onsubmit = async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button");
  btn.disabled = true;
  try {
    const p = await api("/api/tickets", "POST", { text: $("query").value });
    $("prediction").hidden = false;
    $("prediction").innerHTML =
      `<span class="tag ${p.needs_review ? "review" : ""}">${p.needs_review ? "Agent review suggested" : "Suggested route"}</span><h3>${esc(p.queue)}</h3><p>${esc(p.category)}</p><strong>${(p.confidence * 100).toFixed(1)}% model score</strong>`;
    await refreshTickets();
  } catch (err) {
    notice(err.message, true, "support");
  } finally {
    btn.disabled = false;
  }
};
function metric(label, value) {
  return `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
}

const shortRun = (uri) => (uri ? uri.split("/")[1]?.slice(0, 12) : "—");
const percent = (value) =>
  Number.isFinite(value) ? (value * 100).toFixed(1) + "%" : "—";

async function refreshOperations() {
  const p = await api("/api/pipeline");
  $("run-pipeline").disabled = p.running;
  $("run-pipeline").textContent = p.running
    ? "Pipeline running…"
    : "Run pipeline";
  const evaluation = p.metrics;
  $("evaluation").innerHTML = evaluation
    ? [
        metric("Test accuracy", percent(evaluation.metrics.test_accuracy)),
        metric("Test macro-F1", evaluation.metrics.test_macro_f1.toFixed(3)),
        metric(
          "Quality gate",
          evaluation.quality_gate_passed ? "Passed" : "Failed",
        ),
        metric("Test queries", evaluation.metrics.test_rows),
      ].join("")
    : '<p class="empty">Run the pipeline to see evaluation results.</p>';

  const serving = p.serving;
  $("deployment-state").innerHTML = serving
    ? [
        metric("Serving status", serving.status),
        metric("Active run", shortRun(serving.active_uri)),
        metric("Requested run", shortRun(serving.requested_uri)),
        metric("Previous run", shortRun(serving.previous_uri)),
      ].join("")
    : '<p class="empty">The model service has yet to load a version.</p>';

  const prep = p.preparation;
  $("data-preparation").innerHTML = prep
    ? [
        metric("Cleaned training queries", prep.rows.train),
        metric("Held-out queries", prep.rows.test),
        metric("Ray preparation", prep.seconds.toFixed(2) + " s"),
        metric(
          "Preparation throughput",
          Math.round(prep.rows_per_second) + " rows/s",
        ),
      ].join("")
    : '<p class="empty">Data preparation metrics appear after the first run.</p>';

  const monitor = p.monitoring;
  $("input-monitoring").innerHTML = monitor
    ? [
        metric(
          "Input drift",
          monitor.ready
            ? monitor.drift
              ? "Detected"
              : "Within thresholds"
            : "Collecting samples",
        ),
        metric("Recent queries", monitor.samples),
        metric("Length divergence", monitor.length_jsd?.toFixed(3) ?? "—"),
        metric("Unknown-token change", percent(monitor.unknown_delta)),
        metric("Reviewed agreement", percent(monitor.reviewed_agreement)),
        metric("Reviewed queries for active model", monitor.reviewed_samples),
        metric("Last automatic trigger", p.retraining?.last_reason ?? "—"),
      ].join("")
    : '<p class="empty">Waiting for the monitoring worker.</p>';

  $("pipeline-runs").innerHTML = p.runs.length
    ? p.runs
        .map((run) => {
          const stages = run.stages
            .map((stage) => {
              const elapsed = Number.isFinite(stage.seconds)
                ? " · " + stage.seconds.toFixed(1) + " s"
                : "";
              const mode = stage.mode ? " · " + stage.mode : "";
              return `<small>${esc(stage.name)} · ${esc(stage.status + mode + elapsed)}</small>`;
            })
            .join("<br>");
          return `<div class="ticket">
      <span class="tag">${esc(run.status)}</span>
      <p>${esc(run.started_at)}${Number.isFinite(run.seconds) ? " · " + run.seconds.toFixed(1) + " s total" : ""}</p>
      ${stages}
      <p class="muted">Reviewed training queries · ${esc(run.reviewed_training_rows ?? "—")}</p>
      ${run.error ? `<p>${esc(run.error)}</p>` : ""}
    </div>`;
        })
        .join("")
    : '<p class="empty">No pipeline runs recorded.</p>';
}
$("run-pipeline").onclick = async () => {
  const btn = $("run-pipeline");
  btn.disabled = true;
  try {
    await api("/api/pipeline/run", "POST");
    notice("Pipeline requested. Refreshing its status…", false, "operations");
    setTimeout(
      () =>
        refreshOperations().catch((e) => notice(e.message, true, "operations")),
      1500,
    );
  } catch (e) {
    notice(e.message, true, "operations");
    btn.disabled = false;
  }
};
document.querySelectorAll("nav button").forEach(
  (btn) =>
    (btn.onclick = () => {
      const view = btn.dataset.view;
      if (view !== currentView) notice("");
      currentView = view;
      document
        .querySelectorAll("main>section")
        .forEach((s) => (s.hidden = s.id !== currentView + "-view"));
      document
        .querySelectorAll("nav button")
        .forEach((b) => b.classList.toggle("selected", b === btn));
      $("page-title").textContent = {
        account: "Account overview",
        support: "Support inbox",
        operations: "Operations",
      }[currentView];
      location.hash = currentView;
      const load = {
        account: refreshAccount,
        support: refreshTickets,
        operations: refreshOperations,
      }[currentView];
      load().catch((e) => notice(e.message, true, view));
    }),
);
$("refresh-account").onclick = () =>
  refreshAccount().catch((e) => notice(e.message, true, "account"));
$("refresh-tickets").onclick = () =>
  refreshTickets().catch((e) => notice(e.message, true, "support"));
$("mlflow-link").href = cfg.mlflowUrl;
$("grafana-link").href = cfg.grafanaUrl;
async function start() {
  try {
    await api("/health");
    $("connection").textContent = "Backend connected";
    categories = await api("/api/categories");
    await refreshAccount();
    const btn = document.querySelector(
      `nav button[data-view="${["account", "support", "operations"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "account"}"]`,
    );
    btn.click();
  } catch (e) {
    $("connection").textContent = "Backend unavailable";
    notice(e.message, true);
  }
}
setInterval(() => {
  if (currentView === "operations") refreshOperations().catch(() => {});
}, 5000);
start();
