/**
 * 3Cat Downloader — web UI.
 *
 * Vanilla ES2020 module, no build step, no external resources. It only talks to the
 * local REST API + SSE described in docs/CONTRACT.md, using relative URLs.
 *
 * SECURITY: titles, descriptions and errors come from a remote API. The DOM is always
 * built with createElement/textContent (see `h()`); innerHTML is never used with data,
 * and images are only loaded from http(s) URLs (see `safeImageUrl()`).
 *
 * User-facing strings are in Catalan; code and comments are in English.
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Quality values accepted by the API (DownloadJob.quality / Settings.quality). */
const QUALITY_OPTIONS = [
  ['best', 'Millor disponible'],
  ['1080', '1080p'],
  ['720', '720p'],
  ['576', '576p'],
  ['480', '480p'],
  ['mp4', 'MP4 directe'],
];
const QUALITY_LABELS = Object.fromEntries(QUALITY_OPTIONS);

/** JobStatus values (tresdl/models.py) → Catalan labels. */
const STATUS_LABELS = {
  queued: 'En cua',
  resolving: 'Preparant',
  downloading: 'Descarregant',
  postprocessing: 'Processant',
  completed: 'Completada',
  failed: 'Error',
  cancelled: 'Cancel·lada',
  skipped: 'Ja existia',
};
const ACTIVE_STATUSES = new Set(['queued', 'resolving', 'downloading', 'postprocessing']);
const FINISHED_STATUSES = new Set(['completed', 'failed', 'cancelled', 'skipped']);

/** Job list filters: key → [label, predicate]. */
const JOB_FILTERS = {
  all: ['Totes', () => true],
  active: ['En curs', (j) => ACTIVE_STATUSES.has(j.status)],
  done: ['Completades', (j) => j.status === 'completed' || j.status === 'skipped'],
  failed: ['Errors i cancel·lades', (j) => j.status === 'failed' || j.status === 'cancelled'],
};

const DEFAULT_TEMPLATE = '{series}/Season {season:02d}/{series} - S{season:02d}E{episode:02d} - {title}';
/** Same limits as the server (tresdl/config.py MAX_PARALLEL_LIMIT, server RESOLVE_MAX_LENGTH). */
const MAX_PARALLEL_LIMIT = 8;
const RESOLVE_MAX_LENGTH = 2000;
/** Debounce for the server-rendered filename preview (ms). */
const PREVIEW_DELAY = 250;

/** Rows rendered at once in long lists (Telenotícies has ~19 000 videos). */
const EPISODE_PAGE = 300;
const JOB_PAGE = 200;
/** Ask for confirmation when enqueuing more episodes than this. */
const CONFIRM_THRESHOLD = 30;

const EXAMPLES = ['Merlí', 'Polseres vermelles', 'Plats bruts', 'Com si fos ahir'];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  /** @type {Record<string, any> | null} Settings as returned by GET /api/settings */
  settings: null,
  /** Quality chosen in the browse views (session only; defaults to settings.quality). */
  quality: 'best',
  /** @type {Map<string, any>} DownloadJob by id */
  jobs: new Map(),
  jobFilter: 'all',
  jobLimit: JOB_PAGE,
  view: 'search',
  /** Current content of the search view: {kind: 'welcome'|'loading'|'results'|'video'|'program', ...} */
  home: { kind: 'welcome' },
  /** Previous `home` states for the "Tornar" button. */
  backStack: [],
  /** Incremented on every navigation so stale responses are ignored. */
  navToken: 0,
};

// ---------------------------------------------------------------------------
// Small utilities
// ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);

/** Properties that must be assigned as DOM properties rather than attributes. */
const DOM_PROPS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'indeterminate', 'readOnly']);

/**
 * Build a DOM element safely. Strings become text nodes, never HTML.
 * @param {string} tag
 * @param {Record<string, any>} [props] `class`, `text`, `dataset`, `onxxx` handlers, DOM props or attributes
 * @param {...any} children Nodes, strings, numbers, arrays, null/false (skipped)
 * @returns {HTMLElement}
 */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = String(value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (DOM_PROPS.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  appendChildren(el, children);
  return el;
}

/** Replace all children of `el`, skipping null/false entries (unlike Element.replaceChildren). */
function setChildren(el, ...children) {
  el.replaceChildren();
  appendChildren(el, children);
}

function appendChildren(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
}

/** Only allow absolute http(s) URLs for images coming from the API. */
function safeImageUrl(url) {
  if (typeof url !== 'string') return null;
  const trimmed = url.trim();
  return /^https?:\/\//i.test(trimmed) ? trimmed : null;
}

/** <img> for an API image, or a neutral placeholder block when there is none / it fails. */
function imageOrPlaceholder(url, alt, className) {
  const src = safeImageUrl(url);
  const placeholder = h('div', { class: `${className} img-placeholder`, 'aria-hidden': 'true' }, h('span', { text: '3Cat' }));
  if (!src) return placeholder;
  const img = h('img', { class: className, alt: alt || '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer' });
  img.addEventListener('error', () => img.replaceWith(placeholder), { once: true });
  img.src = src;
  return img;
}

const pad2 = (n) => String(n).padStart(2, '0');
const numberFmt = new Intl.NumberFormat('ca', { maximumFractionDigits: 1 });

/** Catalan plural helper: plural(3, 'capítol', 'capítols') → "3 capítols". */
function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

/** "S01E05", "E05", "Cap. 12" or "—". */
function episodeCode(ep) {
  const s = ep.season_number;
  const e = ep.episode_number;
  if (s === 0) return 'Especial';
  if (s !== null && s !== undefined && e !== null && e !== undefined) return `S${pad2(s)}E${pad2(e)}`;
  if (e !== null && e !== undefined) return `E${pad2(e)}`;
  if (ep.absolute_number !== null && ep.absolute_number !== undefined) return `Cap. ${ep.absolute_number}`;
  return '—';
}

function episodeTitle(ep) {
  return ep.title || ep.raw_title || 'Sense títol';
}

/** ISO "YYYY-MM-DD" → "DD/MM/YYYY". */
function formatDate(iso) {
  if (typeof iso !== 'string') return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** Seconds → "1 h 05 min", "45 min", "1 min 46 s", "40 s". */
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '';
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 600) return `${Math.floor(s / 60)} min ${pad2(s % 60)} s`;
  const totalMin = Math.round(s / 60);
  if (totalMin < 60) return `${totalMin} min`;
  return `${Math.floor(totalMin / 60)} h ${pad2(totalMin % 60)} min`;
}

/** Bytes → "450 MB". */
function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return '';
  const units = ['B', 'kB', 'MB', 'GB', 'TB'];
  let v = bytes;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i += 1; }
  return `${numberFmt.format(i === 0 ? Math.round(v) : v)} ${units[i]}`;
}

