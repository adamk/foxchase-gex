/* Shared authorization presentation; token ownership belongs to the backend. */
const GexAuthorization = (() => {
  function view(status, now = Date.now()) {
    const deadline = Date.parse(status.next_reauth_due_at || "");
    const valid = Number.isFinite(deadline);
    const remaining = valid ? Math.max(0, Math.ceil(Number.isFinite(status._remaining) ? status._remaining : (deadline - now) / 1000)) : null;
    const required = status.reauth_required === true || status.state === "reauth_required" || (valid && remaining === 0);
    const tier = required ? "required" : !valid ? "unknown" : remaining <= 86400 ? "24h" : remaining <= 259200 ? "72h" : "healthy";
    const time = remaining === null ? "Time remaining unavailable" : `${Math.floor(remaining/86400)}d ${Math.floor(remaining%86400/3600)}h ${Math.floor(remaining%3600/60)}m ${remaining%60}s remaining`;
    return {tier, text: `Schwab Authorization · ${required ? "Reauthorization required" : status.reauthorization_pending ? "Authorization pending" : tier === "healthy" ? "Authorized" : tier === "unknown" ? "Status unknown" : tier === "24h" ? "Reauthorize within 24 hours" : "Reauthorize within 72 hours"} · ${valid ? "Deadline " + new Date(deadline).toISOString() : "Deadline unavailable"} · ${time}`};
  }
  let status = null;
  let received = 0;
  function render() {
    if (!status) return;
    const label = document.getElementById("operator-auth-label");
    const action = document.getElementById("operator-reauthorize-link");
    if (!label || !action) return;
    const state = view({...status, _remaining: typeof status.remaining_seconds === "number" ? Math.max(0, status.remaining_seconds - (performance.now() - received) / 1000) : undefined});
    label.textContent = state.text;
    action.dataset.authState = state.tier === "healthy" ? "healthy" : "reauth_required";
    label.dataset.warningTier = state.tier;
    if (label.style) label.style.color = state.tier === "required" ? "#ff7777" : ["72h", "24h"].includes(state.tier) ? "#f5c76b" : "";
  }
  if (typeof window !== "undefined") setInterval(render, 1000);
  return {view, update(value) { status = value; received = performance.now(); render(); }};
})();
