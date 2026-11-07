/* Passage side panel. Any element with data-passage-id opens it (search results now, [C#]
   citation chips in chat later). The passage is fetched from GET /api/v1/chunks/{id} and written
   with textContent only, so nothing from a document is ever interpreted as HTML. Escape or the
   Close button closes it, Tab stays inside it while open, and focus returns to the opener.
   No inline scripts (CSP): loaded as a file. */
(function () {
  "use strict";

  var panel = document.getElementById("passage-panel");
  var backdrop = document.getElementById("passage-backdrop");
  if (!panel || !backdrop) { return; }

  var closeButton = document.getElementById("passage-close");
  var statusEl = document.getElementById("passage-status");
  var bodyEl = document.getElementById("passage-body");
  var titleEl = document.getElementById("passage-title");
  var pathEl = document.getElementById("passage-path");
  var locationEl = document.getElementById("passage-location");
  var textEl = document.getElementById("passage-text");
  var linkEl = document.getElementById("passage-link");
  var opener = null;
  var requestId = 0;

  function focusable() {
    // Skip anything inside a [hidden] container (the link is hidden while the passage loads or
    // fails): a hidden element cannot take focus, so using it as the trap's edge would let Tab
    // leave the dialog.
    return Array.prototype.filter.call(
      panel.querySelectorAll("a[href], button:not([disabled])"),
      function (el) { return !el.closest("[hidden]"); }
    );
  }

  function setStatus(message) { statusEl.textContent = message; }

  function show(passage) {
    titleEl.textContent = passage.document_title || "Passage";
    pathEl.textContent = passage.path || "";
    var where = [];
    if (passage.location) { where.push(passage.location); }
    if (passage.section_path) { where.push(passage.section_path); }
    locationEl.textContent = where.join(" · ");
    textEl.textContent = passage.text || "";
    var url = passage.document_url || "";
    // Only same-origin document links are ever used.
    if (url.indexOf("/api/v1/documents/") === 0) {
      linkEl.setAttribute("href", url);
      linkEl.textContent = passage.page_start
        ? "Open document at page " + passage.page_start
        : "Open document";
      linkEl.hidden = false;
    } else {
      linkEl.hidden = true;
    }
    bodyEl.hidden = false;
    setStatus("");
  }

  function open(chunkId, trigger) {
    opener = trigger || document.activeElement;
    requestId += 1;
    var mine = requestId;
    bodyEl.hidden = true;
    titleEl.textContent = "Passage";
    setStatus("Loading the passage…");
    panel.hidden = false;
    backdrop.hidden = false;
    panel.focus();
    fetch("/api/v1/chunks/" + encodeURIComponent(chunkId), {
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (response) {
      if (!response.ok) { throw new Error(String(response.status)); }
      return response.json();
    }).then(function (passage) {
      if (mine === requestId) { show(passage); }
    }).catch(function () {
      if (mine === requestId) { setStatus("This passage could not be loaded. It may have been removed."); }
    });
  }

  function close() {
    if (panel.hidden) { return; }
    requestId += 1;
    panel.hidden = true;
    backdrop.hidden = true;
    if (opener && typeof opener.focus === "function") { opener.focus(); }
    opener = null;
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    var trigger = target && target.closest ? target.closest("[data-passage-id]") : null;
    if (trigger) {
      event.preventDefault();
      open(trigger.getAttribute("data-passage-id"), trigger);
    }
  });
  closeButton.addEventListener("click", close);
  backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (event) {
    if (panel.hidden) { return; }
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "Tab") {
      var items = focusable();
      if (!items.length) { event.preventDefault(); return; }
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!panel.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    }
  });

  window.CoursePilotPassages = { open: open, close: close };
})();
