/* IT-Ops section editor (core island, ADR 0003).
 *
 * Section-based authoring (ITGlue/Word style, carried over from Loretome): add typed
 * sections, drag to reorder, a live Contents pane, paste/drop screenshots. Reads and
 * writes the platform block format (itops.sdk.content) as JSON in a hidden input.
 * The server sanitizes everything on save; this editor is a convenience, not a gate.
 *
 * Markup contract (rendered by the ui.section_editor macro):
 *   <div data-section-editor data-field="content" data-upload="/api/docs/files">
 *     <script type="application/json" data-initial>{"blocks": [...]}</script>
 *   </div>
 */
(function () {
  "use strict";

  const TYPES = {
    heading: { label: "Heading", icon: "H" },
    text: { label: "Text", icon: "¶" },
    step: { label: "Step", icon: "#" },
    html: { label: "HTML block", icon: "</>" },
  };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    });
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }

  function init(root) {
    const form = root.closest("form");
    const uploadUrl = root.dataset.upload || "";
    const hidden = el("input", { type: "hidden", name: root.dataset.field || "content" });
    const initial = JSON.parse(root.querySelector("[data-initial]").textContent || "{}");
    let activeBody = null;
    let seq = 0;

    // ---- layout -------------------------------------------------------------------
    const toc = el("ol", { class: "se-toc-list" });
    const tocPane = el("aside", { class: "se-toc" }, [
      el("h3", { text: "Contents" }),
      toc,
      el("p", { class: "se-hint", text: "Headings appear here. Drag ⠿ to reorder." }),
    ]);
    const status = el("p", { class: "se-status", role: "status" });
    const toolbar = el("div", { class: "se-toolbar", role: "toolbar", "aria-label": "Formatting" });
    [
      ["bold", "B", "Bold"],
      ["italic", "I", "Italic"],
      ["insertUnorderedList", "• List", "Bulleted list"],
      ["insertOrderedList", "1. List", "Numbered list"],
      ["link", "Link", "Add link"],
      ["image", "Image", "Insert image"],
    ].forEach(([cmd, label, title]) => {
      const b = el("button", { type: "button", class: "btn btn-ghost se-tb", title, text: label });
      b.addEventListener("mousedown", (e) => e.preventDefault()); // keep text selection
      b.addEventListener("click", () => command(cmd));
      toolbar.appendChild(b);
    });
    const sections = el("div", { class: "se-sections" });
    const addBar = el("div", { class: "se-add" }, [el("span", { class: "se-hint", text: "Add a section:" })]);
    Object.entries(TYPES).forEach(([type, t]) => {
      const b = el("button", { type: "button", class: "btn", text: t.icon + " " + t.label });
      b.addEventListener("click", () => add(type, {}, true));
      addBar.appendChild(b);
    });
    const fileInput = el("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp", hidden: "" });
    fileInput.addEventListener("change", () => {
      if (fileInput.files[0]) upload(fileInput.files[0]);
      fileInput.value = "";
    });
    const main = el("div", { class: "se-main" }, [toolbar, sections, addBar, status]);
    root.append(el("div", { class: "se-layout" }, [tocPane, main]), hidden, fileInput);

    // ---- sections -----------------------------------------------------------------
    function card(type) {
      const c = el("div", { class: "se-card", id: "se-" + ++seq });
      c.dataset.type = type;
      const handle = el("span", { class: "se-handle", title: "Drag to reorder", text: "⠿" });
      const del = el("button", { type: "button", class: "btn btn-ghost se-del", title: "Delete section", text: "✕" });
      del.addEventListener("click", () => { c.remove(); rebuild(); });
      c.appendChild(el("div", { class: "se-card-head" }, [
        handle, el("span", { class: "badge badge-neutral se-type", text: TYPES[type].label }), del,
      ]));
      handle.addEventListener("mousedown", () => { c.draggable = true; });
      c.addEventListener("dragstart", (e) => { dragging = c; c.classList.add("se-dragging"); e.dataTransfer.effectAllowed = "move"; });
      c.addEventListener("dragend", () => { c.classList.remove("se-dragging"); c.draggable = false; dragging = null; rebuild(); });
      return c;
    }

    function richBody(html, placeholder) {
      const body = el("div", { class: "se-body input", contenteditable: "true", "data-placeholder": placeholder });
      body.innerHTML = html || "";
      body.addEventListener("focusin", () => {
        activeBody = body;
        // Start typing inside a real paragraph, not as a bare text line.
        if (!body.innerHTML.trim()) {
          body.innerHTML = "<p><br></p>";
          const range = document.createRange();
          range.setStart(body.firstChild, 0);
          range.collapse(true);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
        }
      });
      body.addEventListener("paste", (e) => {
        const items = (e.clipboardData || {}).items || [];
        for (const it of items) {
          if (it.type && it.type.startsWith("image/")) { e.preventDefault(); activeBody = body; upload(it.getAsFile()); return; }
        }
      });
      body.addEventListener("dragover", (e) => { if (!dragging) { e.preventDefault(); body.classList.add("se-drop"); } });
      body.addEventListener("dragleave", () => body.classList.remove("se-drop"));
      body.addEventListener("drop", (e) => {
        body.classList.remove("se-drop");
        const f = (e.dataTransfer.files || [])[0];
        if (f && f.type.startsWith("image/")) { e.preventDefault(); activeBody = body; upload(f); }
      });
      return body;
    }

    function add(type, block, focus) {
      const c = card(type);
      if (type === "heading") {
        const input = el("input", { class: "input se-heading", placeholder: "Heading text" });
        input.value = block.text || "";
        input.addEventListener("input", rebuild);
        const level = el("select", { class: "input se-level", "aria-label": "Heading level" }, [
          el("option", { value: "2", text: "H2" }), el("option", { value: "3", text: "H3" }),
        ]);
        level.value = String(block.level || 2);
        level.addEventListener("change", rebuild);
        c.appendChild(el("div", { class: "se-row" }, [input, level]));
      } else if (type === "html") {
        const ta = el("textarea", { class: "input se-html", spellcheck: "false", placeholder: "<h2>Rich HTML, kept as written…</h2>" });
        ta.value = block.html || "";
        c.appendChild(ta);
        c.appendChild(el("p", { class: "se-hint", text: "Kept exactly as written (scripts and styles are removed on save)." }));
      } else {
        if (type === "step") {
          const title = el("input", { class: "input se-step-title", placeholder: "Step title (optional)", "aria-label": "Step title" });
          title.value = block.title || "";
          title.addEventListener("input", rebuild);
          c.appendChild(title);
        }
        c.appendChild(richBody(block.html, type === "step" ? "Describe this step… screenshots welcome." : "Write… paste or drop screenshots."));
        if (type === "step") {
          const dur = el("input", { class: "input se-duration", type: "number", min: "0", placeholder: "—" });
          if (block.duration_min != null) dur.value = block.duration_min;
          const label = el("label", { class: "se-hint" }, [document.createTextNode("Duration "), dur, document.createTextNode(" min")]);
          c.appendChild(label);
        }
      }
      sections.appendChild(c);
      rebuild();
      if (focus) (c.querySelector(".se-heading, .se-body, .se-html") || c).focus();
    }

    function rebuild() {
      toc.innerHTML = "";
      sections.querySelectorAll('.se-card[data-type="heading"], .se-card[data-type="step"]').forEach((c) => {
        const heading = c.dataset.type === "heading";
        const text = heading ? (c.querySelector(".se-heading").value.trim() || "Untitled heading")
                             : c.querySelector(".se-step-title").value.trim();
        if (!text) return;  // untitled steps aren't listed (same as the reader)
        const a = el("a", { href: "#" + c.id, text });
        a.addEventListener("click", (e) => { e.preventDefault(); c.scrollIntoView({ behavior: "smooth", block: "start" }); });
        const sub = heading ? c.querySelector(".se-level").value === "3" : true;
        toc.appendChild(el("li", { class: sub ? "se-toc-sub" : "" }, [a]));
      });
      let n = 0;
      sections.querySelectorAll('.se-card[data-type="step"] .se-type').forEach((b) => { b.textContent = "Step " + ++n; });
    }

    // ---- drag to reorder ----------------------------------------------------------
    let dragging = null;
    sections.addEventListener("dragover", (e) => {
      if (!dragging) return;
      e.preventDefault();
      const after = Array.from(sections.querySelectorAll(".se-card:not(.se-dragging)"))
        .find((c) => e.clientY < c.getBoundingClientRect().top + c.offsetHeight / 2);
      if (after) sections.insertBefore(dragging, after); else sections.appendChild(dragging);
    });

    // ---- formatting + images ------------------------------------------------------
    function command(cmd) {
      if (!activeBody) { say("Click into a Text or Step section first."); return; }
      activeBody.focus();
      if (cmd === "link") {
        const url = window.prompt("Link address (https://…)");
        if (url) document.execCommand("createLink", false, url);
      } else if (cmd === "image") {
        fileInput.click();
      } else {
        document.execCommand(cmd, false, null);
      }
    }

    async function upload(file) {
      if (!activeBody || !uploadUrl) return;
      const target = activeBody;
      say("Uploading " + (file.name || "image") + "…");
      const fd = new FormData();
      fd.append("file", file);
      try {
        const res = await fetch(uploadUrl, { method: "POST", body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { say(data.detail || "Upload failed."); return; }
        target.focus();
        document.execCommand("insertHTML", false, '<img src="' + data.url + '" alt="">');
        say("");
      } catch (e) {
        say("Upload failed.");
      }
    }

    function say(msg) { status.textContent = msg; }

    // ---- serialize ----------------------------------------------------------------
    function serialize() {
      const blocks = [];
      sections.querySelectorAll(".se-card").forEach((c) => {
        const type = c.dataset.type;
        if (type === "heading") {
          blocks.push({ type, text: c.querySelector(".se-heading").value, level: Number(c.querySelector(".se-level").value) });
        } else if (type === "html") {
          blocks.push({ type, html: c.querySelector(".se-html").value });
        } else if (type === "step") {
          const d = c.querySelector(".se-duration").value.trim();
          blocks.push({ type, html: c.querySelector(".se-body").innerHTML, duration_min: d === "" ? null : Number(d),
                        title: c.querySelector(".se-step-title").value });
        } else {
          blocks.push({ type, html: c.querySelector(".se-body").innerHTML });
        }
      });
      return { blocks };
    }
    if (form) form.addEventListener("submit", () => { hidden.value = JSON.stringify(serialize()); });

    // ---- start ----------------------------------------------------------------------
    const blocks = (initial && initial.blocks) || [];
    if (blocks.length) blocks.forEach((b) => { if (TYPES[b.type]) add(b.type, b, false); });
    else add("text", {}, false);
  }

  // Enter makes a new <p>, not a <div> (the server tidies either way).
  document.execCommand("defaultParagraphSeparator", false, "p");
  document.querySelectorAll("[data-section-editor]").forEach(init);
})();
