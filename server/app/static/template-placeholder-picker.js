(() => {
  function initPicker(picker) {
    const form = picker.closest("form");
    const source = picker.querySelector("[data-template-placeholder-source]");
    const search = picker.querySelector("[data-template-placeholder-search]");
    const results = picker.querySelector("[data-template-placeholder-results]");
    const empty = picker.querySelector("[data-template-placeholder-empty]");
    const options = Array.from(picker.querySelectorAll("[data-template-placeholder-option]"));
    const sourceOptions = Array.from(source.querySelectorAll("[data-template-placeholder-source-option]"));
    const context = form?.querySelector("[data-email-template-context]");
    const subject = form?.querySelector('input[name="subject"]');
    const editor = form?.querySelector("[data-email-compose-content]");
    if (!form || !source || !search || !results || !editor) return;

    let lastTarget = editor;
    let subjectStart = 0;
    let subjectEnd = 0;
    let editorRange = null;
    let open = false;

    const normalize = (value) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase("de").replace(/ß/g, "ss");

    function matchesContext(option) {
      const allowed = (option.dataset.contexts || "").split(",").filter(Boolean);
      return !context || allowed.length === 0 || allowed.includes(context.value);
    }

    function updateSources() {
      sourceOptions.forEach((sourceOption) => {
        if (!sourceOption.value) {
          sourceOption.hidden = false;
          sourceOption.disabled = false;
          return;
        }
        const available = options.some((option) => option.dataset.source === sourceOption.value && matchesContext(option));
        sourceOption.hidden = !available;
        sourceOption.disabled = !available;
      });
      if (source.selectedOptions[0]?.disabled) {
        source.value = sourceOptions.find((option) => option.value && !option.disabled)?.value || "";
      }
    }

    function render() {
      const query = normalize(search.value.trim());
      let count = 0;
      options.forEach((option) => {
        const matchesSource = !source.value || option.dataset.source === source.value;
        const matchesQuery = !query || normalize(option.textContent || "").includes(query);
        option.hidden = !matchesContext(option) || !matchesSource || !matchesQuery || (option.hasAttribute("data-body-only") && lastTarget === subject);
        if (!option.hidden) count += 1;
      });
      empty.hidden = count !== 0;
      results.hidden = !open;
      search.setAttribute("aria-expanded", String(open));
    }

    function rememberSubject() {
      lastTarget = subject;
      subjectStart = subject.selectionStart ?? subject.value.length;
      subjectEnd = subject.selectionEnd ?? subjectStart;
      render();
    }
    if (subject) {
      ["focus", "click", "input", "keyup", "select"].forEach((eventName) => subject.addEventListener(eventName, rememberSubject));
    }

    function attachEditorSelection() {
      const frame = editor.closest("[data-email-rich-editor]")?.querySelector(".jodit-wysiwyg_iframe");
      const doc = frame?.contentDocument;
      if (!doc?.body || doc.body.dataset.templatePickerTracked) return Boolean(doc?.body);
      doc.body.dataset.templatePickerTracked = "true";
      const rememberEditor = () => {
        const selection = frame.contentWindow?.getSelection();
        if (selection?.rangeCount && doc.body.contains(selection.getRangeAt(0).commonAncestorContainer)) {
          editorRange = selection.getRangeAt(0).cloneRange();
          lastTarget = editor;
          render();
        }
      };
      doc.addEventListener("selectionchange", rememberEditor);
      doc.body.addEventListener("focusin", () => { lastTarget = editor; render(); });
      doc.body.addEventListener("mouseup", rememberEditor);
      doc.body.addEventListener("keyup", rememberEditor);
      return true;
    }

    if (!attachEditorSelection()) {
      const observer = new MutationObserver(() => {
        if (attachEditorSelection()) observer.disconnect();
      });
      observer.observe(editor.closest("[data-email-rich-editor]") || form, { childList: true, subtree: true });
    }
    editor.addEventListener("focus", () => { lastTarget = editor; render(); });

    function insert(token) {
      if (lastTarget === subject && subject) {
        subject.setRangeText(token, subjectStart, subjectEnd, "end");
        subject.dispatchEvent(new Event("input", { bubbles: true }));
        subject.focus();
        rememberSubject();
        return;
      }
      const frame = editor.closest("[data-email-rich-editor]")?.querySelector(".jodit-wysiwyg_iframe");
      const doc = frame?.contentDocument;
      const selection = frame?.contentWindow?.getSelection();
      if (doc?.body && selection) {
        const range = editorRange && doc.body.contains(editorRange.commonAncestorContainer)
          ? editorRange.cloneRange()
          : doc.createRange();
        if (!editorRange || !doc.body.contains(editorRange.commonAncestorContainer)) {
          range.selectNodeContents(doc.body);
          range.collapse(false);
        }
        range.deleteContents();
        const node = doc.createTextNode(token);
        range.insertNode(node);
        range.setStartAfter(node);
        range.collapse(true);
        selection.removeAllRanges();
        selection.addRange(range);
        editorRange = range.cloneRange();
        doc.body.focus();
        doc.body.dispatchEvent(new frame.contentWindow.Event("input", { bubbles: true }));
        return;
      }
      const start = editor.selectionStart ?? editor.value.length;
      const end = editor.selectionEnd ?? start;
      editor.setRangeText(token, start, end, "end");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      editor.focus();
    }

    source.addEventListener("change", () => { open = true; render(); search.focus(); });
    if (context) {
      context.addEventListener("change", () => {
        updateSources();
        search.value = "";
        open = false;
        render();
      });
    }
    search.addEventListener("focus", () => { open = true; render(); });
    search.addEventListener("input", () => { open = true; render(); });
    search.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { open = false; render(); return; }
      if (event.key === "ArrowDown") {
        const first = options.find((option) => !option.hidden);
        if (first) { event.preventDefault(); first.focus(); }
      }
      if (event.key === "Enter") {
        const first = options.find((option) => !option.hidden);
        if (first) { event.preventDefault(); first.click(); }
      }
    });
    options.forEach((option) => {
      option.addEventListener("click", () => {
        insert(option.dataset.token || "");
        open = false;
        render();
      });
      option.addEventListener("keydown", (event) => {
        if (event.key === "Escape") { open = false; render(); search.focus(); }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          const visible = options.filter((item) => !item.hidden);
          const next = visible[visible.indexOf(option) + (event.key === "ArrowDown" ? 1 : -1)];
          if (next) { event.preventDefault(); next.focus(); }
        }
      });
    });
    document.addEventListener("click", (event) => {
      if (!picker.contains(event.target)) { open = false; render(); }
    });
    updateSources();
    render();
  }

  function init() {
    document.querySelectorAll("[data-template-placeholder-picker]").forEach(initPicker);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
