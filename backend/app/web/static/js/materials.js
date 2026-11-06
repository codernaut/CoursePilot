/* Materials page: drag-and-drop multi-file upload with per-file progress, delete/re-index
   confirmation and screen-reader announcements. No inline scripts (CSP): loaded as a file. */
(function () {
  "use strict";

  var CONCURRENCY = 2;

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function announce(message) {
    var region = document.getElementById("upload-status");
    if (!region) { return; }
    region.textContent = "";
    // A short delay makes screen readers announce repeated identical messages too.
    window.setTimeout(function () { region.textContent = message; }, 50);
  }

  /* Confirmation for forms marked data-confirm (works for rows swapped in by HTMX too). */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form && form.getAttribute ? form.getAttribute("data-confirm") : null;
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  /* Announce document status changes picked up by HTMX polling. */
  var knownStatuses = {};
  function scanStatuses(announceChanges) {
    var rows = document.querySelectorAll("#document-list [data-doc-id]");
    var next = {};
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var id = row.getAttribute("data-doc-id");
      var status = row.getAttribute("data-status");
      next[id] = status;
      if (announceChanges && knownStatuses[id] && knownStatuses[id] !== status) {
        announce(row.getAttribute("data-title") + " is now " + status + ".");
      }
    }
    knownStatuses = next;
  }
  scanStatuses(false);
  document.body.addEventListener("htmx:afterSwap", function () { scanStatuses(true); });

  var zone = document.getElementById("drop-zone");
  var input = document.getElementById("file-input");
  var queue = document.getElementById("upload-queue");
  if (!zone || !input || !queue) { return; }

  var uploadUrl = zone.getAttribute("data-upload-url");
  var listUrl = zone.getAttribute("data-list-url");
  var maxBytes = parseInt(zone.getAttribute("data-max-bytes") || "0", 10);
  var extensions = (zone.getAttribute("data-extensions") || "").split(",");

  function refreshList() {
    if (window.htmx && listUrl) {
      window.htmx.ajax("GET", listUrl, { target: "#document-list", swap: "outerHTML" });
    } else {
      window.location.reload();
    }
  }

  function formatMb(bytes) {
    return Math.round(bytes / (1024 * 1024)) + " MB";
  }

  function addItem(file) {
    var li = document.createElement("li");
    li.className = "upload-item";
    var name = document.createElement("span");
    name.className = "upload-name";
    name.textContent = file.name;
    var bar = document.createElement("progress");
    bar.max = 100;
    bar.value = 0;
    bar.setAttribute("aria-label", "Upload progress for " + file.name);
    var state = document.createElement("span");
    state.className = "upload-state";
    state.textContent = "Waiting";
    li.appendChild(name);
    li.appendChild(bar);
    li.appendChild(state);
    queue.appendChild(li);
    return { li: li, bar: bar, state: state, file: file };
  }

  function fail(item, message) {
    item.li.classList.add("upload-failed");
    item.state.textContent = "Failed: " + message;
    item.bar.value = 0;
  }

  function errorMessage(xhr) {
    try {
      var body = JSON.parse(xhr.responseText);
      if (body && body.error && body.error.message) { return body.error.message; }
    } catch (e) { /* not JSON */ }
    if (xhr.status === 0) { return "network error"; }
    return "the server answered " + xhr.status;
  }

  function precheck(file) {
    var dot = file.name.lastIndexOf(".");
    var ext = dot >= 0 ? file.name.slice(dot + 1).toLowerCase() : "";
    if (extensions.indexOf(ext) === -1) {
      return "unsupported file type (allowed: " + extensions.join(", ") + ")";
    }
    if (maxBytes && file.size > maxBytes) {
      return "larger than the " + formatMb(maxBytes) + " limit";
    }
    if (file.size === 0) { return "the file is empty"; }
    return null;
  }

  function send(item) {
    return new Promise(function (resolve) {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", uploadUrl);
      xhr.setRequestHeader("X-CSRF-Token", csrfToken());
      xhr.setRequestHeader("Accept", "application/json");
      item.state.textContent = "Uploading";
      xhr.upload.onprogress = function (event) {
        if (event.lengthComputable && event.total > 0) {
          item.bar.value = Math.round((100 * event.loaded) / event.total);
        }
      };
      xhr.onload = function () {
        if (xhr.status === 202) {
          item.bar.value = 100;
          item.li.classList.add("upload-done");
          item.state.textContent = "Uploaded, queued for processing";
          resolve(true);
        } else {
          fail(item, errorMessage(xhr));
          resolve(false);
        }
      };
      xhr.onerror = function () { fail(item, "network error"); resolve(false); };
      xhr.onabort = function () { fail(item, "upload cancelled"); resolve(false); };
      var data = new FormData();
      data.append("file", item.file, item.file.name);
      xhr.send(data);
    });
  }

  function uploadAll(files) {
    var items = [];
    for (var i = 0; i < files.length; i++) {
      var item = addItem(files[i]);
      var problem = precheck(files[i]);
      if (problem) { fail(item, problem); } else { items.push(item); }
    }
    var next = 0;
    var ok = 0;
    var failed = files.length - items.length;
    function worker() {
      if (next >= items.length) { return Promise.resolve(); }
      var item = items[next++];
      return send(item).then(function (success) {
        if (success) { ok++; refreshList(); } else { failed++; }
        return worker();
      });
    }
    var workers = [];
    for (var w = 0; w < Math.min(CONCURRENCY, items.length); w++) { workers.push(worker()); }
    Promise.all(workers).then(function () {
      announce(ok + " file(s) uploaded" + (failed ? ", " + failed + " failed." : "."));
      if (ok) { refreshList(); }
    });
  }

  input.addEventListener("change", function () {
    if (input.files && input.files.length) { uploadAll(Array.prototype.slice.call(input.files)); }
    input.value = "";
  });

  ["dragenter", "dragover"].forEach(function (name) {
    zone.addEventListener(name, function (event) {
      event.preventDefault();
      zone.classList.add("dropzone-active");
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    zone.addEventListener(name, function (event) {
      event.preventDefault();
      zone.classList.remove("dropzone-active");
    });
  });
  zone.addEventListener("drop", function (event) {
    var files = event.dataTransfer && event.dataTransfer.files;
    if (files && files.length) { uploadAll(Array.prototype.slice.call(files)); }
  });
  /* Dropping a file next to the zone must not make the browser navigate away from the page. */
  ["dragover", "drop"].forEach(function (name) {
    window.addEventListener(name, function (event) { event.preventDefault(); });
  });
})();
