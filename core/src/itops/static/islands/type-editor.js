/* IT-Ops record-type field editor (core island, ADR 0008).
 *
 * Edits a type's field list: add, remove, reorder fields; the right inputs per kind
 * (options for select, target for link). Writes the list as JSON into a hidden input
 * on submit. The server validates everything (itops.core.types); this is a convenience.
 *
 * Markup contract (rendered by the ui.type_editor macro):
 *   <div data-type-editor data-field="fields" data-locked='["key", ...]' data-kinds='{...}'
 *        data-targets='[{target, label}, ...]'>
 *     <script type="application/json" data-initial>[{key, label, kind, ...}, ...]</script>
 *   </div>
 * Locked keys (already published) keep their key, kind and link target. With targets, a
 * link field picks what it links to from a list (modules' link_targets services).
 */
(function () {
  "use strict";

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? "" : v);
    });
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }

  function slug(text) {
    let s = (text || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
    if (s && !/^[a-z]/.test(s)) s = ("f_" + s).slice(0, 40);
    return s;
  }

  function labelled(text, control, cls, help) {
    const parts = [el("span", { class: "field-label", text }), control];
    if (help) parts.push(el("span", { class: "field-help", text: help }));
    return el("label", { class: "field " + (cls || "") }, parts);
  }

  function init(root) {
    const form = root.closest("form");
    const locked = new Set(JSON.parse(root.dataset.locked || "[]"));
    const kinds = JSON.parse(root.dataset.kinds || "{}");
    const targets = JSON.parse(root.dataset.targets || "[]");
    const initial = JSON.parse(root.querySelector("[data-initial]").textContent || "[]");
    const hidden = el("input", { type: "hidden", name: root.dataset.field || "fields" });
    const list = el("div", { class: "te-fields" });
    const empty = el("p", { class: "muted te-empty", text: "No fields yet. Add the first one." });
    const addBtn = el("button", { type: "button", class: "btn btn-secondary", text: "Add field" });
    root.append(hidden, list, empty, el("p", {}, [addBtn]));

    function refresh() {
      const cards = list.querySelectorAll(".te-card");
      empty.hidden = cards.length > 0;
      cards.forEach((c, i) => {
        c.querySelector(".te-up").disabled = i === 0;
        c.querySelector(".te-down").disabled = i === cards.length - 1;
      });
    }

    function add(f, focus) {
      const isLocked = !!f.key && locked.has(f.key);
      const label = el("input", { class: "input", value: f.label || "", required: true, maxlength: "100" });
      const key = el("input", { class: "input te-key", value: f.key || "", maxlength: "40",
        pattern: "[a-z][a-z0-9_]{0,39}", readonly: isLocked, title: isLocked ? "Published fields keep their ID" : "" });
      let keyEdited = !!f.key;
      key.addEventListener("input", () => { keyEdited = true; });
      label.addEventListener("input", () => { if (!keyEdited && !isLocked) key.value = slug(label.value); });

      const kind = el("select", { class: "input", disabled: isLocked });
      Object.entries(kinds).forEach(([k, help]) => kind.appendChild(el("option", { value: k, text: k, title: help })));
      kind.value = f.kind || "text";

      const required = el("input", { type: "checkbox", checked: !!f.required });
      const help = el("input", { class: "input", value: f.help || "", maxlength: "300",
        placeholder: "e.g. Find it on the vendor's invoice" });
      const options = el("textarea", { class: "input te-options", rows: "4" });
      options.value = (f.options || []).join("\n");
      const multiple = el("input", { type: "checkbox", checked: !!f.multiple });
      let target;
      if (targets.length) {
        target = el("select", { class: "input", disabled: isLocked });
        target.appendChild(el("option", { value: "", text: "Choose what it links to" }));
        const known = targets.map((t) => t.target);
        const choices = f.target && !known.includes(f.target) ? [...targets, { target: f.target, label: f.target }] : targets;
        choices.forEach((t) => target.appendChild(el("option", { value: t.target, text: t.label })));
        target.value = f.target || "";
      } else {
        target = el("input", { class: "input", value: f.target || "", placeholder: "docs or assets.device",
          readonly: isLocked });
      }

      const kindHelp = el("span", { class: "field-help" });
      const optionsBox = labelled("Options (one per line)", options);
      const multipleBox = el("label", { class: "check" }, [multiple, document.createTextNode(" Several values")]);
      const targetBox = labelled("Links to", target);
      const extraRow = el("div", { class: "te-row" }, [optionsBox, targetBox, multipleBox]);
      const title = el("strong", { class: "te-title" });
      function showKind() {
        const k = kind.value;
        extraRow.hidden = !(k === "select" || k === "link");
        kindHelp.textContent = kinds[k] || "";
        optionsBox.hidden = k !== "select";
        multipleBox.hidden = !(k === "select" || k === "link");
        targetBox.hidden = k !== "link";
      }
      kind.addEventListener("change", showKind);

      const up = el("button", { type: "button", class: "btn btn-ghost te-up", title: "Move up", text: "↑" });
      const down = el("button", { type: "button", class: "btn btn-ghost te-down", title: "Move down", text: "↓" });
      const del = el("button", { type: "button", class: "btn btn-ghost te-del", title: "Remove field", text: "Remove" });
      // Field ID: filled in from the label, so it lives in a collapsed section.
      const advanced = el("details", { class: "te-advanced" }, [
        el("summary", { text: isLocked ? "Advanced: field ID (fixed, published)" : "Advanced: field ID" }),
        el("div", { class: "te-advanced-body" }, [
          labelled("Field ID", key, "te-keybox"),
          el("p", { class: "field-help", text: "The name imports and AI tools use for this field. It's filled in from the label, so you usually don't need to touch it. Change it only to match a specific name, for example the column name in an import. It can't change after the type is published." }),
          el("p", { class: "field-help", text: "Rules: lowercase letters, digits and underscores, starting with a letter. Examples: account_number, support_phone, renewal_date, primary_contact, warranty_expires." }),
        ]),
      ]);
      const kindBox = el("label", { class: "field te-kind" }, [el("span", { class: "field-label", text: "Kind" }), kind, kindHelp]);
      const card = el("div", { class: "te-card" }, [
        el("div", { class: "te-head" }, [title,
          isLocked ? el("span", { class: "badge badge-info", text: "Published", title: "Field ID and kind are fixed so stored values keep their meaning" }) : el("span"),
          el("span", { class: "te-actions" }, [up, down, del])]),
        el("div", { class: "te-row" }, [labelled("Label", label, "te-grow"), kindBox]),
        labelled("Help text", help, "",
          "A tip shown under this field when someone fills in a document. Leave blank if the label says it all."),
        extraRow,
        el("label", { class: "check" }, [required, document.createTextNode(" Required: must be filled in before a record can be published")]),
        advanced,
      ]);
      // The header names the field by its label; a fresh one says so until it has a label.
      const setTitle = () => {
        const text = label.value.trim();
        title.textContent = text || "New field";
        title.classList.toggle("muted", !text);
      };
      label.addEventListener("input", setTitle);
      setTitle();
      card._read = () => {
        const out = { key: key.value.trim(), label: label.value.trim(), kind: kind.value, required: required.checked };
        if (help.value.trim()) out.help = help.value.trim();
        if (kind.value === "select") out.options = options.value.split("\n").map((o) => o.trim()).filter(Boolean);
        if ((kind.value === "select" || kind.value === "link") && multiple.checked) out.multiple = true;
        if (kind.value === "link") out.target = target.value.trim();
        return out;
      };
      up.addEventListener("click", () => { if (card.previousElementSibling) list.insertBefore(card, card.previousElementSibling); refresh(); });
      down.addEventListener("click", () => { if (card.nextElementSibling) list.insertBefore(card.nextElementSibling, card); refresh(); });
      del.addEventListener("click", () => { card.remove(); refresh(); });
      list.appendChild(card);
      showKind();
      refresh();
      if (focus) label.focus();
    }

    addBtn.addEventListener("click", () => add({}, true));
    if (form) form.addEventListener("submit", () => {
      hidden.value = JSON.stringify(Array.from(list.querySelectorAll(".te-card")).map((c) => c._read()));
    });
    initial.forEach((f) => add(f, false));
    refresh();
  }

  document.querySelectorAll("[data-type-editor]").forEach(init);
})();
