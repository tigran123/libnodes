/* LibNodes client behaviour: only what HTML and htmx cannot do -- the theme, the password
 * eye, copying, the mobile rail, closing dialogs, bounding the terminal, the dock's
 * collapsed state, and row selection with shift-click ranges.
 */
(function () {
  "use strict";

  /* --- theme ------------------------------------------------------------- */

  /* Applied at once and kept in a cookie the server reads, so the next load has no flash. */
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
      // Only the title: which icon shows is CSS keyed off data-theme.
      btn.title = "Switch to " + (light ? "dark" : "light") + " theme";
    });
  });

  /* --- password reveal ----------------------------------------------------- */

  /* Marks the document as scripted, so CSS shows controls that need this file (the eye). */
  document.documentElement.setAttribute("data-js", "");

  document.addEventListener("click", function (e) {
    var eye = e.target.closest("[data-pw-reveal]");
    if (!eye) return;
    var field = document.getElementById(eye.getAttribute("aria-controls"));
    if (!field) return;

    var showing = field.type === "text";
    /* Changing `type` drops the selection; keep the caret where it was. */
    var start = field.selectionStart;
    var end = field.selectionEnd;

    field.type = showing ? "password" : "text";
    eye.setAttribute("aria-pressed", showing ? "false" : "true");
    var label = showing ? "Show password" : "Hide password";
    eye.setAttribute("aria-label", label);
    eye.title = label;

    field.focus();
    if (start !== null) {
      field.setSelectionRange(start, end);
    }
  });

  /* --- copy to clipboard -------------------------------------------------- */

  /* navigator.clipboard needs a secure context and this is plain http, so execCommand is
     first and the modern API the fallback; it needs a user gesture, hence the text is
     already in the DOM. */
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
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var text = btn.dataset.copy;
    if (!text) return;
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

  /* --- dialogs: tap outside or Escape closes ----------------------------- */

  /* A tap on the backdrop or Escape does what Close does. The press must start on the
     backdrop too: releasing a text selection past the dialog's edge is also a click there. */
  var pressedBackdrop = null;

  document.addEventListener("pointerdown", function (e) {
    pressedBackdrop = e.target.classList && e.target.classList.contains("backdrop")
      ? e.target : null;
  });

  document.addEventListener("click", function (e) {
    if (e.target === pressedBackdrop) e.target.remove();
    pressedBackdrop = null;
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var open = document.querySelectorAll(".backdrop");
    if (open.length) open[open.length - 1].remove();
  });

  /* --- terminal: bound the DOM and stay at the tail ---------------------- */

  var TERM_MAX = 200;

  function trimTerminal(term) {
    while (term.children.length > TERM_MAX) {
      term.removeChild(term.firstElementChild);
    }
    term.scrollTop = term.scrollHeight;
  }

  /* Only the dock's live terminals: a job log opened for reading is not a stream. */
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

  /* --- row selection ------------------------------------------------------ */

  /* Click toggles, shift-click selects a range; the whole row is the target. */

  var lastChecked = null;

  function mark(box) {
    var row = box.closest(".trow");
    if (row) row.classList.toggle("is-selected", box.checked);
  }

  /* Row boxes only, never the header's select-all. */
  function rowBoxes(scope) {
    return Array.prototype.slice.call(scope.querySelectorAll(".trow input.check"));
  }


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

  /* Select-all on `click`, not `change`: #sel-form's htmx trigger is `change`, and would
     serialise the form before the boxes were ticked. */
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
    // The shift-click anchor belonged to the old selection.
    lastChecked = null;
  });

  document.addEventListener("click", function (e) {
    // Links and buttons inside the row keep their own behaviour.
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

    var boxes = rowBoxes(scope);
    var ranged = e.shiftKey && lastChecked && boxes.indexOf(lastChecked) !== -1;
    if (ranged) {
      var a = boxes.indexOf(lastChecked);
      var b = boxes.indexOf(box);
      for (var i = Math.min(a, b); i <= Math.max(a, b); i++) {
        boxes[i].checked = box.checked;
        mark(boxes[i]);
      }
    }
    lastChecked = box;

    // The programmatic writes above fire no native change event, so the selection bar
    // would never hear about them. A click on the box itself already fired one, and a
    // second made every tick cost two /lib/selection requests -- unless a shift-range
    // ticked other rows too, which no native event covers.
    if (e.target !== box || ranged) box.dispatchEvent(new Event("change", { bubbles: true }));
  });

  document.addEventListener("change", function (e) {
    var box = e.target.closest && e.target.closest("input.check");
    if (!box) return;
    mark(box);
    var scope = box.closest("[data-selectable]");
    if (scope) syncSelectAll(scope);
  });

  /* A filter swap replaces the rows but not the header box, which must follow them. */
  document.body.addEventListener("htmx:afterSwap", syncEverySelectAll);
  document.addEventListener("DOMContentLoaded", syncEverySelectAll);
})();
