/* Canon AI — note surface enhancements. Vanilla JS, no framework, no fetch.
 *
 * 1. Keyboard triage: j/k next/prev note, enter expand/collapse, s seal,
 *    d dismiss — a full report is triageable start-to-finish on the keyboard.
 * 2. Citation clicks inside the script view highlight the stored supporting
 *    quote in the cited scene and scroll to it (no reload; the href is the
 *    server-rendered fallback for everyone else).
 * 3. Script view scroll-spy: the annotation column tracks the scene in view.
 *
 * Display-only: nothing here writes anything except submitting the same
 * seal/dismiss forms a mouse user would click.
 */
(function () {
  "use strict";

  /* ---------- keyboard triage ---------- */

  var notes = Array.prototype.slice.call(
    document.querySelectorAll('details.note[data-status="open"]'));
  var cursor = -1;

  function focusNote(i) {
    if (!notes.length) return;
    if (cursor >= 0 && notes[cursor]) notes[cursor].classList.remove("kbd-focus");
    cursor = (i + notes.length) % notes.length;
    var n = notes[cursor];
    n.classList.add("kbd-focus");
    n.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function submitIn(note, selector) {
    if (!note) return;
    var form = note.querySelector(selector);
    if (!form) return;
    var btn = form.querySelector("button");
    if (btn && btn.disabled) return; /* viewer: read-only */
    form.submit();
  }

  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    var current = cursor >= 0 ? notes[cursor] : null;
    switch (e.key) {
      case "j": focusNote(cursor + 1); e.preventDefault(); break;
      case "k": focusNote(cursor - 1); e.preventDefault(); break;
      case "Enter":
        if (current) { current.open = !current.open; e.preventDefault(); }
        break;
      case "s": submitIn(current, "form.seal-form"); break;
      case "d": submitIn(current, "form.dismiss-form"); break;
    }
  });

  /* ---------- citation → quote highlight (script view) ---------- */

  function escapeHtml(s) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(s));
    return div.innerHTML;
  }

  function highlightQuote(sceneId, quote) {
    var pre = document.getElementById("scene-text-" + sceneId);
    if (!pre) return false;
    /* clear previous marks by resetting to plain text */
    document.querySelectorAll(".scene-text mark").forEach(function (m) {
      m.outerHTML = m.innerHTML;
    });
    var scene = document.getElementById("scene-" + sceneId);
    if (scene) scene.scrollIntoView({ block: "start", behavior: "smooth" });
    if (!quote) return true;
    var escaped = escapeHtml(pre.textContent);
    var eq = escapeHtml(quote.trim());
    if (eq && escaped.indexOf(eq) !== -1) {
      pre.innerHTML = escaped.split(eq).join("<mark>" + eq + "</mark>");
      var mark = pre.querySelector("mark");
      if (mark) mark.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    return true;
  }

  var inScriptView = !!document.getElementById("script-col");
  if (inScriptView) {
    document.addEventListener("click", function (e) {
      var a = e.target && e.target.closest ? e.target.closest("a.qcite") : null;
      if (!a) return;
      var sceneId = a.getAttribute("data-scene");
      if (sceneId && highlightQuote(sceneId, a.getAttribute("data-quote") || "")) {
        e.preventDefault();
        if (history.replaceState) history.replaceState(null, "", "#scene-" + sceneId);
      }
    });

    /* ---------- scroll-spy: annotation column follows the scene in view ---- */
    var groups = document.querySelectorAll(".anno-group");
    var blocks = document.querySelectorAll(".scene-block");
    function spy() {
      var top = null;
      blocks.forEach(function (b) {
        var r = b.getBoundingClientRect();
        if (top === null && r.bottom > 90) top = b.getAttribute("data-scene-id");
      });
      groups.forEach(function (g) {
        g.classList.toggle("current", g.getAttribute("data-scene-id") === top);
      });
    }
    var ticking = false;
    window.addEventListener("scroll", function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () { spy(); ticking = false; });
    }, { passive: true });
    spy();
  }
})();
