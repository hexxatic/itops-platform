// Shows a stored file next to what was taken from it (components.html `document_viewer`,
// ADR 0015). PDFs are drawn with the vendored PDF.js, one page at a time, onto a canvas: no
// script or form in the file ever runs. Other files show their extracted text by page.
// Any element with data-viewer="<viewer id>" data-show-page="<n>" data-show-quote="<text>"
// jumps the viewer to that page and highlights the quote.
(() => {
  const squash = (s) => s.replace(/\s+/g, " ").trim().toLowerCase();

  function setup(root) {
    if (root.dataset.ready) return;
    root.dataset.ready = "1";
    const kind = root.dataset.kind;
    const pagesBox = root.querySelector("[data-pages]");
    const info = root.querySelector("[data-pageinfo]");
    const zoomInfo = root.querySelector("[data-zoominfo]");
    const note = root.querySelector("[data-note]");
    let page = 1;
    let total = Number(root.dataset.total || 1);
    let zoom = 1;
    let quote = "";
    let pdf = null;
    let rendering = Promise.resolve();

    const say = (text) => {
      note.textContent = text || "";
      note.hidden = !text;
    };
    const showInfo = () => {
      info.textContent = `Page ${page} of ${total}`;
      zoomInfo.textContent = `${Math.round(zoom * 100)}%`;
    };

    // ---- PDF -----------------------------------------------------------------------------
    async function loadPdf() {
      const lib = await import(root.dataset.pdfjs);
      lib.GlobalWorkerOptions.workerSrc = root.dataset.worker;
      const data = await (await fetch(root.dataset.src, { credentials: "same-origin" })).arrayBuffer();
      pdf = await lib.getDocument({ data, isEvalSupported: false }).promise;
      total = pdf.numPages;
    }

    function markPdf(content, viewport, layer) {
      if (!quote) return true;
      // Join the page's text pieces; remember which piece each character came from.
      let joined = "";
      const owner = [];
      content.items.forEach((item, i) => {
        const s = squash(item.str || "");
        if (!s) return;
        if (joined) {
          joined += " ";
          owner.push(-1);
        }
        for (const ch of s) {
          joined += ch;
          owner.push(i);
        }
      });
      const at = joined.indexOf(squash(quote));
      if (at < 0) return false;
      const hit = new Set(owner.slice(at, at + squash(quote).length).filter((i) => i >= 0));
      for (const i of hit) {
        const item = content.items[i];
        const [, , c, d, x, y] = item.transform;
        const h = item.height || Math.hypot(c, d);
        // PDF space -> screen with the viewport's matrix (PDF.js 6 has no helper for it).
        const [a, b, cc, dd, e, f] = viewport.transform;
        const at = (px, py) => [a * px + cc * py + e, b * px + dd * py + f];
        const [x1, y1] = at(x, y);
        const [x2, y2] = at(x + item.width, y + h);
        const box = document.createElement("div");
        box.className = "doc-mark";
        Object.assign(box.style, {
          left: `${Math.min(x1, x2)}px`, top: `${Math.min(y1, y2)}px`,
          width: `${Math.abs(x2 - x1)}px`, height: `${Math.abs(y2 - y1)}px`,
        });
        layer.appendChild(box);
      }
      layer.querySelector(".doc-mark")?.scrollIntoView({ block: "center", behavior: "smooth" });
      return true;
    }

    async function renderPdf() {
      if (!pdf) await loadPdf();
      const p = await pdf.getPage(page);
      const fit = Math.max(1, pagesBox.clientWidth - 2) / p.getViewport({ scale: 1 }).width;
      const viewport = p.getViewport({ scale: fit * zoom });
      const ratio = window.devicePixelRatio || 1;
      const wrap = document.createElement("div");
      wrap.className = "doc-pdf-page";
      wrap.style.width = `${viewport.width}px`;
      wrap.style.height = `${viewport.height}px`;
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      const layer = document.createElement("div");
      layer.className = "doc-marks";
      wrap.append(canvas, layer);
      await p.render({
        canvasContext: canvas.getContext("2d"),
        viewport,
        transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0],
      }).promise;
      pagesBox.replaceChildren(wrap);
      const found = markPdf(await p.getTextContent(), viewport, layer);
      say(found ? "" : "The quote is on this page; it couldn't be marked exactly.");
    }

    // ---- text (Outlook, Word, Excel, ...) ------------------------------------------------
    function renderText() {
      const sections = pagesBox.querySelectorAll("[data-page]");
      sections.forEach((s) => {
        const pre = s.querySelector("pre");
        pre.textContent = pre.textContent; // drops an earlier <mark>
      });
      const section = pagesBox.querySelector(`[data-page="${page}"]`);
      if (!section) return;
      const pre = section.querySelector("pre");
      if (quote) {
        const words = quote.trim().split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
        const m = new RegExp(words.join("\\s+"), "i").exec(pre.textContent);
        if (m) {
          const node = pre.firstChild;
          const after = node.splitText(m.index);
          after.splitText(m[0].length);
          const mark = document.createElement("mark");
          mark.className = "doc-text-mark";
          after.replaceWith(mark);
          mark.appendChild(after);
          mark.scrollIntoView({ block: "center", behavior: "smooth" });
          say("");
          return;
        }
        say("The quote is on this page; it couldn't be marked exactly.");
      }
      section.scrollIntoView({ block: "start", behavior: "smooth" });
    }

    const render = () => {
      showInfo();
      rendering = rendering.then(kind === "pdf" ? renderPdf : async () => renderText()).catch((e) => {
        say("This file couldn't be shown here. Download it instead.");
        console.error(e);
      });
      return rendering;
    };

    root.querySelector("[data-prev]").addEventListener("click", () => {
      if (page > 1) { page -= 1; quote = ""; render(); }
    });
    root.querySelector("[data-next]").addEventListener("click", () => {
      if (page < total) { page += 1; quote = ""; render(); }
    });
    root.querySelector("[data-zoom-in]").addEventListener("click", () => {
      zoom = Math.min(3, zoom + 0.25); render();
    });
    root.querySelector("[data-zoom-out]").addEventListener("click", () => {
      zoom = Math.max(0.5, zoom - 0.25); render();
    });
    root.querySelector("[data-full]").addEventListener("click", () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else root.requestFullscreen?.();
    });
    document.addEventListener("fullscreenchange", () => kind === "pdf" && render());

    root.show = (n, text) => {
      page = Math.min(Math.max(1, n || 1), total || 1);
      quote = text || "";
      render();
      if (window.matchMedia("(max-width: 960px)").matches) {
        root.scrollIntoView({ block: "start", behavior: "smooth" });
      }
    };
    render();
  }

  document.addEventListener("click", (e) => {
    const trigger = e.target.closest("[data-show-page][data-viewer]");
    if (!trigger) return;
    e.preventDefault();
    const viewer = document.getElementById(trigger.dataset.viewer);
    if (viewer && viewer.show) viewer.show(Number(trigger.dataset.showPage), trigger.dataset.showQuote);
  });
  const all = () => document.querySelectorAll("[data-doc-viewer]").forEach(setup);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", all);
  else all();
})();