/** Remaining seconds → "Queden 3 min 20 s". */
function formatEta(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '';
  const s = Math.round(seconds);
  if (s < 60) return `Queden ${s} s`;
  if (s < 3600) return `Queden ${Math.floor(s / 60)} min ${pad2(s % 60)} s`;
  return `Queden ${Math.floor(s / 3600)} h ${pad2(Math.floor((s % 3600) / 60))} min`;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

class ApiError extends Error {
  /** @param {string} message @param {number} [status] */
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Turn a FastAPI error body into a readable message. */
function detailMessage(data) {
  const detail = data && typeof data === 'object' ? data.detail : null;
  if (typeof detail === 'string') return detail;
  // Raw validation lists are English: show a generic Catalan message instead.
  if (Array.isArray(detail)) return 'Les dades enviades no són vàlides.';
  if (detail && typeof detail === 'object') return detail.message ? String(detail.message) : JSON.stringify(detail);
  return null;
}

/**
 * Call the local REST API (relative URL "api/<path>").
 * @param {string} path e.g. "resolve" or "downloads/abc/cancel"
 * @param {{method?: string, body?: any}} [options]
 */
async function api(path, { method = 'GET', body } = {}) {
  const init = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(`api/${path}`, init);
  } catch {
    throw new ApiError("No s'ha pogut connectar amb el servidor de l'aplicació. Prova de tancar-la i tornar-la a obrir.");
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!res.ok) {
    throw new ApiError(detailMessage(data) || `El servidor ha retornat un error (${res.status}).`, res.status);
  }
  return data;
}

function errorMessage(err) {
  if (err instanceof ApiError) return err.message;
  return 'Hi ha hagut un error inesperat.';
}

// ---------------------------------------------------------------------------
// Toasts & confirm dialog
// ---------------------------------------------------------------------------

/**
 * Show a toast notification.
 * @param {string} message
 * @param {{kind?: 'info'|'success'|'error'|'warning', title?: string, timeout?: number, action?: {label: string, run: () => void}}} [opts]
 */
function toast(message, { kind = 'info', title, timeout, action } = {}) {
  const container = $('#toasts');
  while (container.children.length >= 5) container.firstElementChild.remove();
  const close = () => {
    el.classList.add('toast-leaving');
    setTimeout(() => el.remove(), 200);
  };
  const el = h('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('div', { class: 'toast-body' },
      title ? h('strong', { class: 'toast-title', text: title }) : null,
      h('span', { class: 'toast-msg', text: message })),
    action ? h('button', { type: 'button', class: 'btn btn-small', text: action.label, onclick: () => { action.run(); close(); } }) : null,
    h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Tanca la notificació', text: '×', onclick: close }));
  container.append(el);
  const ms = timeout ?? (kind === 'error' ? 9000 : 5000);
  if (ms > 0) setTimeout(close, ms);
}

function showError(err, title) {
  toast(errorMessage(err), { kind: 'error', title });
}

/**
 * Modal confirmation using <dialog>; falls back to window.confirm.
 * @returns {Promise<boolean>}
 */
function confirmDialog({ title, message, okText = "D'acord", cancelText = 'Cancel·la' }) {
  const dialog = /** @type {HTMLDialogElement} */ ($('#confirm-dialog'));
  if (!dialog || typeof dialog.showModal !== 'function') {
    return Promise.resolve(window.confirm(`${title}\n\n${message}`));
  }
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  $('#confirm-ok').textContent = okText;
  $('#confirm-cancel').textContent = cancelText;
  dialog.returnValue = 'cancel';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
    dialog.showModal();
    $('#confirm-ok').focus();
  });
}

// ---------------------------------------------------------------------------
// Views (top navigation)
// ---------------------------------------------------------------------------

function showView(view) {
  state.view = view;
  for (const btn of document.querySelectorAll('.nav-btn')) {
    if (btn.dataset.view === view) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  }
  for (const name of ['search', 'downloads', 'settings']) {
    $(`#view-${name}`).hidden = name !== view;
  }
  if (view === 'downloads') renderJobs();
  if (view === 'settings') loadSettingsForm();
  window.scrollTo({ top: 0 });
}

// ---------------------------------------------------------------------------
// Shared widgets
// ---------------------------------------------------------------------------

let qualityIdCounter = 0;

/** Quality <select> bound to state.quality. */
function qualityPicker() {
  qualityIdCounter += 1;
  const id = `quality-${qualityIdCounter}`;
  const select = h('select', { id, class: 'quality-select', onchange: () => { state.quality = select.value; } },
    QUALITY_OPTIONS.map(([value, label]) => h('option', { value, text: label, selected: value === state.quality })));
  select.value = state.quality;
  return h('div', { class: 'quality-picker' }, h('label', { for: id, text: 'Qualitat' }), select);
}

function spinner(message) {
  return h('div', { class: 'loading', role: 'status' },
    h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: message }));
}

function emptyState(title, text, ...extra) {
  return h('div', { class: 'empty-state' },
    h('p', { class: 'empty-title', text: title }),
    text ? h('p', { class: 'muted', text }) : null,
    extra);
}

function backButton() {
  if (!state.backStack.length) return null;
  return h('button', { type: 'button', class: 'btn btn-ghost back-btn', text: '← Tornar', onclick: goBack });
}

// ---------------------------------------------------------------------------
// Search view: resolve / results / video / programme
// ---------------------------------------------------------------------------

/** Replace the content of the search view. */
function setHome(home) {
  state.home = home;
  renderHome();
}

function goBack() {
  const prev = state.backStack.pop();
  state.navToken += 1;
  state.home = prev || { kind: 'welcome' };
  renderHome();
}

function renderHome() {
  const root = $('#home-content');
  const home = state.home;
  let content;
  switch (home.kind) {
    case 'loading': content = spinner(home.message || 'Carregant…'); break;
    case 'results': content = renderResults(home); break;
    case 'video': content = renderVideo(home); break;
    case 'program': content = renderProgram(home); break;
    default: content = renderWelcome();
  }
  root.replaceChildren(content);
  root.setAttribute('aria-busy', home.kind === 'loading' ? 'true' : 'false');
}

function renderWelcome() {
  const steps = [
    ['1', 'Busca', "Enganxa l'enllaç d'una sèrie o d'un capítol de 3cat, o escriu-ne el nom."],
    ['2', 'Tria', 'Escull tota la sèrie, una temporada o només els capítols que vulguis.'],
    ['3', 'Descarrega', 'Segueix el progrés a «Descàrregues». Els vídeos es desen a la teva carpeta.'],
  ];
  return h('div', { class: 'welcome' },
    h('ol', { class: 'steps' }, steps.map(([n, title, text]) =>
      h('li', { class: 'step card' },
        h('span', { class: 'step-num', 'aria-hidden': 'true', text: n }),
        h('div', {}, h('strong', { text: title }), h('p', { class: 'muted', text }))))),
    h('div', { class: 'examples' },
      h('span', { class: 'muted', text: 'Prova amb:' }),
      EXAMPLES.map((ex) => h('button', {
        type: 'button', class: 'chip', text: ex,
        onclick: () => { $('#resolve-input').value = ex; submitResolve(ex); },
      }))));
}

