// Регистрация обращения с подбором похожих (ПЦ1; Ф1–Ф10).
//
// Подбор запускается при вводе с задержкой; незавершённый запрос
// отменяется следующим (AbortController), устаревшие ответы
// отбрасываются. Пока список не обновлён после правки текста, связать
// обращение с кандидатом нельзя.
//
// Черновик (Ф2) сохраняется на сервере и в localStorage, поэтому текст не
// теряется при ошибке подбора или обрыве соединения. Сохранения на сервер
// идут строго по одному; локальная копия помечается «есть несохранённые
// изменения», пока сервер не подтвердил именно этот текст. Часы браузера и
// сервера не сравниваются. После регистрации черновик больше не пишется.
"use strict";

(() => {
  const SEARCH_DELAY = 700;  // мс после последнего нажатия
  const DRAFT_DELAY = 1000;
  const MIN_QUERY = 5;  // символов в заголовке и описании вместе

  const page = JSON.parse(document.getElementById("page-data").textContent);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const form = document.getElementById("ticket-form");
  const $ = (id) => document.getElementById(id);
  const storageKey = `spdo:new-ticket:${page.user_id}`;

  const state = {
    draftId: null,
    serverUpdatedAt: null,  // версия черновика на сервере, на которой основан текст
    saving: null,  // Promise текущего сохранения черновика
    saveAgain: false,
    registered: false,
    sessionId: null,
    searchKey: null,  // запрос, по которому получен показанный список
    lastQuery: null,
    searchSeq: 0,
    controller: null,
    searchTimer: null,
    draftTimer: null,
    busy: false,
  };

  // --- запросы --------------------------------------------------------

  function message(data) {
    const detail = data && data.detail;
    if (Array.isArray(detail)) {  // ошибки проверки FastAPI
      return "Проверьте поля: " + detail.map((d) => d.msg).join("; ");
    }
    return detail ? String(detail) : "Ошибка сервера";
  }

  async function api(method, url, body, signal) {
    const resp = await fetch(url, {
      method,
      signal,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (resp.status === 401) {
      window.location.href = "/login?next=" + encodeURIComponent("/tickets/new");
      throw new Error("Требуется вход");
    }
    const data = resp.status === 204 ? null : await resp.json().catch(() => null);
    if (!resp.ok) {
      const error = new Error(message(data));
      error.status = resp.status;
      throw error;
    }
    return data;
  }

  // --- форма и черновик ----------------------------------------------

  function idOrNull(value) {
    return value === "" || value === null || value === undefined ? null : Number(value);
  }

  function values() {
    return {
      title: form.title.value,
      description: form.description.value,
      category_id: idOrNull(form.category_id.value),
      component_id: idOrNull(form.component_id.value),
    };
  }

  function fill(draft) {
    form.title.value = draft.title || "";
    form.description.value = draft.description || "";
    form.category_id.value = draft.category_id ?? "";
    form.component_id.value = draft.component_id ?? "";
  }

  function readLocal() {
    try {
      return JSON.parse(localStorage.getItem(storageKey));
    } catch (e) {
      return null;
    }
  }

  function writeLocal(dirty) {
    if (state.registered) return;
    const copy = {
      ...values(),
      draft_id: state.draftId,
      server_updated_at: state.serverUpdatedAt,
      dirty,
    };
    try {
      localStorage.setItem(storageKey, JSON.stringify(copy));
    } catch (e) {
      // хранилище недоступно (приватный режим) — остаётся серверный черновик
    }
  }

  function clearLocal() {
    try {
      localStorage.removeItem(storageKey);
    } catch (e) {
      // хранилище недоступно — удалять нечего
    }
  }

  // Обе метки выдал сервер, но в разной записи (``Z`` и ``+00:00``):
  // сравниваются моменты времени, а не строки.
  function sameInstant(a, b) {
    return a != null && b != null && Date.parse(a) === Date.parse(b);
  }

  function restore() {
    const server = page.draft;
    const local = readLocal();
    if (server) {
      state.draftId = server.id;
      state.serverUpdatedAt = server.updated_at;
    }
    // Локальная копия берётся, только если в ней есть несохранённые правки
    // и сервер с тех пор не получил другой версии этого черновика.
    const localId = idOrNull(local && local.draft_id);
    const useLocal = local && local.dirty && (
      !server || localId !== server.id || sameInstant(local.server_updated_at, server.updated_at));
    if (useLocal) {
      fill(local);
      if (localId !== null && (!server || localId !== server.id)) state.draftId = null;
      scheduleDraft();  // донести несохранённые правки до сервера
    } else if (server) {
      fill(server);
    }
  }

  async function sendDraft() {
    const body = values();
    let draft;
    if (state.draftId === null) {
      draft = await api("POST", "/api/drafts", body);
    } else {
      try {
        draft = await api("PUT", `/api/drafts/${state.draftId}`, body);
      } catch (e) {
        if (e.status !== 404 || state.registered) throw e;
        draft = await api("POST", "/api/drafts", body);  // удалён в другой вкладке
      }
    }
    state.draftId = draft.id;
    state.serverUpdatedAt = draft.updated_at;
    if (state.registered) return;
    const unchanged = JSON.stringify(values()) === JSON.stringify(body);
    writeLocal(!unchanged);
    $("draft-state").textContent = unchanged ? "Черновик сохранён." : "Сохранение черновика…";
  }

  function saveDraft() {
    if (state.registered) return;
    if (state.saving) {  // одно сохранение за раз; следующее — после него
      state.saveAgain = true;
      return;
    }
    state.saving = sendDraft()
      .catch(() => {
        if (!state.registered) {
          $("draft-state").textContent = "Черновик не сохранён на сервере, копия — в браузере.";
        }
      })
      .finally(() => {
        state.saving = null;
        if (state.saveAgain && !state.registered) {
          state.saveAgain = false;
          saveDraft();
        }
      });
  }

  function scheduleDraft() {
    if (state.registered) return;
    writeLocal(true);
    clearTimeout(state.draftTimer);
    state.draftTimer = setTimeout(saveDraft, DRAFT_DELAY);
  }

  function updateCounter() {
    const n = form.description.value.length;
    $("counter").textContent = n > 3500 ? `Описание: ${n} из 4000 символов.` : "";
  }

  // --- подбор ---------------------------------------------------------

  function queryKey(v) {
    return JSON.stringify([v.title.trim(), v.description.trim(), v.category_id, v.component_id]);
  }

  function setStale(stale) {
    $("candidates").classList.toggle("stale", stale);
    for (const b of document.querySelectorAll("#candidates button[data-link]")) {
      b.disabled = stale || b.closest("tr").classList.contains("rejected");
    }
  }

  async function search(force) {
    if (state.registered) return;
    const v = values();
    const key = queryKey(v);
    if ((v.title + v.description).trim().length < MIN_QUERY) return;
    if (!force && key === state.lastQuery) {
      setStale(key !== state.searchKey);
      return;
    }
    state.lastQuery = key;
    if (state.controller) state.controller.abort();
    state.controller = new AbortController();
    const seq = ++state.searchSeq;
    $("check").disabled = true;
    try {
      const data = await api("POST", "/api/search", v, state.controller.signal);
      if (seq !== state.searchSeq) return;  // пришёл ответ на устаревший запрос
      state.sessionId = data.session_id;
      state.searchKey = key;
      render(data);
    } catch (e) {
      if (e.name === "AbortError" || seq !== state.searchSeq) return;
      state.lastQuery = null;  // повторить при следующем вводе
      showResults();
      notice(e.status === 400 ? e.message
        : "Подбор похожих сейчас недоступен. Обращение можно зарегистрировать.");
    } finally {
      if (seq === state.searchSeq) $("check").disabled = false;
    }
  }

  function scheduleSearch() {
    if (state.registered) return;
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => search(false), SEARCH_DELAY);
  }

  function resetSearch() {
    state.sessionId = null;
    state.searchKey = null;
    state.lastQuery = null;
    $("candidates").querySelector("tbody").replaceChildren();
    $("results").hidden = true;
  }

  function showResults() {
    $("results").hidden = false;
  }

  function notice(text) {
    $("notice").textContent = text || "";
    $("notice").hidden = !text;
  }

  function score(value) {
    return value.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function cell(row, text, className) {
    const td = document.createElement("td");
    td.textContent = text;
    if (className) td.className = className;
    row.append(td);
    return td;
  }

  function button(text, handler, className) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    if (className) b.className = className;
    b.addEventListener("click", handler);
    return b;
  }

  function render(data) {
    showResults();
    notice(data.unavailable ? data.notice : null);
    const tbody = $("candidates").querySelector("tbody");
    tbody.replaceChildren();
    for (const rec of data.recommendations) tbody.append(row(rec));
    const count = data.recommendations.length;
    $("found").textContent = data.unavailable ? "" : `(найдено ${count})`;
    $("candidates").hidden = count === 0;
    $("empty").hidden = count !== 0 || data.unavailable;
    setStale(false);
  }

  function linkButton(text, rec, kind) {
    const b = button(text, () => register({ recommendation_id: rec.id, kind }),
      rec.kind === kind ? "" : "secondary");
    b.dataset.link = kind;
    return b;
  }

  function row(rec) {
    const tr = document.createElement("tr");
    cell(tr, String(rec.position));
    const title = cell(tr, "");
    const open = button(rec.title, () => openCard(rec.ticket_id), "link");
    open.title = "Открыть карточку обращения";
    title.append(open);
    const why = document.createElement("div");
    why.className = "muted small";
    why.textContent = rec.explanation;
    title.append(why);
    cell(tr, score(rec.score), "num");
    cell(tr, page.statuses[rec.status] || rec.status);
    cell(tr, rec.category);
    cell(tr, page.kinds[rec.kind] || rec.kind, rec.kind === "duplicate" ? "kind-dup" : "");
    const actions = document.createElement("div");
    actions.className = "actions nowrap";
    cell(tr, "").append(actions);
    const reject = button("Отклонить", () => rejectRec(rec.id, tr), "secondary");
    reject.dataset.reject = "1";
    actions.append(linkButton("Дубликат", rec, "duplicate"), linkButton("Связать", rec, "related"),
      reject);
    if (rec.verdict === "rejected") markRejected(tr);
    return tr;
  }

  function markRejected(tr) {
    tr.classList.add("rejected");
    for (const b of tr.querySelectorAll("button[data-link], button[data-reject]")) b.disabled = true;
  }

  async function rejectRec(id, tr) {
    if (state.busy) return;
    try {
      await api("POST", `/api/recommendations/${id}/reject`);
      markRejected(tr);
    } catch (e) {
      formError(e.message);
    }
  }

  // --- карточка найденного обращения (Ф5, Ф6) ----------------------

  async function openCard(ticketId) {
    let card;
    try {
      card = await api("GET", `/api/tickets/${ticketId}/card`);
    } catch (e) {
      formError(e.message);
      return;
    }
    $("card-title").textContent = `№ ${card.ticket_id}. ${card.title}`;
    $("card-meta").textContent = [page.statuses[card.status] || card.status, card.category,
      card.component].filter(Boolean).join(" · ");
    $("card-description").textContent = card.description || "";
    $("card-description").hidden = !card.description;
    const solution = $("card-solution");
    solution.hidden = card.solution_id === null;
    solution.dataset.id = card.solution_id ?? "";
    $("card-no-solution").hidden = card.solution_id !== null;
    $("card-solution-text").textContent = card.solution_text || "";
    showRating(card.my_rating);
    $("card").showModal();
  }

  function showRating(helped) {
    $("card-rating").textContent = helped === null ? ""
      : helped ? "Ваша оценка: помогло." : "Ваша оценка: не помогло.";
  }

  async function rate(helped) {
    const id = Number($("card-solution").dataset.id);
    try {
      await api("PUT", `/api/solutions/${id}/rating`, { helped });
      showRating(helped);
    } catch (e) {
      $("card-rating").textContent = e.message;
    }
  }

  // --- регистрация ---------------------------------------------------

  function formError(text) {
    $("form-error").textContent = text || "";
    $("form-error").hidden = !text;
  }

  function validate(v) {
    if (!v.title.trim()) return "Укажите заголовок.";
    if (!v.description.trim()) return "Укажите описание.";
    if (v.category_id === null) return "Выберите категорию.";
    if (v.component_id === null) return "Выберите компонент.";
    return null;
  }

  function setBusy(busy) {
    state.busy = busy;
    $("register").textContent = busy ? "Регистрация…" : "Зарегистрировать как независимое";
    for (const b of document.querySelectorAll("#ticket-form button, #candidates button")) {
      if (!b.classList.contains("link")) b.disabled = busy;
    }
    if (!busy) {
      for (const tr of document.querySelectorAll("#candidates tr.rejected")) markRejected(tr);
      setStale(queryKey(values()) !== state.searchKey);
    }
  }

  async function register(link) {
    if (state.busy) return;
    const v = values();
    const problem = validate(v);
    if (problem) {
      formError(problem);
      return;
    }
    if (link && queryKey(v) !== state.searchKey) {
      formError("Текст изменён — дождитесь обновления списка похожих.");
      return;
    }
    formError(null);
    setBusy(true);
    clearTimeout(state.draftTimer);
    clearTimeout(state.searchTimer);
    if (state.controller) state.controller.abort();
    // Новые сохранения черновика запрещаются; идущее дожидается, чтобы
    // передать номер черновика и не оставить его после регистрации.
    state.registered = true;
    if (state.saving) await state.saving;
    let created;
    try {
      const body = { ...v, session_id: state.sessionId, draft_id: state.draftId, link };
      created = await api("POST", "/api/registrations", body);
    } catch (e) {
      state.registered = false;
      setBusy(false);
      formError(e.message);
      if (e.status === 409 || e.status === 404) {  // сеанс или рекомендация устарели
        resetSearch();
        search(true);
      }
      scheduleDraft();
      return;
    }
    clearLocal();
    window.location.href = `/tickets/${created.ticket_id}`;
  }

  // --- обработчики ---------------------------------------------------

  restore();
  updateCounter();
  form.addEventListener("input", () => {
    if (state.registered) return;
    formError(null);
    updateCounter();
    setStale(queryKey(values()) !== state.searchKey);
    scheduleDraft();
    scheduleSearch();
  });
  form.addEventListener("submit", (event) => {  // Enter в поле заголовка
    event.preventDefault();
    clearTimeout(state.searchTimer);
    search(true);
  });
  $("check").addEventListener("click", () => {
    clearTimeout(state.searchTimer);
    search(true);
  });
  $("register").addEventListener("click", () => register(null));
  for (const b of document.querySelectorAll("#card-solution button[data-helped]")) {
    b.addEventListener("click", () => rate(b.dataset.helped === "true"));
  }
  if ((form.title.value + form.description.value).trim()) search(false);
})();
