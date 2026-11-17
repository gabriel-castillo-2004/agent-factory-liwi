(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AgentFactoryPanel = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var PROJECT_ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
  var DEFAULT_REGISTRY_BASE = ".";

  function isValidProjectId(projectId) {
    return typeof projectId === "string" && PROJECT_ID_RE.test(projectId);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function redactDisplayText(value) {
    if (value == null) {
      return "";
    }
    return String(value)
      .replace(/\bbot\d+:[A-Za-z0-9_-]{8,}/g, "bot[REDACTED]")
      .replace(/\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{12,})\b/g, "[REDACTED]")
      .replace(/(Bearer\s+)[A-Za-z0-9_.-]+/gi, "$1[REDACTED]")
      .replace(/(https?:\/\/[^/\s:@]+:)[^/\s@]+(@)/g, "$1[REDACTED]$2")
      .replace(/([?&](?:token|access_token|api_key|apikey|secret|password|auth)=)[^&\s#]+/gi, "$1[REDACTED]");
  }

  function sanitizeLinkUrl(rawUrl) {
    if (typeof rawUrl !== "string") {
      return null;
    }
    var trimmed = rawUrl.trim();
    if (!trimmed) {
      return null;
    }
    if (!/^https?:\/\//i.test(trimmed)) {
      return null;
    }
    try {
      var parsed = new URL(trimmed);
      if (parsed.username || parsed.password) {
        return null;
      }
      if (parsed.protocol === "https:" || parsed.protocol === "http:") {
        return redactDisplayText(trimmed);
      }
      return null;
    } catch (_err) {
      return null;
    }
  }

  function normalizeRegistryBase(rawBase) {
    var candidate = typeof rawBase === "string" ? rawBase.trim() : "";
    if (!candidate) {
      return DEFAULT_REGISTRY_BASE;
    }
    if (/^(javascript|data|vbscript):/i.test(candidate)) {
      throw new Error("Esquema de origen inválido para registry.");
    }
    if (/^https?:\/\//i.test(candidate)) {
      var parsed = new URL(candidate);
      if (parsed.username || parsed.password) {
        throw new Error("El origen de solo lectura no debe incluir credenciales en la URL.");
      }
      return candidate.replace(/\/+$/, "");
    }
    if (candidate === "." || candidate.startsWith("./")) {
      return candidate.replace(/\/+$/, "") || ".";
    }
    throw new Error("El origen configurable debe ser una ruta relativa o URL HTTP(S) de solo lectura.");
  }

  function resolveRegistryUrls(base, projectId) {
    var cleanBase = normalizeRegistryBase(base);
    if (!projectId) {
      return {
        indexUrl: cleanBase + "/registry.json",
        projectUrl: null
      };
    }
    if (!isValidProjectId(projectId)) {
      throw new Error("project_id inválido: " + String(projectId));
    }
    return {
      indexUrl: cleanBase + "/registry.json",
      projectUrl: cleanBase + "/projects/" + encodeURIComponent(projectId) + ".json"
    };
  }

  function parseRegistryCatalog(rawInput) {
    var data = rawInput;
    if (typeof rawInput === "string") {
      try {
        data = JSON.parse(rawInput);
      } catch (_err) {
        throw new Error("El archivo registry.json no contiene JSON válido.");
      }
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("Estructura inválida en registry.json: se esperaba un objeto raíz.");
    }
    var projectsMap = data.projects;
    if (projectsMap == null || typeof projectsMap !== "object" || Array.isArray(projectsMap)) {
      throw new Error("Estructura inválida en registry.json: falta el diccionario 'projects'.");
    }

    var entries = [];
    Object.keys(projectsMap).forEach(function (pid) {
      var rawEntry = projectsMap[pid];
      if (!rawEntry || typeof rawEntry !== "object" || !isValidProjectId(pid)) {
        return;
      }
      var pct = Number(rawEntry.progress_pct);
      var clampedPct = Number.isFinite(pct) ? Math.max(0, Math.min(100, Math.round(pct))) : 0;
      entries.push({
        project_id: pid,
        name: redactDisplayText(rawEntry.name || pid),
        objective: redactDisplayText(rawEntry.objective || ""),
        state: String(rawEntry.state || "IDLE"),
        progress_pct: clampedPct,
        repo_url: sanitizeLinkUrl(rawEntry.repo_url),
        deploy_url: sanitizeLinkUrl(rawEntry.deploy_url),
        plan_hash: rawEntry.plan_hash ? String(rawEntry.plan_hash) : null,
        updated_at: rawEntry.updated_at ? String(rawEntry.updated_at) : null
      });
    });

    entries.sort(function (a, b) {
      return (b.updated_at || "").localeCompare(a.updated_at || "") || a.project_id.localeCompare(b.project_id);
    });

    return {
      version: String(data.version || "1.0"),
      updated_at: data.updated_at ? String(data.updated_at) : null,
      projects: entries
    };
  }

  function filterProjects(entries, query, stateFilter) {
    var normalizedQuery = String(query || "").trim().toLowerCase();
    var targetState = String(stateFilter || "ALL").toUpperCase();
    return (entries || []).filter(function (item) {
      var matchesState = targetState === "ALL" || String(item.state).toUpperCase() === targetState;
      if (!matchesState) {
        return false;
      }
      if (!normalizedQuery) {
        return true;
      }
      var haystack = (
        item.project_id + " " + (item.name || "") + " " + (item.objective || "")
      ).toLowerCase();
      return haystack.indexOf(normalizedQuery) !== -1;
    });
  }

  function toFiniteNonNegativeInt(val) {
    var n = Number(val);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
  }

  function normalizeProjectDetail(rawInput, expectedProjectId) {
    var data = rawInput;
    if (typeof rawInput === "string") {
      try {
        data = JSON.parse(rawInput);
      } catch (_err) {
        throw new Error("El detalle del proyecto no contiene JSON válido.");
      }
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      throw new Error("Estructura inválida en detalle de proyecto.");
    }
    var pid = String(data.project_id || expectedProjectId || "");
    if (!isValidProjectId(pid)) {
      throw new Error("project_id inválido en detalle de proyecto.");
    }

    var links = data.links && typeof data.links === "object" ? data.links : {};
    var tests = data.tests && typeof data.tests === "object" ? data.tests : {};
    var rawEvents = Array.isArray(data.events)
      ? data.events
      : (Array.isArray(data.history) ? data.history : []);
    var events = rawEvents
      .filter(function (ev) { return ev && typeof ev === "object"; })
      .map(function (ev) {
        return {
          id: String(ev.id || ""),
          type: String(ev.type || "EVENT"),
          at: ev.at ? String(ev.at) : "",
          summary: redactDisplayText(
            ev.summary || (ev.payload && (ev.payload.reason || ev.payload.summary)) || ""
          )
        };
      });

    var report = data.build_report && typeof data.build_report === "object" ? data.build_report : null;
    var pct = Number(data.progress_pct);
    return {
      project_id: pid,
      name: redactDisplayText(data.name || pid),
      objective: redactDisplayText(data.objective || ""),
      state: String(data.state || "IDLE"),
      phase: String(data.phase || data.state || "IDLE"),
      progress_pct: Number.isFinite(pct) ? Math.max(0, Math.min(100, Math.round(pct))) : 0,
      plan_hash: data.plan_hash ? String(data.plan_hash) : null,
      agent_invocations: toFiniteNonNegativeInt(data.agent_invocations),
      ci_rounds: toFiniteNonNegativeInt(data.ci_rounds),
      tests: {
        total: toFiniteNonNegativeInt(tests.total),
        passed: toFiniteNonNegativeInt(tests.passed),
        failed: toFiniteNonNegativeInt(tests.failed)
      },
      links: {
        repo: sanitizeLinkUrl(links.repo || data.repo_url),
        deploy: sanitizeLinkUrl(links.deploy || data.deploy_url)
      },
      failure_reason: data.failure_reason ? redactDisplayText(data.failure_reason) : null,
      project_prompt: data.project_prompt ? redactDisplayText(data.project_prompt) : null,
      build_report: report
        ? {
            invocations: toFiniteNonNegativeInt(report.invocations),
            ci_rounds: toFiniteNonNegativeInt(report.ci_rounds),
            failure_cause: report.failure_cause ? redactDisplayText(report.failure_cause) : null
          }
        : null,
      events: events,
      updated_at: data.updated_at ? String(data.updated_at) : null
    };
  }

  function computeKpis(entries) {
    var list = entries || [];
    var delivered = 0;
    var failed = 0;
    var active = 0;
    list.forEach(function (item) {
      if (item.state === "DELIVERED") {
        delivered += 1;
      } else if (item.state === "FAILED") {
        failed += 1;
      } else if (item.state !== "IDLE") {
        active += 1;
      }
    });
    return {
      total: list.length,
      delivered: delivered,
      active: active,
      failed: failed
    };
  }

  function bindBrowserApp() {
    if (typeof document === "undefined") {
      return;
    }

    var state = {
      base: DEFAULT_REGISTRY_BASE,
      catalog: { version: "1.0", updated_at: null, projects: [] },
      selectedProjectId: null
    };

    var errorBanner = document.getElementById("error-banner");
    var baseInput = document.getElementById("registry-base-input");
    var sourceForm = document.getElementById("source-form");
    var refreshBtn = document.getElementById("refresh-btn");
    var searchInput = document.getElementById("search-input");
    var stateFilter = document.getElementById("state-filter");
    var listEl = document.getElementById("projects-list");
    var emptyEl = document.getElementById("projects-empty");
    var detailPlaceholder = document.getElementById("detail-placeholder");
    var detailContent = document.getElementById("detail-content");
    var connectionBadge = document.getElementById("connection-badge");
    var updatedAtBadge = document.getElementById("updated-at-badge");

    function showError(message) {
      if (!errorBanner) {
        return;
      }
      if (!message) {
        errorBanner.textContent = "";
        errorBanner.classList.add("hidden");
        return;
      }
      errorBanner.textContent = redactDisplayText(message);
      errorBanner.classList.remove("hidden");
    }

    function updateConnectionStatus() {
      if (!connectionBadge || typeof navigator === "undefined") {
        return;
      }
      if (navigator.onLine) {
        connectionBadge.textContent = "En línea";
        connectionBadge.className = "pill pill-online";
      } else {
        connectionBadge.textContent = "Offline (caché PWA)";
        connectionBadge.className = "pill pill-offline";
      }
    }

    function renderCatalog() {
      var kpis = computeKpis(state.catalog.projects);
      document.getElementById("kpi-total").textContent = String(kpis.total);
      document.getElementById("kpi-delivered").textContent = String(kpis.delivered);
      document.getElementById("kpi-active").textContent = String(kpis.active);
      document.getElementById("kpi-failed").textContent = String(kpis.failed);

      if (updatedAtBadge) {
        updatedAtBadge.textContent = state.catalog.updated_at
          ? "Actualizado: " + state.catalog.updated_at
          : "Sin fecha de actualización";
      }

      var filtered = filterProjects(
        state.catalog.projects,
        searchInput ? searchInput.value : "",
        stateFilter ? stateFilter.value : "ALL"
      );

      listEl.innerHTML = "";
      if (filtered.length === 0) {
        emptyEl.classList.remove("hidden");
        return;
      }
      emptyEl.classList.add("hidden");

      filtered.forEach(function (item) {
        var card = document.createElement("article");
        card.className = "project-card" + (state.selectedProjectId === item.project_id ? " selected" : "");
        card.setAttribute("role", "listitem");
        card.tabIndex = 0;
        card.innerHTML =
          '<div class="project-head">' +
            '<h3 class="project-title">' + escapeHtml(item.name) + "</h3>" +
            '<span class="state-badge state-' + escapeHtml(item.state) + '">' + escapeHtml(item.state) + "</span>" +
          "</div>" +
          '<p class="project-objective">' + escapeHtml(item.objective || "Sin objetivo registrado.") + "</p>" +
          '<div class="progress-track"><div class="progress-fill" style="width:' + item.progress_pct + '%"></div></div>' +
          '<div class="project-meta">' +
            "<span><code>" + escapeHtml(item.project_id) + "</code></span>" +
            "<span>Avance: <strong>" + item.progress_pct + "%</strong></span>" +
          "</div>";

        card.addEventListener("click", function () {
          loadProjectDetail(item.project_id);
        });
        card.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            loadProjectDetail(item.project_id);
          }
        });
        listEl.appendChild(card);
      });
    }

    function renderDetail(detail) {
      detailPlaceholder.classList.add("hidden");
      detailContent.classList.remove("hidden");

      var repoLink = detail.links.repo
        ? '<a class="link-btn" href="' + escapeHtml(detail.links.repo) + '" target="_blank" rel="noopener noreferrer">Repositorio</a>'
        : "";
      var deployLink = detail.links.deploy
        ? '<a class="link-btn" href="' + escapeHtml(detail.links.deploy) + '" target="_blank" rel="noopener noreferrer">Abrir App</a>'
        : "";
      var failureHtml = detail.failure_reason
        ? '<div class="alert alert-error"><strong>Causa:</strong> ' + escapeHtml(detail.failure_reason) + "</div>"
        : "";
      var reportCauseHtml = (detail.build_report && detail.build_report.failure_cause)
        ? '<div class="alert alert-warning"><strong>Causa informe build:</strong> ' + escapeHtml(detail.build_report.failure_cause) + "</div>"
        : "";
      var promptHtml = detail.project_prompt
        ? '<h4 class="section-title">Prompt Maestro (PROJECT_PROMPT.md)</h4><pre class="prompt-box"><code>' + escapeHtml(detail.project_prompt) + "</code></pre>"
        : "";
      var eventsHtml = detail.events.length
        ? detail.events.map(function (ev) {
            return (
              '<li class="event-item">' +
                '<div class="event-head"><span>' + escapeHtml(ev.type) + "</span><span>" + escapeHtml(ev.at) + "</span></div>" +
                (ev.summary ? '<div class="event-summary">' + escapeHtml(ev.summary) + "</div>" : "") +
              "</li>"
            );
          }).join("")
        : '<li class="empty-state">No hay historial de eventos publicado para este proyecto.</li>';

      detailContent.innerHTML =
        '<div class="project-head">' +
          '<h3 class="project-title">' + escapeHtml(detail.name) + "</h3>" +
          '<span class="state-badge state-' + escapeHtml(detail.state) + '">' + escapeHtml(detail.state) + "</span>" +
        "</div>" +
        '<p class="project-objective">' + escapeHtml(detail.objective) + "</p>" +
        '<div class="progress-track"><div class="progress-fill" style="width:' + detail.progress_pct + '%"></div></div>' +
        '<div class="project-meta">' +
          "<span>Fase: <strong>" + escapeHtml(detail.phase) + "</strong></span>" +
          "<span>Avance: <strong>" + detail.progress_pct + "%</strong></span>" +
          "<span>Invocaciones: <strong>" + detail.agent_invocations + "</strong></span>" +
          "<span>Rondas CI: <strong>" + detail.ci_rounds + "</strong></span>" +
        "</div>" +
        '<div class="links-row">' + repoLink + deployLink + "</div>" +
        failureHtml +
        reportCauseHtml +
        promptHtml +
        '<h4 class="section-title">Historial de eventos sanitizados</h4>' +
        '<ul class="events-list">' + eventsHtml + "</ul>";
    }

    function fetchReadOnlyJson(url) {
      return fetch(url, { method: "GET", headers: { Accept: "application/json" } })
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status + " al consultar " + url);
          }
          return resp.text();
        })
        .then(function (rawText) {
          return JSON.parse(rawText);
        });
    }

    function loadCatalog() {
      showError("");
      var urls;
      try {
        urls = resolveRegistryUrls(state.base);
      } catch (err) {
        showError(err.message);
        return Promise.resolve();
      }
      return fetchReadOnlyJson(urls.indexUrl)
        .then(function (rawData) {
          state.catalog = parseRegistryCatalog(rawData);
          renderCatalog();
        })
        .catch(function (err) {
          showError("No se pudo cargar registry.json: " + (err && err.message ? err.message : String(err)));
        });
    }

    function loadProjectDetail(projectId) {
      state.selectedProjectId = projectId;
      renderCatalog();
      showError("");
      var urls;
      try {
        urls = resolveRegistryUrls(state.base, projectId);
      } catch (err) {
        showError(err.message);
        return Promise.resolve();
      }
      return fetchReadOnlyJson(urls.projectUrl)
        .then(function (rawDetail) {
          renderDetail(normalizeProjectDetail(rawDetail, projectId));
        })
        .catch(function (err) {
          var fallback = state.catalog.projects.find(function (p) { return p.project_id === projectId; });
          if (fallback) {
            renderDetail(normalizeProjectDetail(fallback, projectId));
          }
          showError("No se pudo cargar projects/" + projectId + ".json: " + (err && err.message ? err.message : String(err)));
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
      var params = new URLSearchParams(window.location.search);
      var queryBase = params.get("registryBase");
      var savedBase = null;
      try {
        savedBase = window.localStorage.getItem("af_registry_base");
      } catch (_err) {
        savedBase = null;
      }
      var initialBase = queryBase || savedBase || DEFAULT_REGISTRY_BASE;
      try {
        state.base = normalizeRegistryBase(initialBase);
      } catch (_err) {
        state.base = DEFAULT_REGISTRY_BASE;
      }
      if (baseInput) {
        baseInput.value = state.base === "." ? "" : state.base;
      }

      if (sourceForm) {
        sourceForm.addEventListener("submit", function (ev) {
          ev.preventDefault();
          try {
            state.base = normalizeRegistryBase(baseInput ? baseInput.value : "");
            try {
              window.localStorage.setItem("af_registry_base", state.base);
            } catch (_storageErr) {
              /* noop */
            }
            loadCatalog();
          } catch (err) {
            showError(err.message);
          }
        });
      }

      if (refreshBtn) {
        refreshBtn.addEventListener("click", loadCatalog);
      }
      if (searchInput) {
        searchInput.addEventListener("input", renderCatalog);
      }
      if (stateFilter) {
        stateFilter.addEventListener("change", renderCatalog);
      }

      window.addEventListener("online", updateConnectionStatus);
      window.addEventListener("offline", updateConnectionStatus);
      updateConnectionStatus();

      if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
        navigator.serviceWorker.register("./sw.js").catch(function () {
          /* registro best-effort */
        });
      }

      loadCatalog();
    });
  }

  bindBrowserApp();

  return {
    PROJECT_ID_RE: PROJECT_ID_RE,
    isValidProjectId: isValidProjectId,
    escapeHtml: escapeHtml,
    redactDisplayText: redactDisplayText,
    sanitizeLinkUrl: sanitizeLinkUrl,
    normalizeRegistryBase: normalizeRegistryBase,
    resolveRegistryUrls: resolveRegistryUrls,
    parseRegistryCatalog: parseRegistryCatalog,
    filterProjects: filterProjects,
    normalizeProjectDetail: normalizeProjectDetail,
    computeKpis: computeKpis
  };
});