async function submitResolve(rawText) {
  const text = (rawText ?? '').trim();
  const input = $('#resolve-input');
  if (!text) {
    toast("Escriu el nom d'una sèrie o enganxa un enllaç de 3cat.", { kind: 'warning' });
    input.focus();
    return;
  }
  if (text.length > RESOLVE_MAX_LENGTH) {
    toast(`El text és massa llarg (màxim ${RESOLVE_MAX_LENGTH} caràcters).`, { kind: 'warning' });
    input.focus();
    return;
  }
  const token = ++state.navToken;
  const previous = state.home;
  const button = $('#resolve-btn');
  button.disabled = true;
  setHome({ kind: 'loading', message: 'Cercant…' });
  try {
    const result = await api('resolve', { method: 'POST', body: { text } });
    if (token !== state.navToken) return;
    state.backStack = [];
    if (result && result.type === 'search') {
      setHome({ kind: 'results', query: text, results: Array.isArray(result.results) ? result.results : [] });
    } else if (result && result.type === 'program' && result.program) {
      await openProgram(result.program.id, result.program, { pushHistory: false, token });
    } else if (result && result.type === 'video' && result.episode) {
      setHome({ kind: 'video', episode: result.episode, program: result.program || null });
    } else {
      throw new ApiError('El servidor ha donat una resposta inesperada.');
    }
  } catch (err) {
    if (token !== state.navToken) return;
    showError(err, "No s'ha pogut fer la cerca");
    state.home = previous.kind === 'loading' ? { kind: 'welcome' } : previous;
    renderHome();
  } finally {
    button.disabled = false;
  }
}

function renderResults(home) {
  const results = home.results;
  const header = h('div', { class: 'section-header' },
    h('h2', { text: results.length ? `Resultats per «${home.query}»` : 'Cap resultat' }),
    results.length ? h('p', { class: 'muted', text: `${plural(results.length, 'programa', 'programes')}. Tria'n un per veure'n els capítols.` }) : null);
  if (!results.length) {
    return h('div', {}, header,
      emptyState(`No s'ha trobat cap programa per «${home.query}».`,
        "Prova amb un altre nom o enganxa directament l'enllaç de la sèrie des del web de 3cat."));
  }
  const grid = h('ul', { class: 'program-grid' }, results.map((p) =>
    h('li', {},
      h('button', {
        type: 'button', class: 'program-card',
        'aria-label': `${p.title}. Veure capítols`,
        onclick: () => openProgram(p.id, p, { pushHistory: true }),
      },
      imageOrPlaceholder(p.image, '', 'program-card-img'),
      h('span', { class: 'program-card-body' },
        h('span', { class: 'program-card-title', text: p.title }),
        p.description ? h('span', { class: 'program-card-desc', text: p.description }) : null,
        p.info ? h('span', { class: 'program-card-info', text: p.info }) : null)))));
  return h('div', {}, header, grid);
}

function renderVideo(home) {
  const ep = home.episode;
  const program = home.program;
  const programId = (program && program.id) || ep.program_id;
  const seriesTitle = (program && program.title) || ep.program_title;
  const meta = [episodeCode(ep), formatDate(ep.air_date), formatDuration(ep.duration_seconds)].filter((x) => x && x !== '—');
  const downloadBtn = h('button', {
    type: 'button', class: 'btn btn-primary btn-lg', text: 'Descarregar aquest capítol',
    onclick: async () => {
      downloadBtn.disabled = true;
      try { await enqueue({ episode_ids: [ep.id] }); } finally { downloadBtn.disabled = false; }
    },
  });
  return h('article', { class: 'video-view card' },
    backButton(),
    h('div', { class: 'video-layout' },
      imageOrPlaceholder(ep.image || (program && program.image), '', 'video-img'),
      h('div', { class: 'video-info' },
        seriesTitle ? h('p', { class: 'eyebrow', text: seriesTitle }) : null,
        h('h2', { text: episodeTitle(ep) }),
        meta.length ? h('p', { class: 'muted', text: meta.join(' · ') }) : null,
        ep.description ? h('p', { class: 'video-desc', text: ep.description }) : null,
        h('div', { class: 'action-row' }, qualityPicker(), downloadBtn),
        programId ? h('p', {},
          h('button', {
            type: 'button', class: 'link-btn',
            text: seriesTitle ? `Veure tota la sèrie «${seriesTitle}» →` : 'Veure tota la sèrie →',
            onclick: () => openProgram(programId, program, { pushHistory: true }),
          })) : null)));
}

/**
 * Load a programme and show it.
 * @param {number} id
 * @param {any} [preview] Program already known (for the loading message)
 */
async function openProgram(id, preview, { pushHistory = true, token } = {}) {
  const myToken = token ?? ++state.navToken;
  const previous = state.home;
  const name = preview && preview.title ? `«${preview.title}»` : 'el programa';
  setHome({ kind: 'loading', message: `Carregant els capítols de ${name}…` });
  try {
    const data = await api(`programs/${encodeURIComponent(id)}`);
    if (myToken !== state.navToken) return;
    if (pushHistory && previous.kind !== 'loading' && previous.kind !== 'welcome') state.backStack.push(previous);
    const seasons = Array.isArray(data.seasons) ? data.seasons : [];
    setHome({
      kind: 'program',
      program: data.program || preview,
      seasons,
      episodes: Array.isArray(data.episodes) ? data.episodes : [],
      // Single-season programmes start on that season so "Descarregar temporada" is visible.
      activeSeason: seasons.length === 1 ? seasons[0].id : null,
      selected: new Set(),
      filter: '',
      limit: EPISODE_PAGE,
    });
  } catch (err) {
    if (myToken !== state.navToken) return;
    showError(err, "No s'ha pogut carregar el programa");
    state.home = previous.kind === 'loading' ? { kind: 'welcome' } : previous;
    renderHome();
  }
}

function seasonLabel(season) {
  if (season.number) return `Temporada ${season.number}`;   // 0 = specials, null = no season
  return season.title || 'Sense temporada';
}

