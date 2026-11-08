/* Theme, accent colour and small header behaviour. Loaded first in <head>, NOT deferred, so the
   stored theme and accent are applied before the first paint (no flash, no inline script: CSP).
   localStorage keys: "cp-theme" ("light" | "dark"; absent = follow the system) and "cp-accent". */
(function () {
  "use strict";

  var ACCENTS = ["crimson", "coral", "plum", "teal", "ocean"];
  var root = document.documentElement;
  var dark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function read(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function write(key, value) {
    try {
      if (value === null) { window.localStorage.removeItem(key); }
      else { window.localStorage.setItem(key, value); }
    } catch (e) { /* storage blocked: the choice just is not remembered */ }
  }

  function resolvedTheme() {
    var stored = read("cp-theme");
    if (stored === "light" || stored === "dark") { return stored; }
    return dark && dark.matches ? "dark" : "light";
  }

  function apply() {
    var theme = resolvedTheme();
    var accent = read("cp-accent");
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-accent", ACCENTS.indexOf(accent) >= 0 ? accent : "crimson");
    root.classList.add("js");
  }

  function syncControls() {
    var theme = root.getAttribute("data-theme");
    var accent = root.getAttribute("data-accent");
    Array.prototype.forEach.call(document.querySelectorAll("[data-theme-toggle]"), function (button) {
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    });
    Array.prototype.forEach.call(document.querySelectorAll("button[data-accent]"), function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-accent") === accent ? "true" : "false");
    });
  }

  apply();
  if (dark && dark.addEventListener) {
    dark.addEventListener("change", function () { apply(); syncControls(); });
  }
  document.addEventListener("DOMContentLoaded", syncControls);

  /* Event delegation: works for controls that do not exist yet when this file runs. */
  document.addEventListener("click", function (event) {
    var target = event.target && event.target.closest ? event.target : null;
    if (!target) { return; }

    var toggle = target.closest("[data-theme-toggle]");
    if (toggle) {
      write("cp-theme", root.getAttribute("data-theme") === "dark" ? "light" : "dark");
      apply();
      syncControls();
      return;
    }
    var swatch = target.closest("button[data-accent]");
    if (swatch) {
      write("cp-accent", swatch.getAttribute("data-accent"));
      apply();
      syncControls();
      return;
    }
    var menu = target.closest("[data-menu-toggle]");
    if (menu) {
      var header = menu.closest("header");
      var open = header.getAttribute("data-menu-open") !== "true";
      header.setAttribute("data-menu-open", open ? "true" : "false");
      menu.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    /* A click outside an open user menu closes it. */
    Array.prototype.forEach.call(document.querySelectorAll("details.usermenu[open]"), function (details) {
      if (!details.contains(target)) { details.removeAttribute("open"); }
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") { return; }
    Array.prototype.forEach.call(document.querySelectorAll("details.usermenu[open]"), function (details) {
      details.removeAttribute("open");
      var summary = details.querySelector("summary");
      if (summary) { summary.focus(); }
    });
  });
})();
