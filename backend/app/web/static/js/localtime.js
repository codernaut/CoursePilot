/* Shows <time data-local-time datetime="..."> in the reader's own time zone (HH:MM). Without
   JavaScript the server-rendered UTC text stays. */
(function () {
  "use strict";
  var items = document.querySelectorAll("time[data-local-time]");
  Array.prototype.forEach.call(items, function (el) {
    var stamp = new Date(el.getAttribute("datetime") || "");
    if (isNaN(stamp.getTime())) { return; }
    el.textContent = stamp.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  });
})();