/** Programme view: header, season chips, actions, episode list. Re-renders its dynamic parts in place. */
function renderProgram(home) {
  const program = home.program || {};
  const episodes = home.episodes;
  const seasons = home.seasons;

  const header = h('header', { class: 'program-header card' },
    imageOrPlaceholder(program.image, '', 'program-header-img'),
    h('div', { class: 'program-header-body' },
      h('h2', { text: program.title || 'Programa' }),
      program.description ? h('p', { class: 'program-desc', text: program.description }) : null,
      h('p', { class: 'muted' },
        [program.info, plural(episodes.length, 'capítol disponible', 'capítols disponibles'),
          seasons.length > 1 ? plural(seasons.length, 'temporada', 'temporades') : null]
          .filter(Boolean).join(' · '))));

  if (!episodes.length) {
    return h('div', { class: 'program-view' }, backButton(), header,
      emptyState('Aquest programa no té capítols disponibles ara mateix.',
        'Pot ser que encara no els hagin publicat o que ja no estiguin disponibles a 3cat.'));
  }

  // --- dynamic parts -----------------------------------------------------
  const chips = h('div', { class: 'chips', role: 'group', 'aria-label': 'Temporades' });
  const actions = h('div', { class: 'program-actions' });
  const listHeader = h('div', { class: 'ep-row ep-head' });
  const list = h('ul', { class: 'ep-list', 'aria-label': 'Capítols' });
  const more = h('div', { class: 'more-row' });
  const filterInput = h('input', {
    type: 'search', class: 'ep-filter', placeholder: 'Filtra per títol…', 'aria-label': 'Filtra els capítols per títol',
    value: home.filter,
    oninput: () => { home.filter = filterInput.value; home.limit = EPISODE_PAGE; update(); },
  });
  const qualityBox = qualityPicker();

  const visibleEpisodes = () => {
    const needle = normalise(home.filter);
    return episodes.filter((ep) =>
      (home.activeSeason === null || ep.season_id === home.activeSeason)
      && (!needle || normalise(`${episodeCode(ep)} ${episodeTitle(ep)}`).includes(needle)));
  };

  /**
   * Enqueue with an optional confirmation.
   * @param {HTMLButtonElement} btn button to disable meanwhile
   * @param {object} body POST /api/downloads body (without quality)
   * @param {number} count number of episodes involved
   * @param {string} what human description for the dialog title
   * @param {{alwaysConfirm?: boolean, clearSelection?: boolean}} [opts]
   */
  const runDownload = async (btn, body, count, what, { alwaysConfirm = false, clearSelection = false } = {}) => {
    if (alwaysConfirm || count > CONFIRM_THRESHOLD) {
      const ok = await confirmDialog({
        title: `Descarregar ${what}`,
        message: `Es descarregaran ${plural(count, 'capítol', 'capítols')} de «${program.title || ''}» amb qualitat «${QUALITY_LABELS[state.quality] || state.quality}». Pot trigar una estona i ocupar força espai al disc. Vols continuar?`,
        okText: 'Sí, descarrega',
      });
      if (!ok) return;
    }
    btn.disabled = true;
    try {
      const jobs = await enqueue(body);
      if (jobs && clearSelection) {
        home.selected.clear();
        update();
      }
    } finally { btn.disabled = false; }
  };

  function renderChips() {
    const chip = (key, label, count) => h('button', {
      type: 'button', class: 'chip', 'aria-pressed': String(home.activeSeason === key),
      dataset: { focusKey: `season:${key === null ? '*' : key}` },
      onclick: () => { home.activeSeason = key; home.limit = EPISODE_PAGE; update(); },
    }, label, h('span', { class: 'chip-count', text: String(count) }));
    setChildren(chips,
      seasons.length > 1 ? chip(null, 'Totes', episodes.length) : null,
      ...seasons.map((s) => chip(s.id, s.title || seasonLabel(s),
        s.episode_count || episodes.filter((ep) => ep.season_id === s.id).length)));
    chips.hidden = seasons.length === 0;
  }

  function renderActions() {
    const selectedCount = home.selected.size;
    const season = home.activeSeason === null ? null : seasons.find((s) => s.id === home.activeSeason);
    const seasonEpisodes = season ? episodes.filter((ep) => ep.season_id === season.id) : [];

    const allBtn = h('button', { type: 'button', class: 'btn btn-primary', dataset: { focusKey: 'dl-all' } },
      'Descarregar tota la sèrie', h('span', { class: 'btn-sub', text: ` (${episodes.length})` }));
    allBtn.addEventListener('click', () => runDownload(allBtn, { program_id: program.id }, episodes.length, 'tota la sèrie', { alwaysConfirm: episodes.length > 1 }));

    let seasonBtn = null;
    if (season && seasons.length > 0) {
      const label = season.number
        ? `Descarregar temporada ${season.number}` : `Descarregar «${season.title || 'temporada'}»`;
      seasonBtn = h('button', { type: 'button', class: 'btn', dataset: { focusKey: 'dl-season' } }, label, h('span', { class: 'btn-sub', text: ` (${seasonEpisodes.length})` }));
      // An empty season id cannot be expressed as season_ids, so send the episode ids instead.
      const body = season.id
        ? { program_id: program.id, season_ids: [season.id] }
        : { program_id: program.id, episode_ids: seasonEpisodes.map((ep) => ep.id) };
      seasonBtn.disabled = seasonEpisodes.length === 0;
      seasonBtn.addEventListener('click', () => {
        if (!season.id && seasonEpisodes.length === 0) {
          toast("Aquesta temporada no té cap capítol.", { kind: 'warning' });
          return;
        }
        runDownload(seasonBtn, body, seasonEpisodes.length, label.replace(/^Descarregar /, ''));
      });
    }

    const selBtn = h('button', { type: 'button', class: 'btn', dataset: { focusKey: 'dl-sel' }, disabled: selectedCount === 0, text: `Descarregar seleccionats (${selectedCount})` });
    selBtn.addEventListener('click', () => {
      // Only ids still present in this listing (a stale selection must never
      // become an empty list, which the server rejects).
      const ids = episodes.filter((ep) => home.selected.has(ep.id)).map((ep) => ep.id);
      if (ids.length === 0) {
        home.selected.clear();
        update();
        toast("No hi ha cap capítol seleccionat.", { kind: 'warning' });
        return;
      }
      runDownload(selBtn, { program_id: program.id, episode_ids: ids },
        ids.length, plural(ids.length, 'capítol seleccionat', 'capítols seleccionats'), { clearSelection: true });
    });

    const hint = !season && seasons.length > 1
      ? h('p', { class: 'help', text: 'Consell: tria una temporada a dalt per descarregar-la sencera.' }) : null;

    setChildren(actions,
      h('div', { class: 'action-row' }, qualityBox, allBtn, seasonBtn, selBtn,
        selectedCount ? h('button', {
          type: 'button', class: 'link-btn', text: 'Desmarca-ho tot',
          onclick: () => { home.selected.clear(); update(); },
        }) : null),
      hint);
  }

  function renderList() {
    const visible = visibleEpisodes();
    const shown = visible.slice(0, home.limit);
    const allChecked = visible.length > 0 && visible.every((ep) => home.selected.has(ep.id));
    const someChecked = visible.some((ep) => home.selected.has(ep.id));

    const selectAll = h('input', {
      type: 'checkbox', checked: allChecked, indeterminate: someChecked && !allChecked,
      'aria-label': 'Selecciona tots els capítols de la llista', disabled: visible.length === 0,
      dataset: { focusKey: 'select-all' },
      onchange: () => {
        for (const ep of visible) {
          if (selectAll.checked) home.selected.add(ep.id); else home.selected.delete(ep.id);
        }
        update();
      },
    });
    listHeader.replaceChildren(
      h('div', { class: 'ep-main' },
        h('span', { class: 'ep-check' }, selectAll),
        h('span', { class: 'ep-num', text: 'Capítol' }),
        h('span', { class: 'ep-title', text: 'Títol' }),
        h('span', { class: 'ep-date', text: 'Data' }),
        h('span', { class: 'ep-dur', text: 'Durada' })),
      h('span', { class: 'ep-action-spacer', 'aria-hidden': 'true' }));

    list.replaceChildren(...shown.map((ep) => episodeRow(ep, home, () => { renderActions(); refreshSelectAll(); })));
    if (!visible.length) {
      list.append(h('li', { class: 'ep-empty muted', text: 'Cap capítol coincideix amb el filtre.' }));
    }
    more.replaceChildren();
    if (visible.length > shown.length) {
      more.append(
        h('span', { class: 'muted', text: `Es mostren ${shown.length} de ${visible.length} capítols.` }),
        h('button', { type: 'button', class: 'btn', text: 'Mostrar-ne més', onclick: () => { home.limit += EPISODE_PAGE; renderList(); } }));
    }
    more.hidden = visible.length <= shown.length;

    function refreshSelectAll() {
      const all = visible.length > 0 && visible.every((ep) => home.selected.has(ep.id));
      const some = visible.some((ep) => home.selected.has(ep.id));
      selectAll.checked = all;
      selectAll.indeterminate = some && !all;
    }
  }

  /** Re-render the dynamic parts, keeping keyboard focus on the equivalent control. */
  function update() {
    const active = document.activeElement;
    const focusKey = active instanceof HTMLElement ? active.dataset.focusKey : undefined;
    renderChips();
    renderActions();
    renderList();
    if (focusKey) {
      const selector = `[data-focus-key="${CSS.escape(focusKey)}"]`;
      const target = chips.querySelector(selector) || actions.querySelector(selector) || listHeader.querySelector(selector);
      if (target instanceof HTMLElement && !target.hasAttribute('disabled')) target.focus();
    }
  }

  update();
  return h('div', { class: 'program-view' },
    backButton(),
    header,
    h('div', { class: 'program-toolbar card' }, chips, actions),
    h('div', { class: 'ep-table card' },
      h('div', { class: 'ep-table-top' },
        h('h3', { text: 'Capítols' }),
        filterInput),
      listHeader, list, more));
}

