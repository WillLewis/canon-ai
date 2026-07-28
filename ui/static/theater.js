// Ingest theater (P3-FRONTDOOR): short-poll the run's event ledger from
// after=0 (a reload replays the whole narration), append Courier lines,
// tick the FACTS LEARNED counter, and reveal the first-finding card when a
// 'finding' event with first:true arrives. No promises in the narration.
(function () {
  var root = document.querySelector(".fd-theater");
  if (!root || !root.dataset.runId) return;
  var runId = root.dataset.runId, worldId = root.dataset.worldId, after = 0;
  var $ = function (id) { return document.getElementById(id); };
  var ledger = $("th-ledger"), facts = $("th-facts"), phase = $("th-phase");

  function line(text, cls) {
    var li = document.createElement("li");
    li.textContent = text;
    if (cls) li.className = cls;
    ledger.appendChild(li);
    ledger.scrollTop = ledger.scrollHeight;
  }

  function onEvent(ev) {
    var d = ev.data || {};
    if (ev.kind === "scene") {
      line("sc " + d.index + "/" + d.total + "  " + (d.slug || "") +
           "  — " + d.facts_scene + " fact" + (d.facts_scene === 1 ? "" : "s"));
      facts.textContent = d.facts_total;
    } else if (ev.kind === "entity") {
      line("connected “" + d.alias + "” → " + d.name + " [" + d.kind + "]", "fd-dim");
    } else if (ev.kind === "phase") {
      phase.textContent = d.phase;
      phase.dataset.phase = d.phase;
    } else if (ev.kind === "finding" && d.first) {
      $("th-first-sev").textContent = (d.severity || "") + " · " + (d.check || "");
      $("th-first-sev").className = "fd-sev fd-sev-" + (d.severity || "note");
      $("th-first-summary").textContent = "It caught something.";
      $("th-first-ev").textContent = d.explanation || "";
      $("th-first-links").innerHTML = d.scene_id
        ? '<a href="/worlds/' + worldId + '/script?scene=' + d.scene_id + '">show me in the script</a>' : "";
      $("th-first").hidden = false;
    } else if (ev.kind === "finding") {
      line("flag: " + (d.explanation || d.check), "fd-flag");
    } else if (ev.kind === "stat") {
      line("");
    } else if (ev.kind === "cost_abort") {
      line("run stopped at the cost ceiling ($" + d.cost_usd.toFixed(2) + ")", "fd-flag");
    }
  }

  function onRun(run) {
    if (run.status === "done") {
      var done = $("th-done");
      done.textContent = "Canon now holds " + run.facts_total + " facts about your world.";
      done.innerHTML += ' <a href="/worlds/' + worldId + '/report">Read the report →</a>';
      done.hidden = false;
      return true;
    }
    if (run.status === "failed" || run.status === "aborted") {
      var err = $("th-error");
      err.textContent = run.error || "the run stopped";
      err.hidden = false;
      if (run.resumable && root.dataset.canEdit === "1") $("th-resume").hidden = false;
      return true;
    }
    return false;
  }

  var resumeBtn = $("th-resume-btn");
  if (resumeBtn) resumeBtn.addEventListener("click", function () {
    fetch("/api/runs/" + runId + "/resume", { method: "POST" }).then(function (r) {
      if (r.status === 202) { $("th-error").hidden = true; $("th-resume").hidden = true; poll(); }
    });
  });

  function poll() {
    fetch("/api/runs/" + runId + "/events?after=" + after).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function (j) {
      (j.events || []).forEach(onEvent);
      after = j.next;
      facts.textContent = Math.max(+facts.textContent || 0, j.run.facts_total || 0);
      if (!onRun(j.run)) setTimeout(poll, 1500);
    }).catch(function () { setTimeout(poll, 4000); });
  }
  poll();
})();
