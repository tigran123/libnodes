/* LibNodes client behaviour.
 *
 * Deliberately tiny. The design's rule is that state lives on the server and the DOM
 * carries whatever the client still needs, so this file only does the four things HTML
 * and HTMX genuinely cannot: toggle the mobile rail, keep the terminal bounded and
 * pinned to its tail, remember the dock's collapsed state across page swaps, and
 * implement shift-click range selection.
 */
(function () {
  "use strict";

  /* --- theme ------------------------------------------------------------- */

  /* Applied instantly on the client and persisted in a cookie, which the server reads
     to stamp data-theme on <html> for the next page load. No request, no flash. */
  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-theme-toggle]")) return;
    var root = document.documentElement;
    var light = root.getAttribute("data-theme") !== "light";
    if (light) {
      root.setAttribute("data-theme", "light");
    } else {
      root.removeAttribute("data-theme");
    }
    document.cookie =
      "libnodes_theme=" + (light ? "light" : "dark") + ";path=/;max-age=31536000;samesite=lax";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.textContent = light ? "◐" : "◑";
      btn.title = "Switch to " + (light ? "dark" : "light") + " theme";
    });
  });

  /* --- copy to clipboard -------------------------------------------------- */

  /* navigator.clipboard exists only in a secure context, and LibNodes is normally
     reached over plain http on the LAN — so the modern API is the fallback here, not
     the other way round. document.execCommand("copy") still works on http, but only
     from inside a user gesture, which is why the text is already in the DOM rather
     than fetched on click. */
  function copyText(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    document.body.removeChild(area);
    if (!ok && navigator.clipboard) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return false; }
      );
    }
    return Promise.resolve(ok);
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-from], [data-copy]");
    if (!btn) return;
    var text = btn.dataset.copy;
    if (!text) {
      var src = document.querySelector(btn.dataset.copyFrom);
      if (!src) return;
      text = src.value !== undefined ? src.value : src.textContent;
    }
    var label = btn.textContent;
    copyText(text).then(function (ok) {
      btn.textContent = ok ? "Copied" : "Copy failed";
      setTimeout(function () { btn.textContent = label; }, 1600);
    });
  });

  /* --- mobile rail ------------------------------------------------------- */

  document.addEventListener("click", function (e) {
    var toggle = e.target.closest("[data-rail-toggle]");
    if (toggle) {
      document.getElementById("shell").classList.toggle("rail-open");
      return;
    }
    var shell = document.getElementById("shell");
    if (shell && shell.classList.contains("rail-open") && !e.target.closest(".rail")) {
      shell.classList.remove("rail-open");
    }
  });

  /* --- terminal: bound the DOM and stay at the tail ---------------------- */

  var TERM_MAX = 200;

  function trimTerminal(term) {
    while (term.children.length > TERM_MAX) {
      term.removeChild(term.firstElementChild);
    }
    term.scrollTop = term.scrollHeight;
  }

  /* Only the dock's live terminals are trimmed. A job log opened for reading is not a
     stream, and trimming it threw away most of what was asked for. */
  document.body.addEventListener("htmx:afterSwap", function () {
    document.querySelectorAll("#job-dock .term").forEach(trimTerminal);
  });

  /* --- dock collapse, persisted across page swaps ------------------------ */

  var DOCK_KEY = "libnodes.dock.collapsed";

  function applyDockState() {
    var dock = document.getElementById("job-dock");
    if (!dock) return;
    var collapsed = sessionStorage.getItem(DOCK_KEY) === "1";
    dock.classList.toggle("is-collapsed", collapsed);
    dock.querySelectorAll("[data-dock-full]").forEach(function (el) {
      el.hidden = collapsed;
    });
    dock.querySelectorAll("[data-dock-pill]").forEach(function (el) {
      el.hidden = !collapsed;
    });
  }

  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-dock-collapse]")) return;
    var collapsed = sessionStorage.getItem(DOCK_KEY) === "1";
    sessionStorage.setItem(DOCK_KEY, collapsed ? "0" : "1");
    applyDockState();
  });

  document.body.addEventListener("htmx:afterSwap", applyDockState);
  document.addEventListener("DOMContentLoaded", applyDockState);

  /* --- library tree: expand / collapse ------------------------------------ */

  /* The tree is a flat list of rows carrying data-depth, so a subtree is just the
     contiguous run of deeper rows that follows. Collapsing hides that run; expanding a
     node whose children were never loaded fetches one level. Visibility is recomputed
     from scratch each time, so a collapsed node nested inside another stays collapsed
     when its parent reopens. */

  function refreshTree() {
    var rows = document.querySelectorAll(".tree-row[data-depth]");
    var collapsed = []; // depths of collapsed ancestors
    rows.forEach(function (row) {
      var depth = parseInt(row.dataset.depth, 10);
      while (collapsed.length && collapsed[collapsed.length - 1] >= depth) collapsed.pop();
      row.hidden = collapsed.length > 0;
      if (row.dataset.collapsed === "1") collapsed.push(depth);
    });
  }

  function childRows(row) {
    var depth = parseInt(row.dataset.depth, 10);
    var out = [];
    var next = row.nextElementSibling;
    while (next && next.dataset && parseInt(next.dataset.depth, 10) > depth) {
      out.push(next);
      next = next.nextElementSibling;
    }
    return out;
  }

  document.addEventListener("click", function (e) {
    var caret = e.target.closest("[data-tree-toggle]");
    if (!caret) return;
    e.preventDefault();
    e.stopPropagation();

    var row = caret.closest(".tree-row");
    var willOpen = row.dataset.collapsed === "1";
    row.dataset.collapsed = willOpen ? "0" : "1";
    caret.textContent = willOpen ? "▾" : "▸";

    if (willOpen && childRows(row).length === 0 && window.htmx) {
      // Never opened before: ask for one level and drop it in after this row.
      htmx.ajax(
        "GET",
        "/lib/tree/children?p=" +
          encodeURIComponent(row.dataset.path) +
          "&depth=" +
          row.dataset.depth,
        { target: row, swap: "afterend" }
      );
    }
    refreshTree();
  });

  document.body.addEventListener("htmx:afterSwap", refreshTree);
  document.addEventListener("DOMContentLoaded", refreshTree);

  /* --- row selection ------------------------------------------------------ */

  /* Per the design: "click toggles, shift-click selects a range". The whole row is the
     target — an 11px checkbox is not a control anyone should have to hit. */

  var lastChecked = null;

  function mark(box) {
    var row = box.closest(".trow");
    if (row) row.classList.toggle("is-selected", box.checked);
  }

  /* Row boxes only. The header's select-all is a control, not a selection, and counting
     it would make "all rows checked" impossible to reach. */
  function rowBoxes(scope) {
    return Array.prototype.slice.call(scope.querySelectorAll(".trow input.check"));
  }

  /* Indeterminate whenever the rows disagree with the header, so a part-selection never
     reads as "nothing selected". */
  function syncSelectAll(scope) {
    var all = scope.querySelector("[data-select-all]");
    if (!all) return;
    var boxes = rowBoxes(scope);
    var on = boxes.filter(function (b) { return b.checked; }).length;
    all.checked = boxes.length > 0 && on === boxes.length;
    all.indeterminate = on > 0 && on < boxes.length;
  }

  function syncEverySelectAll() {
    document.querySelectorAll("[data-selectable]").forEach(syncSelectAll);
  }

  /* Select-all lives on `click`, not on `change`, and the difference is load-bearing.
     #sel-form has hx-trigger="change", and a change event reaches the form on its way up
     to a document listener — so htmx would serialise the form before this code had ticked
     anything, and the selection bar would report the previous state. click fires first,
     and a checkbox is already toggled by the time a click handler sees it, so the boxes
     are set before the change that follows reaches htmx. */
  document.addEventListener("click", function (e) {
    var all = e.target.closest("[data-select-all]");
    if (!all) return;
    var scope = all.closest("[data-selectable]");
    if (!scope) return;
    rowBoxes(scope).forEach(function (box) {
      box.checked = all.checked;
      mark(box);
    });
    all.indeterminate = false;
    // The shift-click anchor belonged to the old selection; keeping it would extend a
    // range from a row the user never touched.
    lastChecked = null;
  });

  document.addEventListener("click", function (e) {
    // Real controls inside the row keep their own behaviour: push buttons, links,
    // the format select. Only the inert parts of the row toggle selection.
    if (e.target.closest("a, button, select, textarea")) return;

    var row = e.target.closest(".trow");
    if (!row) return;
    var scope = row.closest("[data-selectable]");
    if (!scope) return;
    var box = row.querySelector("input.check");
    if (!box) return;

    // Clicking the checkbox itself already toggled it; anywhere else has not.
    if (e.target !== box) box.checked = !box.checked;
    mark(box);

    // rowBoxes, not every input.check in the scope: the header's select-all is one too,
    // and including it would put a control inside the shift-click range.
    var boxes = rowBoxes(scope);
    if (e.shiftKey && lastChecked && boxes.indexOf(lastChecked) !== -1) {
      var a = boxes.indexOf(lastChecked);
      var b = boxes.indexOf(box);
      for (var i = Math.min(a, b); i <= Math.max(a, b); i++) {
        boxes[i].checked = box.checked;
        mark(boxes[i]);
      }
    }
    lastChecked = box;

    // The programmatic writes above fire no native change event, so the selection bar
    // would never hear about them.
    box.dispatchEvent(new Event("change", { bubbles: true }));
  });

  document.addEventListener("change", function (e) {
    var box = e.target.closest && e.target.closest("input.check");
    if (!box) return;
    mark(box);
    var scope = box.closest("[data-selectable]");
    if (scope) syncSelectAll(scope);
  });

  /* A filter keystroke swaps #file-rows for a fresh, wholly unselected set while the
     header box is left standing outside it — so without this it stays ticked over rows
     that are not. */
  document.body.addEventListener("htmx:afterSwap", syncEverySelectAll);
  document.addEventListener("DOMContentLoaded", syncEverySelectAll);
})();