function episodeRow(ep, home, onToggle) {
  const id = `ep-${ep.id}`;
  const checkbox = h('input', {
    type: 'checkbox', id, checked: home.selected.has(ep.id),
    onchange: () => {
      if (checkbox.checked) home.selected.add(ep.id); else home.selected.delete(ep.id);
      row.classList.toggle('is-selected', checkbox.checked);
      onToggle();
    },
  });
  const dl = h('button', {
    type: 'button', class: 'btn btn-icon', title: 'Descarrega aquest capítol',
    'aria-label': `Descarrega ${episodeCode(ep)} ${episodeTitle(ep)}`, text: '↓',
    onclick: async () => {
      dl.disabled = true;
      try { await enqueue({ program_id: ep.program_id, episode_ids: [ep.id] }); } finally { dl.disabled = false; }
    },
  });
  const row = h('li', { class: `ep-row${home.selected.has(ep.id) ? ' is-selected' : ''}` },
    h('label', { class: 'ep-main', for: id },
      h('span', { class: 'ep-check' }, checkbox),
      h('span', { class: 'ep-num', text: episodeCode(ep) }),
      h('span', { class: 'ep-title' },
        h('span', { class: 'ep-title-text', text: episodeTitle(ep) }),
        ep.description ? h('span', { class: 'ep-desc', text: ep.description }) : null),
      h('span', { class: 'ep-date', text: formatDate(ep.air_date) }),
      h('span', { class: 'ep-dur', text: formatDuration(ep.duration_seconds) })),
    dl);
  return row;
}

/** Lower-case, accent-insensitive text for filtering. */
function normalise(text) {
  return String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
}

// ---------------------------------------------------------------------------
// Enqueue downloads
// ---------------------------------------------------------------------------

/**
 * POST /api/downloads with the current quality.
 * @param {{episode_ids?: number[], program_id?: number, season_ids?: string[]}} body
 * @returns {Promise<any[] | null>} created jobs, or null on error
 */
async function enqueue(body) {
  try {
    const result = await api('downloads', { method: 'POST', body: { ...body, quality: state.quality } });
    const jobs = (result && Array.isArray(result.jobs)) ? result.jobs : [];
    for (const job of jobs) upsertJob(job, false);
    scheduleJobRender();
    if (jobs.length) {
      toast(`${jobs.length === 1 ? "S'ha afegit" : "S'han afegit"} ${plural(jobs.length, 'capítol', 'capítols')} a la cua.`, {
        kind: 'success',
        action: { label: 'Veure descàrregues', run: () => showView('downloads') },
      });
    } else {
      toast('No hi ha res de nou per descarregar: aquests capítols ja són a la cua.', { kind: 'info' });
    }
    return jobs;
  } catch (err) {
    showError(err, "No s'ha pogut afegir a la cua");
    return null;
  }
}

// ---------------------------------------------------------------------------
// Downloads panel
// ---------------------------------------------------------------------------

/** @type {Map<string, {el: HTMLElement, refs: Record<string, any>}>} */
const jobRows = new Map();
let jobRenderPending = false;
let pendingFailures = [];
let failureTimer = 0;
let hadActiveJobs = false;

/**
 * Insert or update a job from the API.
 * @param {any} job DownloadJob dict
 * @param {boolean} live true when it came from SSE (enables notifications)
 */
function upsertJob(job, live) {
  if (!job || typeof job.id !== 'string') return;
  const prev = state.jobs.get(job.id);
  state.jobs.set(job.id, job);
  if (live && prev && prev.status !== 'failed' && job.status === 'failed') notifyFailure(job);
}

function replaceJobs(list) {
  state.jobs = new Map();
  for (const job of list) upsertJob(job, false);
  for (const id of [...jobRows.keys()]) {
    if (!state.jobs.has(id)) jobRows.delete(id);
  }
  scheduleJobRender();
}

/** Group failures that arrive together into a single toast. */
function notifyFailure(job) {
  pendingFailures.push(job);
  clearTimeout(failureTimer);
  failureTimer = setTimeout(() => {
    const failures = pendingFailures;
    pendingFailures = [];
    if (failures.length === 1) {
      const j = failures[0];
      toast(j.error || 'Error desconegut.', {
        kind: 'error', title: `Ha fallat: ${jobTitle(j)}`,
        action: state.view !== 'downloads' ? { label: 'Veure', run: () => showView('downloads') } : undefined,
      });
    } else if (failures.length > 1) {
      toast(`${failures.length} descàrregues han fallat. Consulta'n el motiu a «Descàrregues».`, {
        kind: 'error', action: state.view !== 'downloads' ? { label: 'Veure', run: () => showView('downloads') } : undefined,
      });
    }
  }, 800);
}

function jobTitle(job) {
  const ep = job.episode || {};
  const code = episodeCode(ep);
  return [ep.program_title, code !== '—' ? code : null, episodeTitle(ep)].filter(Boolean).join(' · ');
}

function scheduleJobRender() {
  if (jobRenderPending) return;
  jobRenderPending = true;
  requestAnimationFrame(() => {
    jobRenderPending = false;
    renderJobs();
  });
}

function sortedJobs() {
  return [...state.jobs.values()].sort((a, b) =>
    (a.created_at || 0) - (b.created_at || 0) || String(a.id).localeCompare(String(b.id)));
}

