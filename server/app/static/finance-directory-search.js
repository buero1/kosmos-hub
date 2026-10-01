(() => {
  const fields = document.querySelectorAll("[data-finance-directory-search]");
  if (!fields.length) return;

  fields.forEach((field) => {
    const input = field.querySelector("[data-finance-directory-search-input]");
    const results = field.querySelector("[data-finance-directory-search-results]");
    const moduleKey = field.dataset.financeModule || "";
    if (!input || !results || !moduleKey) return;

    let requestTimer;
    let pendingRequest;

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
      if (query.length < 2) {
        pendingRequest?.abort();
        closeResults();
        return;
      }
      requestTimer = window.setTimeout(async () => {
        pendingRequest?.abort();
        pendingRequest = new AbortController();
        const parameters = new URLSearchParams({ module_key: moduleKey, q: query });
        try {
          const response = await fetch(`/finance/suggestions?${parameters}`, {
            credentials: "same-origin",
            signal: pendingRequest.signal,
          });
          if (!response.ok || input.value.trim() !== query) return;
          const payload = await response.json();
          showResults(Array.isArray(payload.suggestions) ? payload.suggestions : []);
        } catch (error) {
          if (error.name !== "AbortError") closeResults();
        }
      }, 220);
    };

    input.addEventListener("input", loadResults);
    input.addEventListener("focus", loadResults);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeResults();
      if (event.key === "ArrowDown") {
        const first = results.querySelector("a");
        if (first) {
          event.preventDefault();
          first.focus();
        }
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
