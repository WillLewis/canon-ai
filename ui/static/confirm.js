/* Canon AI — confirm queue (P3-CONFIRM). Vanilla JS, no framework, no deps.
 *
 * 1. Keyboard triage: j/k next/prev card, c confirm, r reject, u undo-last —
 *    a full queue is triageable start-to-finish on the keyboard.
 * 2. Undo grace: a ruling is held client-side and its POST fires after 5s, on
 *    the next ruling, or when the page is left — one keystroke rules, u takes
 *    it back inside the window, nothing guilt-trips.
 * 3. Progressive enhancement: without JS the Confirm/Reject forms submit
 *    normally (immediate POST + redirect); the server-side status='draft'
 *    guard keeps every path idempotent.
 *
 * Display-only otherwise: nothing here writes anything except firing the same
 * confirm/reject POSTs a mouse user's form submit would send.
 */
(function () {
  "use strict";

  var GRACE_MS = 5000;
  var queue = document.getElementById("confirm-queue");
  if (!queue) return; /* empty queue — nothing to triage */

  var counterN = document.getElementById("queue-n");
  var counterPlural = document.getElementById("queue-plural");
  var kbdHint = document.getElementById("confirm-kbd-hint");
  var emptyState = document.getElementById("queue-empty");
  var cards = Array.prototype.slice.call(queue.querySelectorAll(".draft-card"));
  var cursor = -1;
  var pending = null; /* { card, form, timer } — at most one ruling in grace */

  function live() {
    return cards.filter(function (c) { return !c.classList.contains("is-ruled"); });
  }

  function updateCount() {
    var n = live().length;
    if (counterN) counterN.textContent = String(n);
    if (counterPlural) counterPlural.textContent = n === 1 ? "" : "s";
  }

  function maybeEmpty() {
    if (live().length === 0 && !pending) {
      queue.hidden = true;
      if (kbdHint) kbdHint.hidden = true;
      if (emptyState) emptyState.hidden = false;
    }
  }

  function focusOn(card) {
    if (cursor >= 0 && cards[cursor]) cards[cursor].classList.remove("kbd-focus");
    cursor = card ? cards.indexOf(card) : -1;
    if (card) {
      card.classList.add("kbd-focus");
      card.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  function step(dir) {
    var l = live();
    if (!l.length) return;
    var cur = cursor >= 0 ? cards[cursor] : null;
    var i = cur ? l.indexOf(cur) : -1;
    if (i === -1) i = dir > 0 ? -1 : l.length; /* enter the list from either end */
    focusOn(l[(i + dir + l.length) % l.length]);
  }

  function nextLiveAfter(card) {
    for (var i = cards.indexOf(card) + 1; i < cards.length; i++)
      if (!cards[i].classList.contains("is-ruled")) return cards[i];
    return live()[0] || null;
  }

  function markRuled(card, verb) {
    card.classList.add("is-ruled", "is-pending");
    var line = card.querySelector(".ruled-line");
    if (line) {
      line.querySelector(".ruled-verb").textContent = verb;
      line.hidden = false;
    }
  }

  function unmark(card) {
    card.classList.remove("is-ruled", "is-pending");
    var line = card.querySelector(".ruled-line");
    if (line) line.hidden = true;
  }

  function settle(card) {
    card.remove(); /* keeps its is-ruled class, so live() stays correct */
    maybeEmpty();
  }

  /* The grace window closed: the ruling becomes real. On any failure the card
     is restored un-ruled — a lost ruling must be visible, never silent. */
  function flush() {
    if (!pending) return;
    var p = pending;
    pending = null;
    clearTimeout(p.timer);
    fetch(p.form.action, { method: "POST" })
      .then(function (r) {
        if (r.ok) settle(p.card);
        else { unmark(p.card); updateCount(); }
      })
      .catch(function () { unmark(p.card); updateCount(); });
  }

  function rule(card, kind) {
    if (!card || card.classList.contains("is-ruled")) return;
    var form = card.querySelector(
      kind === "confirm" ? "form.confirm-form" : "form.reject-form");
    if (!form) return;
    var btn = form.querySelector("button");
    if (btn && btn.disabled) return; /* viewer / anonymous: read-only */
    flush(); /* the previous ruling's window closes on the next action */
    markRuled(card, kind === "confirm" ? "Confirmed" : "Rejected");
    pending = { card: card, form: form, timer: setTimeout(flush, GRACE_MS) };
    updateCount();
    var nxt = nextLiveAfter(card);
    if (nxt) focusOn(nxt);
  }

  function undo() {
    if (!pending) return;
    clearTimeout(pending.timer);
    var card = pending.card;
    pending = null;
    unmark(card);
    updateCount();
    focusOn(card);
  }

  /* Mouse path: intercept the forms so clicks get the same undo grace. */
  queue.addEventListener("submit", function (e) {
    var form = e.target;
    var isConfirm = form.classList.contains("confirm-form");
    if (!isConfirm && !form.classList.contains("reject-form")) return;
    e.preventDefault();
    rule(form.closest(".draft-card"), isConfirm ? "confirm" : "reject");
  });

  /* Leaving the page fires the held ruling rather than dropping it. */
  window.addEventListener("pagehide", function () {
    if (!pending) return;
    clearTimeout(pending.timer);
    if (navigator.sendBeacon) navigator.sendBeacon(pending.form.action);
    pending = null;
  });

  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    var current = cursor >= 0 ? cards[cursor] : null;
    switch (e.key) {
      case "j": step(1); e.preventDefault(); break;
      case "k": step(-1); e.preventDefault(); break;
      case "c": if (current) rule(current, "confirm"); break;
      case "r": if (current) rule(current, "reject"); break;
      case "u": undo(); break;
    }
  });
})();