/** Badge, title, summary, filters and (if visible) the job list. */
function renderJobs() {
  const jobs = sortedJobs();
  const counts = { all: jobs.length, active: 0, done: 0, failed: 0 };
  let downloading = 0;
  for (const job of jobs) {
    for (const key of ['active', 'done', 'failed']) if (JOB_FILTERS[key][1](job)) counts[key] += 1;
    if (job.status === 'downloading') downloading += 1;
  }

  // Badge + window title
  const badge = $('#downloads-badge');
  badge.textContent = String(counts.active);
  badge.hidden = counts.active === 0;
  badge.setAttribute('aria-label', plural(counts.active, 'descàrrega activa', 'descàrregues actives'));
  document.title = counts.active ? `(${counts.active}) 3Cat Downloader` : '3Cat Downloader';

  if (hadActiveJobs && counts.active === 0 && counts.done > 0) {
    toast('Totes les descàrregues han acabat.', { kind: 'success' });
  }
  hadActiveJobs = counts.active > 0;

  $('#downloads-summary').textContent = jobs.length
    ? [
      downloading ? `${downloading} descarregant` : null,
      counts.active - downloading > 0 ? `${counts.active - downloading} en espera` : null,
      counts.done ? plural(counts.done, 'completada', 'completades') : null,
      counts.failed ? `${counts.failed} amb error o cancel·lades` : null,
    ].filter(Boolean).join(' · ')
    : 'No hi ha cap descàrrega.';
  $('#clear-finished').disabled = counts.done + counts.failed === 0;

  if (state.view !== 'downloads') return;

  // Filters (rebuilt only when something changed, so keyboard focus survives live ticks)
  const filters = $('#job-filters');
  const filtersKey = `${state.jobFilter}|${counts.all}|${counts.active}|${counts.done}|${counts.failed}`;
  if (filters.dataset.key !== filtersKey) {
    filters.dataset.key = filtersKey;
    filters.replaceChildren(...Object.entries(JOB_FILTERS).map(([key, [label]]) => h('button', {
      type: 'button', class: 'chip', 'aria-pressed': String(state.jobFilter === key),
      onclick: () => {
        state.jobFilter = key;
        state.jobLimit = JOB_PAGE;
        renderJobs();
        const pressed = filters.querySelector('[aria-pressed="true"]');
        if (pressed) pressed.focus();
      },
    }, label, h('span', { class: 'chip-count', text: String(counts[key]) }))));
  }
  filters.hidden = jobs.length === 0;

  // List (rows are reused so focus and hover survive live updates)
  const predicate = JOB_FILTERS[state.jobFilter][1];
  const visible = jobs.filter(predicate);
  const shown = visible.slice(0, state.jobLimit);
  const list = $('#job-list');
  const wanted = shown.map((job) => {
    let row = jobRows.get(job.id);
    if (!row) { row = createJobRow(job.id); jobRows.set(job.id, row); }
    updateJobRow(row, job);
    return row.el;
  });
  const current = [...list.children];
  if (current.length !== wanted.length || current.some((el, i) => el !== wanted[i])) {
    list.replaceChildren(...wanted);
  }

  const empty = $('#job-empty');
  empty.hidden = visible.length > 0;
  if (!visible.length) {
    empty.replaceChildren(jobs.length
      ? emptyState('No hi ha cap descàrrega en aquesta categoria.', null)
      : emptyState('Encara no has descarregat res.', 'Busca una sèrie a «Cercar» i tria què vols descarregar.',
        h('button', { type: 'button', class: 'btn btn-primary', text: 'Anar a cercar', onclick: () => { showView('search'); $('#resolve-input').focus(); } })));
  }

  const more = $('#job-more');
  more.hidden = visible.length <= shown.length;
  more.replaceChildren();
  if (!more.hidden) {
    more.append(
      h('span', { class: 'muted', text: `Es mostren ${shown.length} de ${visible.length}.` }),
      h('button', { type: 'button', class: 'btn', text: 'Mostrar-ne més', onclick: () => { state.jobLimit += JOB_PAGE; renderJobs(); } }));
  }
}

function createJobRow(jobId) {
  const refs = {};
  refs.thumb = h('div', { class: 'job-thumb-wrap' });
  refs.series = h('span', { class: 'job-series' });
  refs.title = h('span', { class: 'job-title' });
  refs.status = h('span', { class: 'status-pill' });
  refs.meta = h('span', { class: 'job-meta muted' });
  refs.bar = h('div', { class: 'progress-bar' });
  refs.progress = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, refs.bar);
  refs.error = h('p', { class: 'job-error', role: 'note' });
  refs.path = h('p', { class: 'job-path muted' });

  const action = (label, cls, handler) => {
    const btn = h('button', { type: 'button', class: `btn btn-small ${cls}`, text: label });
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try { await handler(); } finally { btn.disabled = false; }
    });
    return btn;
  };
  refs.cancel = action('Cancel·la', 'btn-danger-ghost', () => jobAction(jobId, 'cancel'));
  refs.retry = action('Reintenta', '', () => jobAction(jobId, 'retry'));
  refs.remove = action('Treu de la llista', 'btn-ghost', () => removeJob(jobId));
  // By job id: the server opens the job's own folder even if the download
  // folder setting changed since.
  refs.open = action('Obre la carpeta', '', () => openFolder({ job_id: jobId }));

  const el = h('li', { class: 'job card' },
    refs.thumb,
    h('div', { class: 'job-body' },
      h('div', { class: 'job-head' },
        h('div', { class: 'job-names' }, refs.series, refs.title),
        refs.status),
      refs.progress,
      refs.meta,
      refs.error,
      refs.path),
    h('div', { class: 'job-actions' }, refs.open, refs.retry, refs.cancel, refs.remove));
  return { el, refs, lastImage: undefined };
}

