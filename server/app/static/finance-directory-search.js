(() => {
  const fields = document.querySelectorAll("[data-finance-directory-search]");
  if (!fields.length) return;

  fields.forEach((field) => {
    const input = field.querySelector("[data-finance-directory-search-input]");
    const results = field.querySelector("[data-finance-directory-search-results]");
    const moduleKey = field.dataset.financeModule || "";
    const supportsTableSearch = field.dataset.financeTableSearch === "true";
    if (!input || !results || !moduleKey) return;

    let requestTimer;
    let pendingRequest;
    const cache = new Map();

    const closeResults = () => {
      results.replaceChildren();
      results.hidden = true;
      input.setAttribute("aria-expanded", "false");
    };

    const resultDetails = (item) => {
      const amount = item.total_gross || item.net_price || "";
      return [item.party, item.sku, item.status, item.date, amount]
        .filter((value) => value && value !== "-")
        .join(" · ");
    };

    const showResults = (items) => {
      results.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("p");
        empty.className = "finance-directory-search-empty";
        empty.textContent = "Keine passenden Einträge gefunden.";
        results.append(empty);
      } else {
        items.forEach((item) => {
          const link = document.createElement("a");
          link.href = item.href;
          link.setAttribute("role", "option");
          link.className = "customer-search-suggestion";

          const title = document.createElement("strong");
          title.textContent = item.title;
          link.append(title);

          const details = resultDetails(item);
          if (details) {
            const meta = document.createElement("small");
            meta.textContent = details;
            link.append(meta);
          }
          results.append(link);
        });
      }
      results.hidden = false;
      input.setAttribute("aria-expanded", "true");
    };

    const loadResults = () => {
      const query = input.value.trim();
      window.clearTimeout(requestTimer);
      pendingRequest?.abort();
      pendingRequest = null;
      if (query.length < 2) {
        closeResults();
        return;
      }
      const cacheKey = query.toLocaleLowerCase("de-DE");
      if (cache.has(cacheKey)) {
        showResults(cache.get(cacheKey));
        return;
      }
      closeResults();
      requestTimer = window.setTimeout(async () => {
        const controller = new AbortController();
        pendingRequest = controller;
        const parameters = new URLSearchParams({ module_key: moduleKey, q: query });
        try {
          const response = await fetch(`/finance/suggestions?${parameters}`, {
            credentials: "same-origin",
            signal: controller.signal,
          });
          if (!response.ok || input.value.trim() !== query) return;
          const payload = await response.json();
          const items = Array.isArray(payload.suggestions) ? payload.suggestions : [];
          if (cache.size >= 30) cache.delete(cache.keys().next().value);
          cache.set(cacheKey, items);
          showResults(items);
        } catch (error) {
          if (error.name !== "AbortError") closeResults();
        } finally {
          if (pendingRequest === controller) pendingRequest = null;
        }
      }, 100);
    };

    const applyTableSearch = () => {
      const url = new URL(window.location.href);
      const query = input.value.trim();
      url.searchParams.delete("page");
      url.searchParams.delete("created");
      url.searchParams.delete("deleted");
      if (query) url.searchParams.set("q", query);
      else url.searchParams.delete("q");
      url.hash = "";
      window.location.assign(url);
    };

    input.addEventListener("input", loadResults);
    input.addEventListener("focus", loadResults);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeResults();
      if (event.key === "Enter" && supportsTableSearch) {
        event.preventDefault();
        window.clearTimeout(requestTimer);
        pendingRequest?.abort();
        applyTableSearch();
        return;
      }
      if (event.key === "ArrowDown") {
        const first = results.querySelector("a");
        if (first) {
          event.preventDefault();
          first.focus();
        }
      }
    });
    input.addEventListener("search", () => {
      if (supportsTableSearch && !input.value.trim() && new URL(window.location.href).searchParams.has("q")) {
        applyTableSearch();
      }
    });
    results.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      const links = Array.from(results.querySelectorAll("a"));
      const current = links.indexOf(document.activeElement);
      const next = event.key === "ArrowDown" ? current + 1 : current - 1;
      if (links[next]) {
        event.preventDefault();
        links[next].focus();
      } else if (event.key === "ArrowUp" && current === 0) {
        event.preventDefault();
        input.focus();
      }
    });
    document.addEventListener("pointerdown", (event) => {
      if (!field.contains(event.target)) closeResults();
    });
  });
})();
