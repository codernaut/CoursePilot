/* Chat page: streams an answer over POST (fetch + ReadableStream; EventSource cannot POST),
   turns [C#] / [W#] markers into chips AFTER escaping (everything is written with textContent or
   built as DOM nodes, never parsed as markup), opens the passage panel (passage_panel.js) for [C#] chips,
   sends thumbs up/down and deletes conversations. No inline scripts (CSP): loaded as a file.

   SSE events (CLAUDE.md 6.3): status {text}, token {text}, citations {citations, content},
   done {message_id, conversation_id, usage, latency_ms}, error {code, message}. */
(function () {
  "use strict";

  var app = document.getElementById("chat-app");
  if (!app) { return; }

  var list = document.getElementById("messages");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var sendButton = document.getElementById("chat-send");
  var stopButton = document.getElementById("chat-stop");
  var statusEl = document.getElementById("chat-status");
  var moduleSelect = document.getElementById("chat-module");
  var providerAlert = document.getElementById("provider-alert");
  var conversationList = document.getElementById("conversation-list");
  var emptyNote = document.getElementById("conversation-empty");

  var conversationId = app.getAttribute("data-conversation-id") || "";
  var busy = false;
  var controller = null;

  var MARKER = /\[((?:[CW]\d{1,3})(?:\s*[,;]\s*[CW]\d{1,3})*)\]/g;
  var ID = /[CW]\d{1,3}/g;

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function setStatus(message) { statusEl.textContent = message; }

  /* A decorative icon from the sprite in _icons.html (built as DOM nodes, never parsed markup). */
  function icon(name) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "i");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(ns, "use");
    use.setAttribute("href", "#" + name);
    svg.appendChild(use);
    return svg;
  }

  function showProviderError(message) {
    if (!providerAlert) { return; }
    providerAlert.textContent = message;
    providerAlert.hidden = false;
  }

  function isHttpUrl(url) {
    return typeof url === "string" && /^https?:\/\//i.test(url);
  }

  /* ---- rendering ---- */

  function byMarker(citations) {
    var map = {};
    (citations || []).forEach(function (c) { map[c.marker] = c; });
    return map;
  }

  /* `kind` is the stable key of a citation ("material" | "web"); `type` carries the same value. */
  function isWeb(citation, marker) {
    return (citation && (citation.kind === "web" || citation.type === "web")) ||
      (!citation && marker.charAt(0) === "W");
  }

  function chipFor(marker, citation) {
    if (!citation) { return document.createTextNode("[" + marker + "]"); }
    if (isWeb(citation, marker)) {
      if (!isHttpUrl(citation.url)) { return document.createTextNode("[" + marker + "]"); }
      var link = el("a", "citation-chip citation-web", marker);
      link.setAttribute("href", citation.url);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      link.setAttribute("aria-label", "Web source " + marker + ": " + (citation.title || citation.url));
      link.setAttribute("title", [citation.site, citation.title].filter(Boolean).join(" - "));
      return link;
    }
    var chip = el("button", "citation-chip", marker);
    chip.type = "button";
    chip.setAttribute("data-passage-id", citation.chunk_id);
    chip.setAttribute(
      "aria-label",
      "Source " + marker + ": " + (citation.document_title || "document") +
        (citation.location ? ", " + citation.location : "")
    );
    return chip;
  }

  /* Plain text with chips for known markers; unknown markers stay plain text. */
  function renderText(container, text, citations) {
    container.textContent = "";
    var known = byMarker(citations);
    var last = 0;
    var match;
    MARKER.lastIndex = 0;
    while ((match = MARKER.exec(text)) !== null) {
      if (match.index > last) {
        container.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      var ids = match[1].match(ID) || [];
      ids.forEach(function (marker) { container.appendChild(chipFor(marker, known[marker])); });
      last = match.index + match[0].length;
    }
    if (last < text.length) { container.appendChild(document.createTextNode(text.slice(last))); }
  }

  function insertSources(item, box) {
    var tools = item.querySelector(".message-tools");
    item.insertBefore(box, tools);
  }

  /* Material sources and, in a separate labelled block, the sources from the web. */
  function renderSources(item, citations) {
    Array.prototype.forEach.call(item.querySelectorAll(".message-sources"), function (n) { n.remove(); });
    if (!citations || !citations.length) { return; }
    var material = citations.filter(function (c) { return !isWeb(c, c.marker || ""); });
    var web = citations.filter(function (c) { return isWeb(c, c.marker || "") && isHttpUrl(c.url); });
    if (material.length) {
      var box = el("div", "message-sources");
      box.appendChild(el("p", "message-sources-title", "Sources from the material"));
      var ul = el("ul", "source-list");
      material.forEach(function (c) {
        var li = el("li");
        li.appendChild(chipFor(c.marker, c));
        var where = [c.document_title || "Document", c.location, c.path].filter(Boolean).join(" · ");
        li.appendChild(document.createTextNode(" " + where));
        ul.appendChild(li);
      });
      box.appendChild(ul);
      insertSources(item, box);
    }
    if (web.length) {
      var webBox = el("div", "message-sources message-web");
      webBox.setAttribute("role", "group");
      webBox.setAttribute("aria-label", "From the web");
      var webTitle = el("p", "message-sources-title");
      webTitle.appendChild(icon("globe"));
      webTitle.appendChild(document.createTextNode("From the web"));
      webBox.appendChild(webTitle);
      webBox.appendChild(el("p", "muted", "The course material did not cover this. These sources are not part of it."));
      var webList = el("ul", "source-list");
      web.forEach(function (c) {
        var li = el("li");
        li.appendChild(chipFor(c.marker, c));
        li.appendChild(document.createTextNode(" " + [c.site, c.title].filter(Boolean).join(" - ") + " "));
        li.appendChild(el("span", "muted web-url", c.url));
        webList.appendChild(li);
      });
      webBox.appendChild(webList);
      insertSources(item, webBox);
    }
  }

  function parseCitations(node) {
    try { return JSON.parse(node.getAttribute("data-citations") || "[]"); } catch (e) { return []; }
  }

  function renderFeedback(item, messageId, current) {
    var tools = item.querySelector(".message-tools");
    if (!tools) { return; }
    tools.textContent = "";
    item.setAttribute("data-message-id", messageId);
    var state = current || "";
    var group = el("div", "feedback");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Rate this answer");
    [["up", "Helpful", "thumbup"], ["down", "Not helpful", "thumbdown"]].forEach(function (pair) {
      var button = el("button", "button button-small button-secondary");
      button.appendChild(icon(pair[2]));
      button.appendChild(document.createTextNode(" " + pair[1]));
      button.type = "button";
      button.setAttribute("aria-pressed", state === pair[0] ? "true" : "false");
      button.addEventListener("click", function () {
        var next = state === pair[0] ? null : pair[0];
        sendFeedback(messageId, next).then(function (ok) {
          if (!ok) { return; }
          state = next || "";
          Array.prototype.forEach.call(group.querySelectorAll("button"), function (b, i) {
            b.setAttribute("aria-pressed", state === ["up", "down"][i] ? "true" : "false");
          });
        });
      });
      group.appendChild(button);
    });
    tools.appendChild(group);
  }

  function sendFeedback(messageId, value) {
    return fetch("/api/v1/messages/" + encodeURIComponent(messageId) + "/feedback", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ feedback: value })
    }).then(function (response) {
      if (!response.ok) { setStatus("Your rating could not be saved."); }
      return response.ok;
    }).catch(function () {
      setStatus("Your rating could not be saved.");
      return false;
    });
  }

  function decorateExisting() {
    var items = list.querySelectorAll(".message");
    Array.prototype.forEach.call(items, function (item) {
      var node = item.querySelector(".message-text");
      if (!node) { return; }
      var citations = parseCitations(node);
      renderText(node, node.textContent, citations);
      if (item.getAttribute("data-role") === "assistant") {
        renderSources(item, citations);
        var tools = item.querySelector(".message-tools");
        renderFeedback(item, item.getAttribute("data-message-id"), tools ? tools.getAttribute("data-feedback") : "");
      }
    });
  }

  function addMessage(role, text) {
    var item = el("li", "message message-" + role);
    item.setAttribute("data-role", role);
    item.appendChild(el("p", "message-author", role === "user" ? "You" : "CoursePilot"));
    var body = el("div", "message-text", text);
    item.appendChild(body);
    if (role === "assistant") { item.appendChild(el("div", "message-tools")); }
    list.appendChild(item);
    item.scrollIntoView({ block: "end" });
    return { item: item, body: body };
  }

  /* ---- sidebar ---- */

  function addConversationToSidebar(id, title, scope) {
    if (!conversationList || conversationList.querySelector('[data-conversation-id="' + id + '"]')) { return; }
    var li = el("li", "conversation-item is-current");
    li.setAttribute("data-conversation-id", id);
    var link = el("a");
    link.setAttribute("href", "/chat/" + id);
    link.setAttribute("aria-current", "page");
    link.appendChild(el("span", "conversation-title", title));
    link.appendChild(el("span", "muted conversation-scope", scope));
    li.appendChild(link);
    var del = el("button", "link-button conversation-delete", "Delete");
    del.type = "button";
    del.setAttribute("data-delete-conversation", id);
    del.setAttribute("aria-label", "Delete conversation: " + title);
    li.appendChild(del);
    conversationList.insertBefore(li, conversationList.firstChild);
    if (emptyNote) { emptyNote.hidden = true; }
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target && event.target.closest ? event.target.closest("[data-delete-conversation]") : null;
    if (!trigger) { return; }
    var id = trigger.getAttribute("data-delete-conversation");
    if (!window.confirm("Delete this conversation? This cannot be undone.")) { return; }
    fetch("/api/v1/conversations/" + encodeURIComponent(id), {
      method: "DELETE",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken() }
    }).then(function (response) {
      if (!response.ok && response.status !== 404) { throw new Error(String(response.status)); }
      if (id === conversationId) { window.location.assign("/chat"); return; }
      var item = trigger.closest("li");
      if (item) { item.remove(); }
      if (emptyNote && conversationList && !conversationList.children.length) { emptyNote.hidden = false; }
      setStatus("Conversation deleted.");
    }).catch(function () { setStatus("The conversation could not be deleted."); });
  });

  /* ---- sending ---- */

  function setBusy(value) {
    busy = value;
    sendButton.disabled = value;
    input.disabled = value;
    stopButton.hidden = !value;
    if (moduleSelect && !conversationId) { moduleSelect.disabled = value; }
  }

  function parseFrame(frame) {
    var event = "message";
    var data = [];
    frame.split("\n").forEach(function (line) {
      if (line.indexOf("event:") === 0) { event = line.slice(6).trim(); }
      else if (line.indexOf("data:") === 0) { data.push(line.slice(5).replace(/^ /, "")); }
    });
    if (!data.length) { return null; }
    try { return { event: event, data: JSON.parse(data.join("\n")) }; } catch (e) { return null; }
  }

  function errorText(payload) {
    return payload && payload.error && payload.error.message ? payload.error.message : "The request failed.";
  }

  function send(text) {
    var scopeText = moduleSelect && moduleSelect.selectedIndex >= 0
      ? moduleSelect.options[moduleSelect.selectedIndex].text : "All material";
    addMessage("user", text);
    var answer = addMessage("assistant", "");
    var streamed = "";
    var failed = false;
    var finished = false;
    setBusy(true);
    setStatus("Sending your question…");
    controller = new AbortController();

    var body = { message: text };
    if (conversationId) { body.conversation_id = conversationId; }
    else if (moduleSelect && moduleSelect.value) { body.module_id = moduleSelect.value; }

    function handle(frame) {
      var parsed = parseFrame(frame);
      if (!parsed) { return; }
      var data = parsed.data;
      if (parsed.event === "status") {
        setStatus(data.text || "");
      } else if (parsed.event === "token") {
        streamed += data.text || "";
        answer.body.textContent = streamed;
      } else if (parsed.event === "citations") {
        renderText(answer.body, data.content !== undefined ? data.content : streamed, data.citations);
        renderSources(answer.item, data.citations);
      } else if (parsed.event === "done") {
        finished = true;
        conversationId = data.conversation_id || conversationId;
        app.setAttribute("data-conversation-id", conversationId);
        if (moduleSelect) { moduleSelect.disabled = true; }
        if (window.location.pathname.indexOf("/chat/") !== 0) {
          window.history.replaceState(null, "", "/chat/" + conversationId);
        }
        addConversationToSidebar(
          conversationId,
          text.length > 60 ? text.slice(0, 59) + "…" : text,
          scopeText
        );
        renderFeedback(answer.item, data.message_id, "");
        setStatus("Answer complete.");
      } else if (parsed.event === "error") {
        failed = true;
        if (data.code === "provider_not_configured") { showProviderError(data.message); }
        answer.item.classList.add("message-error");
        answer.body.textContent = data.message || "Something went wrong.";
        setStatus("");
      }
    }

    fetch("/api/v1/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-CSRF-Token": csrfToken()
      },
      body: JSON.stringify(body),
      signal: controller.signal
    }).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return null; }).then(function (payload) {
          failed = true;
          answer.item.classList.add("message-error");
          answer.body.textContent = errorText(payload);
          setStatus("");
        });
      }
      var newId = response.headers.get("X-Conversation-Id");
      if (newId) { conversationId = newId; }
      var reader = response.body.getReader();
      var decoder = new TextDecoder("utf-8");
      var buffer = "";
      function pump() {
        return reader.read().then(function (result) {
          if (result.done) {
            buffer += decoder.decode();
            if (buffer.trim()) { handle(buffer); }
            return;
          }
          buffer += decoder.decode(result.value, { stream: true });
          var index;
          while ((index = buffer.indexOf("\n\n")) !== -1) {
            handle(buffer.slice(0, index));
            buffer = buffer.slice(index + 2);
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (error) {
      if (error && error.name === "AbortError") {
        answer.body.textContent = streamed ? streamed + " …(stopped)" : "(stopped)";
        setStatus("Stopped.");
      } else {
        failed = true;
        answer.item.classList.add("message-error");
        answer.body.textContent = "The connection was lost before the answer finished.";
        setStatus("");
      }
    }).then(function () {
      controller = null;
      setBusy(false);
      if (!finished && !failed && !streamed) { setStatus(""); }
      input.focus();
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text || busy) { return; }
    input.value = "";
    send(text);
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  stopButton.addEventListener("click", function () {
    if (controller) { controller.abort(); }
  });

  decorateExisting();
})();