function updateJobRow(row, job) {
  const { refs } = row;
  const ep = job.episode || {};
  const status = job.status;

  if (row.lastImage !== ep.image) {
    row.lastImage = ep.image;
    refs.thumb.replaceChildren(imageOrPlaceholder(ep.image, '', 'job-thumb'));
  }
  row.el.dataset.status = status;
  refs.series.textContent = ep.program_title || '';
  const code = episodeCode(ep);
  refs.title.textContent = code !== '—' ? `${code} · ${episodeTitle(ep)}` : episodeTitle(ep);
  refs.status.textContent = STATUS_LABELS[status] || status;
  refs.status.dataset.status = status;

  // Progress
  const indeterminate = status === 'resolving' || status === 'postprocessing'
    || (status === 'downloading' && !job.progress);
  const pct = status === 'completed' || status === 'skipped' ? 100 : Math.max(0, Math.min(100, Number(job.progress) || 0));
  refs.progress.classList.toggle('is-indeterminate', indeterminate);
  refs.progress.hidden = status === 'queued' || status === 'cancelled' || (status === 'failed' && !pct);
  refs.bar.style.width = indeterminate ? '' : `${pct}%`;
  if (indeterminate) refs.progress.removeAttribute('aria-valuenow');
  else refs.progress.setAttribute('aria-valuenow', String(Math.round(pct)));
  refs.progress.setAttribute('aria-label', `Progrés de ${episodeTitle(ep)}`);

  // Meta line
  const parts = [];
  if (status === 'downloading') {
    parts.push(`${numberFmt.format(pct)} %`);
    if (job.speed) parts.push(`${formatBytes(job.speed)}/s`);
    if (job.eta !== null && job.eta !== undefined) parts.push(formatEta(job.eta));
  }
  if (job.downloaded_bytes || job.total_bytes) {
    parts.push(job.total_bytes && status !== 'completed'
      ? `${formatBytes(job.downloaded_bytes)} de ${formatBytes(job.total_bytes)}`
      : formatBytes(job.total_bytes || job.downloaded_bytes));
  }
  if (job.quality) parts.push(QUALITY_LABELS[job.quality] || job.quality);
  if (status === 'queued') parts.unshift('Esperant torn');
  if (status === 'resolving') parts.unshift("Obtenint la informació del vídeo…");
  if (status === 'postprocessing') parts.unshift('Unint àudio i vídeo…');
  refs.meta.textContent = parts.join(' · ');

  refs.error.textContent = job.error || '';
  refs.error.hidden = !job.error;
  refs.path.textContent = job.output_path && (status === 'completed' || status === 'skipped') ? job.output_path : '';
  refs.path.hidden = !refs.path.textContent;

  refs.cancel.hidden = !ACTIVE_STATUSES.has(status);
  refs.retry.hidden = !(status === 'failed' || status === 'cancelled');
  refs.remove.hidden = !FINISHED_STATUSES.has(status);
  refs.open.hidden = !(job.output_path && (status === 'completed' || status === 'skipped'));
}

async function jobAction(jobId, action) {
  try {
    const result = await api(`downloads/${encodeURIComponent(jobId)}/${action}`, { method: 'POST' });
    if (result && result.ok === false) {
      toast(action === 'cancel' ? "Aquesta descàrrega ja no es pot cancel·lar." : "Aquesta descàrrega no es pot reintentar ara.", { kind: 'warning' });
      await refreshJobs();
    }
  } catch (err) {
    showError(err, action === 'cancel' ? "No s'ha pogut cancel·lar" : "No s'ha pogut reintentar");
  }
}

