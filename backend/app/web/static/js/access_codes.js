/* Copy button for a freshly created module access code (no inline script, CSP safe). */
(function () {
  "use strict";
  var button = document.getElementById("copy-code");
  var value = document.getElementById("new-code-value");
  var status = document.getElementById("copy-status");
  if (!button || !value) { return; }
  button.hidden = false;

  function done(message) { if (status) { status.textContent = message; } }

  button.addEventListener("click", function () {
    var text = value.textContent || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { done("Code copied."); },
        function () { done("Copy failed. Select the code and copy it."); }
      );
      return;
    }
    var range = document.createRange();
    range.selectNodeContents(value);
    var selection = window.getSelection();
    if (selection) { selection.removeAllRanges(); selection.addRange(range); }
    done("Code selected. Press Ctrl+C to copy.");
  });
})();