async function removeJob(jobId) {
  try {
    const result = await api(`downloads/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
    if (result && result.ok === false) {
      toast('Només es poden treure les descàrregues que ja han acabat.', { kind: 'warning' });
      return;
    }
    state.jobs.delete(jobId);
    jobRows.delete(jobId);
    scheduleJobRender();
  } catch (err) {
    showError(err, "No s'ha pogut treure de la llista");
  }
}

async function clearFinished() {
  const btn = $('#clear-finished');
  btn.disabled = true;
  try {
    const result = await api('downloads/clear', { method: 'POST' });
    const removed = result && Number.isFinite(result.removed) ? result.removed : 0;
    toast(removed ? `S'han netejat ${plural(removed, 'descàrrega', 'descàrregues')}.` : 'No hi havia res per netejar.', { kind: 'info' });
    await refreshJobs();
  } catch (err) {
    showError(err, "No s'ha pogut netejar la llista");
  } finally {
    btn.disabled = false;
    scheduleJobRender();
  }
}

async function refreshJobs() {
  try {
    const result = await api('downloads');
    replaceJobs(result && Array.isArray(result.jobs) ? result.jobs : []);
  } catch (err) {
    // The connection indicator already reports server problems; avoid toast spam here.
    console.warn('Could not refresh downloads', err);
  }
}

/** POST /api/open-folder; ``body`` is null (download folder), {job_id} or {path}. */
async function openFolder(body) {
  try {
    await api('open-folder', { method: 'POST', body: body || {} });
  } catch (err) {
    showError(err, "No s'ha pogut obrir la carpeta");
  }
}

// ---------------------------------------------------------------------------
// Live updates (SSE) with automatic reconnect
// ---------------------------------------------------------------------------

let eventSource = null;
let reconnectDelay = 1000;
let reconnectTimer = 0;
let pollTimer = 0;

function setConnection(stateName) {
  const el = $('#conn-status');
  el.dataset.state = stateName;
  $('.conn-text', el).textContent = {
    online: 'Connectat',
    connecting: 'Connectant…',
    offline: 'Sense connexió. Reintentant…',
  }[stateName] || '';
}

function connectEvents() {
  clearTimeout(reconnectTimer);
  if (typeof window.EventSource !== 'function') {
    // Very old engines: fall back to polling.
    setConnection('online');
    clearInterval(pollTimer);
    pollTimer = setInterval(refreshJobs, 2000);
    return;
  }
  if (eventSource) eventSource.close();
  setConnection('connecting');
  const es = new EventSource('api/events');
  eventSource = es;

  es.addEventListener('open', () => {
    reconnectDelay = 1000;
    setConnection('online');
    // Resynchronise: we may have missed events while disconnected.
    refreshJobs();
  });
  es.addEventListener('job', (ev) => {
    try {
      upsertJob(JSON.parse(ev.data), true);
      scheduleJobRender();
    } catch (err) {
      console.warn('Bad job event', err);
    }
  });
  es.addEventListener('error', () => {
    setConnection('offline');
    if (es.readyState === EventSource.CLOSED) {
      // The browser gave up (e.g. server restarted with an error status): retry ourselves.
      es.close();
      if (eventSource === es) eventSource = null;
      reconnectTimer = setTimeout(connectEvents, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    }
  });
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

/** Form control ids → Settings field names and value kinds. */
const SETTINGS_FIELDS = [
  ['set-download-dir', 'download_dir', 'text'],
  ['set-template', 'filename_template', 'text'],
  ['set-parallel', 'max_parallel', 'int'],
  ['set-quality', 'quality', 'text'],
  ['set-subtitles', 'subtitles', 'bool'],
  ['set-embed-subtitles', 'embed_subtitles', 'bool'],
  ['set-audio-description', 'audio_description', 'bool'],
  ['set-skip-existing', 'skip_existing', 'bool'],
];

async function loadSettings() {
  const settings = await api('settings');
  state.settings = settings;
  if (settings && QUALITY_LABELS[settings.quality]) state.quality = settings.quality;
  return settings;
}

async function loadSettingsForm() {
  const form = $('#settings-form');
  const loading = $('#settings-loading');
  if (!state.settings) {
    form.hidden = true;
    loading.hidden = false;
    try {
      await loadSettings();
    } catch (err) {
      loading.replaceChildren(emptyState("No s'ha pogut carregar la configuració.", errorMessage(err),
        h('button', { type: 'button', class: 'btn', text: 'Torna-ho a provar', onclick: () => { loading.replaceChildren(spinner('Carregant la configuració…')); loadSettingsForm(); } })));
      return;
    }
  }
  // Keep unsaved edits when the user comes back to this view.
  if (!form.dataset.loaded || !isSettingsDirty()) fillSettingsForm(state.settings);
  form.dataset.loaded = '1';
  loading.hidden = true;
  form.hidden = false;
}

function fillSettingsForm(settings) {
  for (const [id, key, kind] of SETTINGS_FIELDS) {
    const el = document.getElementById(id);
    const value = settings[key] === null || settings[key] === undefined ? '' : String(settings[key]);
    if (kind === 'bool') el.checked = Boolean(settings[key]);
    else {
      if (el.tagName === 'SELECT' && value && ![...el.options].some((o) => o.value === value)) {
        el.append(h('option', { value, text: value }));   // never show a blank field
      }
      el.value = value;
    }
  }
  $('#set-api-key').value = settings.api_key || '';
  $('#set-server-url').textContent = settings.server_port ? `http://127.0.0.1:${settings.server_port}` : window.location.origin;
  onSettingsInput();
}

function readSettingsForm() {
  const values = {};
  for (const [id, key, kind] of SETTINGS_FIELDS) {
    const el = document.getElementById(id);
    if (kind === 'bool') values[key] = el.checked;
    else if (kind === 'int') values[key] = parseInt(el.value, 10);
    else values[key] = el.value.trim();
  }
  return values;
}

function isSettingsDirty() {
  if (!state.settings) return false;
  const values = readSettingsForm();
  return Object.entries(values).some(([key, value]) => value !== state.settings[key]);
}

function onSettingsInput() {
  const values = readSettingsForm();
  // Embedding only makes sense when subtitles are downloaded.
  const embed = $('#set-embed-subtitles');
  embed.disabled = !values.subtitles;
  embed.closest('.check').classList.toggle('is-disabled', !values.subtitles);

  scheduleTemplatePreview(values);
  const dirty = isSettingsDirty();
  // A template problem never blocks saving: the server validates it (422 with
  // a Catalan message), and the preview below already explains what is wrong.
  const valid = values.download_dir.length > 0;
  $('#settings-dirty').hidden = !dirty;
  $('#settings-discard').disabled = !dirty;
  $('#settings-save').disabled = !dirty || !valid;
  // "Obrir carpeta" only opens the saved folder.
  const dirDirty = state.settings && values.download_dir !== state.settings.download_dir;
  const openBtn = $('#set-open-dir');
  openBtn.title = dirDirty ? 'Desa la configuració per obrir aquesta carpeta.' : '';
}

let previewTimer = 0;
let previewSeq = 0;

/** Ask the server for the example path (exactly what the downloader writes). */
function scheduleTemplatePreview(values) {
  clearTimeout(previewTimer);
  const template = values.filename_template;
  const downloadDir = values.download_dir;
  previewTimer = setTimeout(() => renderTemplatePreview(template, downloadDir), PREVIEW_DELAY);
}

async function renderTemplatePreview(template, downloadDir) {
  const out = $('#template-preview');
  const input = $('#set-template');
  const seq = ++previewSeq;
  const show = (text, ok) => {
    if (seq !== previewSeq) return;            // a newer edit is being previewed
    out.textContent = text;
    out.classList.toggle('is-error', !ok);
    if (ok) input.removeAttribute('aria-invalid'); else input.setAttribute('aria-invalid', 'true');
  };
  if (!template) { show('La plantilla no pot estar buida.', false); return; }
  try {
    const result = await api('naming/preview', {
      method: 'POST', body: { filename_template: template, download_dir: downloadDir || null },
    });
    if (result && result.ok) show(result.path, true);
    else show((result && result.error) || 'La plantilla no és vàlida.', false);
  } catch (err) {
    show(errorMessage(err), false);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  if (!state.settings) return;
  const values = readSettingsForm();
  if (!values.download_dir) { toast('Indica una carpeta de descàrrega.', { kind: 'warning' }); return; }
  if (!(values.max_parallel >= 1 && values.max_parallel <= MAX_PARALLEL_LIMIT)) {
    toast(`Les descàrregues simultànies han d'estar entre 1 i ${MAX_PARALLEL_LIMIT}.`, { kind: 'warning' });
    return;
  }

  const btn = $('#settings-save');
  btn.disabled = true;
  try {
    const previousDefault = state.settings.quality;
    const saved = await api('settings', { method: 'PUT', body: { ...state.settings, ...values } });
    state.settings = saved || { ...state.settings, ...values };
    // Keep the quality picked in the browse view unless the default changed.
    if (state.settings.quality !== previousDefault && QUALITY_LABELS[state.settings.quality]) {
      state.quality = state.settings.quality;
    }
    for (const sel of document.querySelectorAll('.quality-select')) sel.value = state.quality;
    fillSettingsForm(state.settings);
    toast("S'ha desat la configuració.", { kind: 'success' });
  } catch (err) {
    showError(err, "No s'ha pogut desar la configuració");
  } finally {
    onSettingsInput();
  }
}

async function copyApiKey() {
  const input = /** @type {HTMLInputElement} */ ($('#set-api-key'));
  if (!input.value) return;
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    input.focus();
    input.select();
    try { document.execCommand('copy'); } catch { /* ignore */ }
  }
  toast("S'ha copiat la clau.", { kind: 'success', timeout: 2500 });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function wireStaticUi() {
  for (const btn of document.querySelectorAll('.nav-btn')) {
    btn.addEventListener('click', () => showView(btn.dataset.view));
  }

  $('#resolve-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submitResolve($('#resolve-input').value);
  });

  $('#clear-finished').addEventListener('click', clearFinished);
  $('#open-downloads-folder').addEventListener('click', () => openFolder(null));

  // Settings form
  const qualitySelect = $('#set-quality');
  qualitySelect.replaceChildren(...QUALITY_OPTIONS.map(([value, label]) => h('option', { value, text: label })));
  const form = $('#settings-form');
  form.addEventListener('input', onSettingsInput);
  form.addEventListener('change', onSettingsInput);
  form.addEventListener('submit', saveSettings);
  $('#settings-discard').addEventListener('click', () => { if (state.settings) fillSettingsForm(state.settings); });
  $('#set-open-dir').addEventListener('click', () => {
    const typed = $('#set-download-dir').value.trim();
    if (state.settings && typed !== state.settings.download_dir) {
      toast('Desa la configuració per obrir aquesta carpeta.', { kind: 'warning' });
      return;
    }
    openFolder(null);
  });
  $('#set-template-reset').addEventListener('click', () => { $('#set-template').value = DEFAULT_TEMPLATE; onSettingsInput(); });
  $('#set-copy-key').addEventListener('click', copyApiKey);
}

async function checkHealth() {
  try {
    const health = await api('health');
    const parts = ['3Cat Downloader'];
    if (health && health.version) parts.push(`versió ${health.version}`);
    $('#footer-info').textContent = parts.join(' · ');
    if (health && health.ffmpeg === false) {
      toast("No s'ha trobat ffmpeg. Les descàrregues en màxima qualitat poden fallar; prova amb «MP4 directe».", {
        kind: 'warning', title: 'Falta un component', timeout: 0,
      });
    }
  } catch (err) {
    showError(err, 'El servidor no respon');
  }
}

async function boot() {
  wireStaticUi();
  renderHome();
  renderJobs();
  connectEvents();
  checkHealth();
  refreshJobs();
  try {
    await loadSettings();
    if (state.home.kind === 'program' || state.home.kind === 'video') renderHome();
  } catch (err) {
    console.warn('Could not load settings', err);
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
