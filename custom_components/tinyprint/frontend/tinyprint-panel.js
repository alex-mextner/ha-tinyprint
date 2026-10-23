/* Native HA web component. No CDN, token storage or automatic printing.
 * The entry file defines the element at once; the renderer and the editor rules load as ES modules
 * relative to this file, so the versioned static path versions every import. */
const BASE_URL = new URL('./', import.meta.url);
const MODULES = Promise.all([
  import('./renderer/index.js'),
  import('./renderer/image.js'),
  import('./renderer/pack.js'),
  import('./studio/state.js'),
  import('./studio/status.js'),
  import('./studio/options.js'),
  import('./studio/live.js'),
  import('./studio/strip.js'),
]);
MODULES.catch(() => {}); // mount() reports the failure

const FIELD_CAPTIONS = {title: 'Заголовок', text: 'Текст', footer: 'Подпись снизу', qr: 'Ссылка или данные QR', location: 'Расположение', material: 'Материал', color: 'Цвет', weight: 'Остаток / масса', nozzle: 'Температура сопла'};
const CHECKLIST_TEXT_CAPTION = 'Пункты — каждый с новой строки';
const EMPTY_STRIP_TEXT = 'Введите текст, QR или загрузите картинку: лента появится здесь.';
const FILE_HINT = 'До 2 МиБ и 4 миллионов пикселей. Файл не покидает браузер: на печать уходит только готовый растр.';
const BROWSER_ONLY_NOTICE = 'Несколько колонок и Tiny5 шириной 2x работают только в панели, поэтому действие для автоматизации недоступно: печатайте из панели.';
// While one of these sliders is dragged the strip shows the coarse-grid approximate render.
const SLIDER_OPTIONS = ['font_width', 'font_weight'];
const RELEASE_EVENTS = ['pointerup', 'pointercancel'];

function element(tag, attributes = {}, text) {
  const created = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) created.setAttribute(name, value);
  if (text !== undefined) created.textContent = text;
  return created;
}
// A required field the user has not filled yet still takes its place on the strip: it is drawn as
// an invisible character so what was typed stays visible. Such a render is never printable.
const PLACEHOLDER_CHAR = '\u200b';
// The options a print_document of each kind may carry; image options never travel with them.
const TEMPLATE_OPTION_KEYS = ['font', 'font_size', 'font_width', 'font_weight', 'columns', 'align', 'border', 'qr_size', 'qr_correction'];
const QR_OPTION_KEYS = ['border', 'qr_size', 'qr_correction'];
const RENDER_ERRORS = {
  bad_document: 'Проверьте значения: обязательные поля не пусты, числа в допустимых пределах, текст не длиннее лимита.',
  too_tall: 'Этикетка длиннее 2048 строк: сократите текст или размер.',
  blank_qr: 'Введите данные для QR.',
  qr_modules_too_small: 'QR не помещается в заданный размер: увеличьте размер QR или сократите данные.',
  qr_too_long: 'Данных для QR слишком много.',
  image_too_large: 'Изображение слишком большое: до 2 МиБ и 4 миллионов пикселей.',
  image_unreadable: 'Не удалось прочитать изображение.',
  font_missing: 'Не удалось загрузить шрифт.',
  font_load_failed: 'Не удалось подготовить шрифт.',
};

class TinyPrintPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.epoch = 0; // bumped on teardown: work started before it must not touch the next mount
    this.imageLoadSequence = 0; // only ever grows, so a decode from an earlier mount is always stale
    this.initState();
  }

  /** Everything a mount owns; the constructor and every teardown start from here. */
  initState() {
    this.mode = 'template';
    this.templates = {};
    this.presets = {};
    this.printers = [];
    this.inFlightStates = [];
    this.drafts = {};
    this.renderer = null;
    this.scheduler = null;
    this.strip = null;
    this.stripObserver = null;
    this.zoomChosen = false; // until the user presses #zoom the strip's room picks the scale
    this.lastResult = null;
    this.failure = null;
    this.image = null;
    this.noticeFromRender = false;
    this.pendingCall = false;
    this.printing = false;
    this.activeDevice = '';
    this.jobResponse = null;
    this.modules = null;
    this.mounting = null;
    this.ready = false;
    this.loaded = false;
    this.loading = false;
    this.loadAttempted = false;
    this.requestedSignature = '';
    this.needsDevicePick = false;
    this.fonts = {};
    this.fontName = null; // the font the size, width and weight controls were built for
    this.releaseSlider = null;
    this.approximateRequested = false; // the newest render request was a slider-drag approximation
  }

  set hass(value) {
    this._hass = value;
    if (this.ready) this.onHass();
  }
  get hass() { return this._hass; }
  connectedCallback() { this.mounting ??= this.mount().catch(error => { this.shadowRoot.textContent = `Не удалось запустить панель: ${error.message || error}`; }); }
  // HA builds a new element on every visit: release the scheduler and the registered font faces, and
  // let the next connect mount from scratch (a disposed renderer cannot be reused).
  disconnectedCallback() {
    if (this.releaseSlider) for (const name of RELEASE_EVENTS) window.removeEventListener(name, this.releaseSlider);
    this.epoch += 1;
    this.imageLoadSequence += 1;
    this.stripObserver?.disconnect();
    this.strip?.dispose();
    this.scheduler?.dispose();
    this.renderer?.dispose();
    this.initState();
  }
  $(selector) { return this.shadowRoot.querySelector(selector); }

  async mount() {
    const epoch = this.epoch;
    try {
      const [renderer, image, pack, state, status, options, live, strip] = await MODULES;
      this.modules = { renderer, image, pack, state, status, options, live, strip };
    } catch (error) {
      this.shadowRoot.textContent = `Не удалось загрузить модули панели: ${error.message || error}`;
      return;
    }
    if (epoch !== this.epoch) return;
    this.drafts = this.modules.state.emptyDrafts();
    this.buildDom();
    this.scheduler = new this.modules.live.RenderScheduler(
      (input, generation) => this.run(input, generation),
      (result) => this.onResult(result),
      (error, generation) => this.onFailure(error, generation),
    );
    this.ready = true;
    this.updateButtons();
    this.onHass();
  }

  buildDom() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; height:100%; overflow:auto; color:var(--primary-text-color,#222832); background:var(--primary-background-color,#f4f6f8); font:15px/1.5 var(--paper-font-body1_-_font-family,system-ui,sans-serif); }
        * { box-sizing:border-box; } button,input,textarea,select { font:inherit; }
        button,select,input,textarea { color:inherit; } button { cursor:pointer; touch-action:manipulation; }
        button:disabled { cursor:default; opacity:.45; } button:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible { outline:3px solid var(--primary-color,#007a73); outline-offset:2px; }
        header { display:flex; gap:16px; align-items:center; padding:20px 28px; background:var(--card-background-color,#fff); border-bottom:1px solid var(--divider-color,#dde1e7); }
        h1 { font-size:25px; margin:0; letter-spacing:-.6px; } .eyebrow { font-size:10px; letter-spacing:1.8px; color:var(--secondary-text-color,#697580); font-weight:700; }
        header .mark { width:42px; height:42px; display:grid; place-items:center; border-radius:12px; background:var(--primary-color,#007a73); color:white; font-size:23px; }
        .local { margin-left:auto; font-size:12px; color:var(--secondary-text-color,#697580); }
        main { max-width:1220px; margin:auto; padding:24px; }
        .devicebar { display:flex; flex-wrap:wrap; align-items:center; gap:12px; margin-bottom:22px; }
        .devicebar label { flex:1; min-width:210px; } .devicebar select { margin-top:4px; }
        .badge { border-radius:999px; background:var(--secondary-background-color,#e8eeed); padding:6px 12px; font-size:12px; }
        .layout { display:grid; grid-template-columns:minmax(0,1fr); align-items:start; gap:24px; }
        .card { border:1px solid var(--divider-color,#dde1e7); background:var(--card-background-color,#fff); border-radius:16px; padding:22px; min-width:0; }
        .tabs { display:flex; gap:4px; padding:4px; border-radius:11px; background:var(--secondary-background-color,#edf0f3); margin-bottom:22px; }
        .tabs button { border:0; background:transparent; flex:1; padding:10px 6px; border-radius:8px; }
        .tabs button[aria-selected=true] { background:var(--card-background-color,#fff); color:var(--primary-color,#007a73); box-shadow:0 1px 4px #0001; font-weight:650; }
        label { display:block; font-size:13px; font-weight:550; } label small { font-weight:400; }
        input:not([type=checkbox]):not([type=range]),textarea,select { width:100%; background:var(--card-background-color,#fff); border:1px solid var(--divider-color,#cdd5dd); padding:10px 12px; border-radius:8px; min-height:44px; }
        textarea { min-height:100px; resize:vertical; } input[type=range] { width:100%; accent-color:var(--primary-color,#007a73); min-height:32px; }
        .field { margin:0 0 15px; } .field label { margin-bottom:5px; } .row { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
        .row > * { min-width:0; } .hint,small { color:var(--secondary-text-color,#697580); font-size:12px; }
        .hint { margin:7px 0 17px; } fieldset { border:0; padding:0; margin:0; min-width:0; }
        .check { display:flex; align-items:center; gap:8px; min-height:38px; } input[type=checkbox] { width:18px; height:18px; accent-color:var(--primary-color,#007a73); }
        summary { cursor:pointer; padding:12px 0; font-weight:550; font-size:13px; } details { border-top:1px solid var(--divider-color,#e2e5eb); margin-top:10px; }
        .buttons { display:flex; gap:10px; flex-wrap:wrap; margin-top:20px; }
        .button { border:1px solid var(--divider-color,#cdd5dd); background:var(--card-background-color,#fff); border-radius:9px; padding:10px 15px; min-height:44px; }
        .primary { background:var(--primary-color,#007a73); border-color:transparent; color:var(--text-primary-color,#fff); font-weight:650; flex:1; }
        .strip-card { min-width:0; } h2 { margin:0; font-size:18px; } .strip-head { display:flex; justify-content:space-between; align-items:center; gap:10px; }
        .canvas { background:var(--secondary-background-color,#edf0f3); border-radius:10px; min-height:300px; max-height:600px; overflow:auto; scrollbar-gutter:stable; margin:18px 0 12px; padding:20px 12px; display:flex; flex-direction:column; align-items:safe center; }
        .chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; } .chip { border:1px dashed var(--secondary-text-color,#697580); background:transparent; border-radius:999px; padding:4px 12px; min-height:32px; font-size:13px; }
        .box { background:white; } .overlay::selection { background:rgba(0,122,115,.3); color:transparent; }
        .overlay:not(.loose)::placeholder { color:transparent; opacity:1; } .overlay:not(.loose):focus-visible { outline:2px solid var(--primary-color,#007a73); outline-offset:0; } .overlay.slot:not(:focus) { pointer-events:none; }
        #docked:not(:empty) { margin-top:16px; }
        .live { min-width:0; } .bar { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:2px 14px; align-items:end; margin-top:12px; }
        .bar .field { margin:0 0 8px; } .bar .check { min-height:44px; } .bar details { grid-column:1/-1; margin-top:0; } .bar summary { padding:6px 0; }
        #options { margin:0; } #font-hint { margin:0 0 6px; color:var(--warning-color,#8a5a00); }
        @media(min-width:900px) and (min-height:900px) { .live { position:sticky; top:0; z-index:2; background:var(--card-background-color,#fff); padding:4px 0 2px; } .live .canvas { min-height:240px; max-height:calc(100vh - 470px); } }
        .empty { padding:54px 12px; text-align:center; max-width:270px; color:var(--secondary-text-color,#697580); font-size:14px; }
        .empty .symbol { font-size:40px; display:block; margin-bottom:12px; }
        #raster { display:block; width:384px; max-width:none; height:auto; background:white; box-shadow:0 4px 15px #0002; image-rendering:pixelated; }
        #raster.ghost { opacity:.55; }
        .caption { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; font-size:12px; color:var(--secondary-text-color,#697580); }
        #notice { padding:12px 15px; border-radius:9px; background:var(--secondary-background-color,#e9efee); margin:18px 0; white-space:pre-wrap; overflow-wrap:anywhere; }
        #notice.error { border-left:4px solid var(--error-color,#be3b3b); }
        pre { max-height:300px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font-size:11px; background:var(--secondary-background-color,#edf0f3); padding:12px; border-radius:8px; }
        [hidden] { display:none !important; }
        @media(max-width:760px) { header { padding:14px 12px; gap:10px; } .local { display:none; } main { padding:14px 12px 32px; } .layout { gap:16px; } .card { padding:17px; } .devicebar { gap:8px; } .devicebar .button { font-size:12px; } .canvas { max-height:450px; } }
      </style>
      <header><ha-menu-button aria-label="Открыть меню"></ha-menu-button><div class="mark" aria-hidden="true">▤</div><div><div class="eyebrow">ЛОКАЛЬНАЯ ТЕРМОПЕЧАТЬ</div><h1>TinyPrint</h1></div><span class="local">Текст · QR · Изображения</span></header>
      <main>
        <div class="devicebar"><label>Принтер<select id="device"><option value="">Загрузка принтеров…</option></select></label><span id="device-status" class="badge">Подключение к HA</span><span id="job-status" class="badge" role="status" hidden></span><button id="refresh" class="button" aria-label="Обновить состояние">Обновить</button><button id="diagnose" class="button">Проверить Bluetooth</button></div>
        <div class="layout">
          <section class="card" aria-label="Редактор этикетки">
            <div class="tabs" role="tablist"><button data-mode="template" role="tab" aria-selected="true">Шаблоны</button><button data-mode="qr" role="tab" aria-selected="false">QR-код</button><button data-mode="image" role="tab" aria-selected="false">Картинка</button></div>
            <fieldset id="editor">
              <div id="template-form"><div class="field"><label for="template">Макет этикетки</label><select id="template"></select></div></div>
              <div id="qr-form" hidden><div class="field"><label for="qr-data">Ссылка или данные QR</label><textarea id="qr-data" maxlength="500" placeholder="https://…"></textarea></div><p class="hint">Целые модули и белое поле сохраняются. Фотофильтры к QR не применяются.</p></div>
              <div id="image-form" hidden>
                <div class="field"><label for="image-file">Изображение с телефона или компьютера</label><input id="image-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"><p id="file-name" class="hint">${FILE_HINT}</p></div>
              </div>
            </fieldset>
            <div class="buttons"><button id="print" class="button primary" disabled>Печатать</button><button id="cancel" class="button" hidden>Остановить</button></div>
          </section>
          <section class="card strip-card" aria-label="Бумажная лента">
            <div class="live">
              <div class="strip-head"><h2>На бумаге</h2><span class="badge">384 точки</span><button id="zoom" class="button" aria-label="Масштаб ленты" aria-pressed="false">1×</button></div>
              <fieldset id="options" aria-label="Параметры этикетки">
                <div id="text-options" class="bar">
                  <div class="field"><label for="font">Шрифт</label><select id="font" data-option="font"></select></div>
                  <div class="field" id="size-field"></div><div class="field" id="width-field"></div><div class="field" id="weight-field"></div>
                  <div class="field" id="columns-field"><label for="columns">Колонки</label><select id="columns" data-option="columns" data-number><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></div>
                  <div class="field"><label for="align">Выравнивание</label><select id="align" data-option="align"><option value="left">Слева</option><option value="center">По центру</option><option value="right">Справа</option></select></div>
                </div>
                <p id="font-hint" class="hint" role="status" hidden></p>
                <div id="shared-options" class="bar">
                  <label class="check"><input type="checkbox" id="border" data-option="border">Рамка этикетки</label>
                  <details id="qr-options"><summary>Размер и надёжность QR</summary><div class="row"><div class="field"><label for="qr_size">Размер QR, точки</label><input id="qr_size" data-option="qr_size" data-number type="number" min="96" max="360" value="320"></div><div class="field"><label for="qr_correction">Коррекция ошибок</label><select id="qr_correction" data-option="qr_correction"><option value="M">M · стандартная</option><option value="Q">Q · повышенная</option><option value="H">H · максимальная</option></select></div></div></details>
                </div>
                <div id="image-options" class="bar" hidden>
                  <div class="field"><label for="preset">Подготовка к термопечати</label><select id="preset" data-option="preset"></select></div>
                  <div class="field"><label for="method">Преобразование</label><select id="method" data-option="method"><option value="floyd_steinberg">Floyd–Steinberg</option><option value="threshold">Чёрное / белое</option><option value="ordered">Упорядоченный растр</option></select></div>
                  <div class="field"><label for="rotate">Поворот</label><select id="rotate" data-option="rotate" data-number><option value="0">0°</option><option value="90">90°</option><option value="180">180°</option><option value="270">270°</option></select></div>
                  ${this.range('brightness','Яркость',.25,2,.05,1.1)}${this.range('contrast','Контраст',.25,3,.05,1.25)}${this.range('sharpness','Резкость',0,3,.1,1.2)}${this.range('threshold','Порог чёрного',1,254,1,128)}
                  <div class="field"><label for="fit">Масштаб</label><select id="fit" data-option="fit"><option value="width">По заданной ширине</option><option value="original">Без увеличения</option></select></div>
                  <div class="field"><label for="width">Ширина, точки</label><input id="width" data-option="width" data-number type="number" min="96" max="384" value="384"></div>
                  <label class="check"><input type="checkbox" id="invert" data-option="invert">Инвертировать изображение</label>
                </div>
              </fieldset>
              <div id="paper"></div><div class="caption"><span id="dimensions"></span><span id="ink"></span></div>
            </div>
            <div id="notice" role="status" aria-live="polite" hidden></div><details id="diagnostics"><summary>Диагностика задания</summary><pre id="diagnostic-data">Проверка ещё не запускалась.</pre></details><details id="automation"><summary>Действие для автоматизации</summary><p class="hint">JSON ниже также допустим в YAML. Он описывает ровно ту этикетку, что показана на ленте.</p><pre id="action-data"></pre><button id="copy" class="button">Скопировать действие</button></details></section>
        </div>
      </main>`;
    this.shadowRoot.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => this.switchMode(button.dataset.mode)));
    this.$('#template').addEventListener('change', () => { this.syncFields(); this.renderIfChanged(); });
    this.$('#device').addEventListener('change', () => { this.needsDevicePick = this.$('#device').value === ''; this.deviceStatus(); this.refreshJob(); this.showAction(); });
    this.$('#preset').addEventListener('change', () => this.applyPreset());
    this.$('#image-file').addEventListener('change', event => this.readImage(event));
    this.$('#editor').addEventListener('input', event => this.onEdit(event));
    this.$('#editor').addEventListener('change', event => { if (event.target !== this.$('#template')) this.renderIfChanged(); });
    this.$('#options').addEventListener('input', event => this.onEdit(event));
    this.$('#options').addEventListener('change', event => this.onOptionChange(event));
    // A drag that ends where it started, or whose pointer is cancelled or lost, fires no `change`.
    this.releaseSlider = () => { if (this.approximateRequested) this.renderIfChanged(); };
    for (const name of RELEASE_EVENTS) window.addEventListener(name, this.releaseSlider);
    this.$('#options').addEventListener('lostpointercapture', this.releaseSlider);
    this.$('#print').addEventListener('click', () => this.print());
    this.$('#cancel').addEventListener('click', () => this.cancel());
    this.$('#refresh').addEventListener('click', () => { if (this.failure) this.renderNow(); this.loadInfo(); });
    this.$('#diagnose').addEventListener('click', () => this.diagnose());
    this.$('#automation').addEventListener('toggle', () => this.showAction());
    this.$('#copy').addEventListener('click', () => this.copyAction());
    this.strip = new this.modules.strip.PaperStrip(this.$('#paper'));
    this.strip.show(null, EMPTY_STRIP_TEXT);
    this.$('#zoom').addEventListener('click', () => { this.zoomChosen = true; this.setZoom(this.zoom === 1 ? 2 : 1); });
    this.stripObserver = new ResizeObserver(() => this.fitZoom());
    this.stripObserver.observe(this.$('#paper'));
  }

  range(id, title, min, max, step, value) {
    return `<div class="field"><label for="${id}">${title} <small id="value-${id}">${value}</small></label><input id="${id}" data-option="${id}" data-number type="range" min="${min}" max="${max}" step="${step}" value="${value}"></div>`;
  }

  /**
   * Size, width and weight controls as the font's table allows them: a number or slider for a range,
   * a select for a closed list, nothing for a fixed weight. `values` are the options to show.
   */
  buildFontControls(fontName, values) {
    const { sizes, width, weight } = this.fonts[fontName];
    this.fontName = fontName;
    const field = (id, title, control, slider) => {
      const label = element('label', { for: id }, title);
      if (slider) label.append(' ', element('small', { id: `value-${id}` }, control.value));
      return [label, control];
    };
    const choice = (id, items, value) => {
      const select = element('select', { id, 'data-option': id, 'data-number': '' });
      for (const [optionValue, text] of items) select.append(element('option', { value: optionValue }, text));
      select.value = value;
      return select;
    };
    const input = (id, type, spec, value) => {
      const control = element('input', { id, 'data-option': id, 'data-number': '', type, min: spec.min, max: spec.max, ...(type === 'range' ? { step: 1 } : {}) });
      control.value = value;
      return control;
    };
    this.$('#size-field').replaceChildren(...field('font_size', 'Размер текста', sizes.values
      ? choice('font_size', sizes.values.map(v => [v, v]), values.font_size)
      : input('font_size', 'number', sizes, values.font_size)));
    this.$('#width-field').replaceChildren(...(width.kind === 'axis'
      ? field('font_width', 'Ширина шрифта', input('font_width', 'range', width, values.font_width), true)
      : field('font_width', 'Ширина шрифта', choice('font_width', width.values.map(v => [v, `${v / 100}x`]), values.font_width))));
    this.$('#weight-field').hidden = weight.kind !== 'axis';
    this.$('#weight-field').replaceChildren(...(weight.kind === 'axis'
      ? field('font_weight', 'Жирность', input('font_weight', 'range', weight, values.font_weight), true)
      : []));
  }
  /** A new font: the controls change to its table, width and weight go to its defaults. */
  switchFont() {
    const next = this.$('#font').value;
    const coerced = this.modules.options.coerceForFont({ ...this.effectiveOptions(), font: this.fontName }, this.fonts, next);
    this.buildFontControls(next, coerced);
  }
  /** What follows from the controls: the columns control exists only for a template with a main text. */
  updateOptionUi() {
    const spec = this.templates[this.$('#template').value];
    this.$('#columns-field').hidden = !(this.mode === 'template' && spec?.fields.includes('text'));
    const { font, font_size, font_width, font_weight } = this.effectiveOptions();
    const hint = this.mode === 'template' ? this.modules.options.closingCounterHint(font, font_size, font_width, font_weight) : null;
    this.$('#font-hint').hidden = !hint;
    this.$('#font-hint').textContent = hint ?? '';
  }

  /** The `hass` object is replaced on every update: it is read here and in `jobState`, never kept. */
  onHass() {
    const menu = this.$('ha-menu-button');
    if (menu) menu.hass = this.hass;
    if (this.hass && !this.loaded && !this.loading && !this.loadAttempted) this.loadInfo();
    this.refreshJob();
  }

  async call(service, service_data = {}, return_response = true) {
    if (!this.hass) throw new Error('Нет соединения с Home Assistant');
    const result = await this.hass.callWS({ type: 'call_service', domain: 'tinyprint', service, service_data, return_response });
    return result?.response ?? result;
  }
  notice(text, error = false) {
    this.noticeFromRender = false;
    this.showNotice(text, error);
  }
  showNotice(text, error) {
    const target = this.$('#notice');
    target.hidden = !text;
    target.textContent = text;
    target.classList.toggle('error', error);
  }
  describe(error) {
    return RENDER_ERRORS[error?.code] ?? (error?.message || String(error));
  }
  fillSelect(element, items, chosen) {
    element.replaceChildren();
    for (const [value, name] of items) {
      const option = document.createElement('option');
      option.value = value; option.textContent = name; element.append(option);
    }
    if (items.some(([value]) => value === chosen)) element.value = chosen;
  }

  async loadInfo() {
    if (this.loading || !this.ready) return;
    const epoch = this.epoch;
    this.loading = true; this.loadAttempted = true;
    try {
      const info = await this.call('printer_info');
      if (epoch !== this.epoch) return;
      this.printers = info.printers;
      this.inFlightStates = info.in_flight_states ?? [];
      const chosen = this.$('#device').value;
      const devices = this.printers.filter(p => p.device_id).map(p => [p.device_id, p.name]);
      // A printer that vanished is never silently replaced by another one: the placeholder stays
      // until the user picks a printer.
      if (chosen !== '' && !devices.some(([id]) => id === chosen)) {
        this.needsDevicePick = true;
        this.notice('Выбранный принтер больше недоступен. Выберите принтер заново.', true);
      }
      const vanished = this.needsDevicePick && devices.length > 0;
      if (vanished) devices.unshift(['', 'Выберите принтер']);
      this.fillSelect(this.$('#device'), devices, vanished ? '' : chosen);
      if (!this.$('#device').options.length) this.fillSelect(this.$('#device'), [['', 'Принтер не найден — печать недоступна']], '');
      if (!this.loaded) {
        this.templates = info.templates; this.presets = info.presets;
        this.fonts = info.fonts;
        this.renderer = new this.modules.renderer.Renderer({ baseUrl: BASE_URL, fonts: info.fonts, templates: info.templates });
        this.fillSelect(this.$('#template'), Object.entries(this.templates).map(([key, item]) => [key, item.name]), 'note');
        this.fillSelect(this.$('#preset'), Object.entries(this.presets).map(([key, item]) => [key, item.name]), 'photo');
        this.fillSelect(this.$('#font'), Object.entries(this.fonts).map(([key, item]) => [key, item.label]), this.modules.renderer.DEFAULT_OPTIONS.font);
        this.buildFontControls(this.$('#font').value, this.modules.renderer.DEFAULT_OPTIONS);
        this.loaded = true;
        this.syncFields();
        this.renderNow();
      }
      this.deviceStatus(); this.refreshJob(); this.showAction();
    } catch (error) { if (epoch === this.epoch) this.notice(error.message || String(error), true); }
    finally { if (epoch === this.epoch) this.loading = false; }
  }
  deviceStatus() {
    const printer = this.printers?.find(p => p.device_id === this.$('#device').value);
    const bridge = printer?.transport?.mode === 'bridge';
    const localLE = printer?.transport?.mode === 'local_le';
    // Bridge reachability says nothing about the printer's own radio link; only diagnose proves that.
    const bridgeStates = {reachable:'Мост доступен', busy:'Мост занят заданием', auth_failed:'Ошибка доступа к мосту', unreachable:'Мост недоступен', misconfigured:'Мост настроен неверно', unchecked:'Проверка моста'};
    this.$('#device-status').textContent = !printer ? 'Офлайн-предпросмотр' : bridge ? (bridgeStates[printer.transport.bridge_state] || 'Состояние моста неизвестно') : localLE ? (printer.visible ? 'Принтер обнаружен' : 'Принтер не найден') : (printer.visible ? 'Виден по Bluetooth' : 'Не виден по Bluetooth');
    this.$('#diagnose').textContent = bridge ? 'Проверить через мост' : localLE ? 'Проверить подключение' : 'Проверить Bluetooth';
    // In bridge mode Bluetooth fields come from Home Assistant's own radio, not the printer's link.
    if (printer) this.$('#diagnostic-data').textContent = JSON.stringify({status: printer.status, transport: printer.transport, last_error: printer.last_error, job: this.jobResponse?.deviceId === printer.device_id ? this.jobResponse.response : undefined, [bridge ? 'local_rssi_raw' : 'rssi_raw']: printer.rssi_raw, ...printer.last_result}, null, 2);
  }

  /** The job sensor of a device, read from the CURRENT `hass` each time; a missing entity never locks. */
  jobState(deviceId) {
    const entityId = this.printers.find(p => p.device_id === deviceId)?.entity_id ?? null;
    const stateObject = entityId ? this.hass?.states?.[entityId] : undefined;
    // Callable from outside before the modules have loaded: nothing is in flight yet.
    const inFlight = Boolean(this.modules) && this.modules.status.jobInFlight(stateObject, this.inFlightStates);
    return { entityId, state: stateObject?.state, inFlight };
  }
  refreshJob() {
    const { entityId } = this.jobState(this.$('#device').value);
    const label = entityId ? this.modules.status.statusText(this.hass?.states?.[entityId]) : null;
    const target = this.$('#job-status');
    target.textContent = label ?? '';
    target.hidden = !label;
    this.updateButtons();
  }

  fieldCaption(template, field) {
    return template === 'checklist' && field === 'text' ? CHECKLIST_TEXT_CAPTION : (FIELD_CAPTIONS[field] || field);
  }
  /** One entry per editable field of the chosen template, values read from the drafts (never stale). */
  fieldSpecs() {
    const key = this.$('#template').value;
    const spec = this.templates[key];
    if (this.mode !== 'template' || !spec) return [];
    const draft = this.modules.state.draftFor(this.drafts, key);
    return spec.fields.map(field => ({
      key: field,
      caption: this.fieldCaption(key, field),
      multiline: field === 'text',
      value: draft[field] || '',
      // The example is a placeholder only: it is never a value, so it can never be printed.
      placeholder: spec.example[field] ?? '',
      maxLength: field === 'qr' ? 500 : 3000,
      required: spec.required.includes(field),
    }));
  }
  /** The controls' values over the renderer defaults (a control that does not exist yet keeps its default). */
  effectiveOptions() { return { ...this.modules.renderer.DEFAULT_OPTIONS, ...this.readOptions() }; }
  /** Overlay or docked per field, from the options now in the controls. */
  syncFields() {
    this.updateOptionUi();
    const options = this.effectiveOptions();
    const { fieldEditMode, EDIT_POLICY } = this.modules.strip;
    const mode = field => fieldEditMode(field, { columns: options.columns, policy: EDIT_POLICY, font: options.font, width: options.font_width });
    this.strip.setFields(this.fieldSpecs(), mode, (field, value) => this.onFieldInput(field, value));
  }
  onFieldInput(field, value) {
    this.drafts = this.modules.state.withDraftField(this.drafts, this.$('#template').value, field, value);
    this.renderIfChanged();
  }
  switchMode(mode) {
    if (this.printing) return;
    this.mode = mode;
    for (const id of ['template', 'qr', 'image']) this.$(`#${id}-form`).hidden = id !== mode;
    this.$('#text-options').hidden = mode !== 'template';
    this.$('#shared-options').hidden = mode === 'image';
    this.$('#image-options').hidden = mode !== 'image';
    this.shadowRoot.querySelectorAll('[data-mode]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.mode === mode)));
    this.syncFields();
    this.renderNow();
  }
  onEdit(event) {
    const control = event.target;
    if (control.type === 'range') this.$(`#value-${control.id}`).textContent = control.value;
    if (control.id !== 'preset' && control.id !== 'font') this.renderIfChanged(control.type === 'range' && SLIDER_OPTIONS.includes(control.id));
  }
  /** `change` ends a slider drag (also a keyboard step): the exact value is rendered. */
  onOptionChange(event) {
    if (event.target.id === 'preset') return; // applyPreset renders
    if (event.target.id === 'font') this.switchFont();
    this.renderIfChanged();
  }
  /** Visible option controls only: a template never carries image options and vice versa. */
  readOptions() {
    const options = {};
    this.shadowRoot.querySelectorAll('[data-option]').forEach(control => {
      if (control.closest('[hidden]') || (control.tagName === 'SELECT' && control.options.length === 0)) return;
      options[control.dataset.option] = control.type === 'checkbox' ? control.checked : (control.hasAttribute('data-number') ? Number(control.value) : control.value);
    });
    return options;
  }
  /** The renderer does not apply `preset`: its values are copied into the controls. */
  applyPreset() {
    if (!this.presets[this.$('#preset').value]) return;
    const options = this.modules.options.applyPreset(this.readOptions(), this.presets, this.$('#preset').value);
    for (const key of this.modules.options.ENHANCEMENT_KEYS) {
      this.$(`#${key}`).value = options[key];
      const label = this.$(`#value-${key}`); if (label) label.textContent = String(options[key]);
    }
    this.renderNow();
  }
  async readImage(event) {
    const file = event.target.files[0];
    const sequence = ++this.imageLoadSequence;
    this.image = null;
    this.$('#file-name').textContent = FILE_HINT;
    this.renderNow();
    if (!file) return;
    try {
      const decoded = await this.modules.image.decodeUpload(file);
      if (sequence !== this.imageLoadSequence || !this.isConnected) { decoded.bitmap.close(); return; }
      this.image = decoded;
      this.$('#file-name').textContent = `${file.name} · ${Math.ceil(file.size / 1024)} КиБ · файл остаётся в браузере`;
      this.notice('Изображение загружено.');
      this.renderNow();
    } catch (error) { if (sequence === this.imageLoadSequence) this.notice(this.describe(error), true); }
  }

  inputState() {
    const template = this.$('#template').value;
    return { mode: this.mode, template, fields: this.modules.state.draftFor(this.drafts, template), qrText: this.$('#qr-data').value, hasImage: Boolean(this.image) };
  }
  /** False while the template list is not loaded: the template rules cannot be evaluated yet. */
  inputKnown(s) { return s.mode !== 'template' || Boolean(this.templates[s.template]); }

  /** Re-render with the current draft, whatever changed. */
  renderNow(approximate = false) {
    if (!this.loaded || !this.scheduler) return;
    this.requestedSignature = this.signature(approximate);
    this.approximateRequested = approximate;
    this.failure = null;
    this.scheduler.request(() => {
      const s = this.inputState();
      const { document: doc, ghost } = this.modules.state.effectiveDocument(s, this.templates, this.readOptions());
      const missing = ghost ? [] : this.modules.state.missingRequired(s, this.templates);
      // Even an invisible character encodes into a scannable QR: a label without its QR data draws nothing.
      if (missing.includes('qr')) return { document: null, ghost: false, note: `${this.modules.state.BLOCKER_TEXT.required_blank} ${this.fieldCaption(s.template, 'qr')}`, image: null };
      for (const key of missing) doc.fields[key] = PLACEHOLDER_CHAR;
      return { document: doc, ghost, partial: missing.length > 0, image: this.image, approximate };
    });
    this.updateButtons();
  }
  /**
   * Handlers call this: a `change` that follows the `input` with the same content (the blur caused
   * by clicking Print) must not start a render, or the click would land on render_pending.
   */
  renderIfChanged(approximate = false) {
    if (this.loaded && this.signature(approximate) !== this.requestedSignature) this.renderNow(approximate);
  }
  /** The inputs plus whether the render is the slider-drag approximation: the release must re-render exactly. */
  signature(approximate = false) {
    return JSON.stringify([this.inputState(), this.readOptions(), this.image?.id ?? null, approximate]);
  }
  /** Scheduler `run`: stamps the generation and the ghost flag onto the result. */
  async run(input, generation) {
    if (!input.document) return { generation, ghost: false, exact: false, empty: true, note: input.note, rows: 0, bitmap: null, pixels: null, document: null, inkPercent: 0 };
    const result = await this.renderer.render({ ...input, generation });
    const stamped = { ...result, generation, ghost: Boolean(input.ghost) };
    // A partial render (a required field still blank) only fills the strip: it carries no bitmap or document.
    return input.partial ? { ...stamped, exact: false, partial: true, bitmap: null, document: null, inkPercent: 0 } : stamped;
  }
  onResult(result) {
    // The scheduler delivers only the newest generation; the panel still refuses anything older.
    if (result.generation !== this.scheduler.generation) return;
    this.failure = null;
    this.drawStrip(result);
    this.lastResult = this.modules.state.keepResult(result);
    if (this.noticeFromRender) this.notice('');
    this.updateButtons(); this.showAction();
  }
  onFailure(error, generation) {
    if (generation !== this.scheduler.generation) return;
    this.failure = error;
    this.lastResult = null;
    this.drawStrip(null, 'Не удалось отрисовать этикетку.');
    this.showNotice(this.describe(error), true);
    this.noticeFromRender = true;
    this.updateButtons(); this.showAction();
  }
  drawStrip(result, emptyText = result?.note ?? EMPTY_STRIP_TEXT) {
    // The modes follow the options (columns, font, width): refresh them before the strip places inputs.
    this.syncFields();
    this.strip.show(result, emptyText);
    this.$('#dimensions').textContent = '';
    this.$('#ink').textContent = '';
    if (!result?.pixels) return;
    this.$('#dimensions').textContent = result.ghost ? 'Пример макета: введите свой текст' : `${this.$('#raster').width} × ${result.rows} точек`;
    if (!result.ghost && !result.partial) this.$('#ink').textContent = `Чёрного: ${Math.round(result.inkPercent * 100) / 100}%`;
  }
  get zoom() { return this.strip?.zoom ?? 1; }
  /** 1x or 2x, never in between: a fractional scale would resample the 1-bit bitmap. */
  setZoom(zoom) {
    this.strip.setZoom(zoom);
    this.$('#zoom').textContent = `${zoom}×`;
    this.$('#zoom').setAttribute('aria-pressed', String(zoom === 2));
  }
  fitZoom() {
    if (!this.zoomChosen) this.setZoom(this.modules.strip.autoZoom(this.strip.availableWidth()));
  }

  /** Every Print blocker, derived from the scheduler's results and the CURRENT `hass`. */
  evaluate() {
    const { state } = this.modules;
    const s = this.inputState();
    const known = this.inputKnown(s);
    const missing = known ? state.missingRequired(s, this.templates) : [];
    const result = this.lastResult;
    const device = this.$('#device').value;
    const settled = Boolean(result) && result.generation === this.scheduler.generation;
    const codes = state.printBlockers({
      deviceId: device,
      hasInput: known && state.hasUserInput(s),
      missing,
      textTooLong: known && state.textTooLong(s, this.templates),
      renderPending: this.scheduler.pending,
      // Before the first result nothing failed and nothing is approximate: only render_pending speaks.
      renderOk: !this.failure,
      renderExact: !settled || result.exact || result.ghost || result.empty || result.partial,
      hasInk: !settled || !result.exact || state.bitmapHasInk(result.bitmap),
      inFlight: this.pendingCall || this.jobState(device).inFlight,
    });
    return { codes, missing, template: s.template };
  }
  updateButtons() {
    if (!this.ready) return;
    const { codes, missing, template } = this.evaluate();
    const { BLOCKER_TEXT } = this.modules.state;
    const text = codes.map(code => code === 'required_blank' ? `${BLOCKER_TEXT.required_blank} ${missing.map(field => this.fieldCaption(template, field)).join(', ')}` : BLOCKER_TEXT[code]);
    this.$('#print').disabled = codes.length > 0;
    this.$('#print').title = text.join('. ');
    this.$('#diagnose').disabled = this.pendingCall || !this.$('#device').value;
    this.$('#cancel').hidden = !this.printing;
    this.$('#editor').disabled = this.printing;
    this.$('#options').disabled = this.printing;
    this.strip.setDisabled(this.printing);
    this.$('#device').disabled = this.printing;
  }

  /** The print_document-shaped document of the current exact render; null for anything else. */
  getDocument() {
    const result = this.scheduler && this.modules.state.printableResult(this.lastResult, { generation: this.scheduler.generation, pending: this.scheduler.pending });
    if (!result || result.document.kind === 'image') return null;
    const keys = result.document.kind === 'qr' ? QR_OPTION_KEYS : TEMPLATE_OPTION_KEYS;
    const { options, ...rest } = result.document;
    return { ...structuredClone(rest), options: Object.fromEntries(keys.map(key => [key, options[key]])) };
  }

  async print() {
    if (!this.ready || this.evaluate().codes.length) return;
    const result = this.modules.state.printableResult(this.lastResult, { generation: this.scheduler.generation, pending: this.scheduler.pending });
    if (!result) return;
    const epoch = this.epoch;
    this.activeDevice = this.$('#device').value;
    this.pendingCall = true; this.printing = true;
    this.jobResponse = null;
    this.updateButtons(); this.notice('Подключаюсь к принтеру…');
    try {
      const response = await this.call('print_bitmap', { device_id: this.activeDevice, rows: result.rows, bitmap_b64: this.modules.pack.bytesToBase64(result.bitmap) });
      if (epoch !== this.epoch) return;
      this.jobResponse = { deviceId: this.activeDevice, response };
      this.deviceStatus();
      this.notice('Данные отправлены. Проверьте отпечаток.');
    } catch (error) { if (epoch === this.epoch) this.notice(error.message || String(error), true); }
    finally {
      if (epoch === this.epoch) { this.pendingCall = false; this.printing = false; this.updateButtons(); await this.loadInfo(); }
    }
  }
  async cancel() {
    if (!this.printing) return;
    this.$('#cancel').disabled = true;
    try { await this.call('cancel', {device_id: this.activeDevice}); this.notice('Задание остановлено. Уже переданную часть отпечатка отменить нельзя.'); }
    catch (error) { this.notice(error.message || String(error), true); }
    finally { this.$('#cancel').disabled = false; }
  }
  async diagnose() {
    if (this.pendingCall || !this.$('#device').value) return;
    const epoch = this.epoch;
    this.pendingCall = true; this.updateButtons(); this.notice('Проверяю соединение с принтером…');
    try {
      const result = await this.call('diagnose', {device_id: this.$('#device').value});
      if (epoch !== this.epoch) return;
      this.$('#diagnostic-data').textContent = JSON.stringify(result, null, 2); this.$('#diagnostics').open = true;
      this.notice(result.supported_profile ? 'GATT-соединение установлено, характеристики найдены. Это ещё не проверка печати.' : 'Соединение установлено, но GATT-профиль отличается от ожидаемого.', !result.supported_profile);
    } catch (error) { if (epoch === this.epoch) this.notice(error.message || String(error), true); }
    finally { if (epoch === this.epoch) { this.pendingCall = false; this.updateButtons(); await this.loadInfo(); } }
  }

  /** Options the Pillow renderer cannot draw make the recipe impossible (the panel still prints the bitmap). */
  browserOnly() {
    return this.mode !== 'image' && this.modules.options.usesBrowserOnlyOptions(this.effectiveOptions());
  }
  action() {
    const payload = this.browserOnly() ? null : this.getDocument();
    return payload && {action: 'tinyprint.print_document', data: {device_id: this.$('#device').value || 'DEVICE_ID', document: payload}};
  }
  showAction() {
    if (!this.$('#automation')?.open) return;
    const action = this.action();
    this.$('#action-data').textContent = this.mode === 'image'
      ? 'Печать картинки из автоматизации использует сервис print_document с image_base64 или разрешённым локальным path; панель такой документ не строит.'
      : this.browserOnly() ? BROWSER_ONLY_NOTICE
      : (action ? JSON.stringify(action, null, 2) : 'Введите данные: действие появится, когда лента отрисована точно.');
  }
  async copyAction() {
    const action = this.action();
    if (!action) { this.notice(this.browserOnly() ? BROWSER_ONLY_NOTICE : 'Нечего копировать: действие строится только для точно отрисованного шаблона или QR.', true); return; }
    try { await navigator.clipboard.writeText(JSON.stringify(action, null, 2)); this.notice('Действие скопировано. Вставьте его в YAML-редактор автоматизации.'); }
    catch (error) { this.notice('Буфер обмена недоступен в этом браузере. Текст действия можно выделить вручную.', true); }
  }
}
if (!customElements.get('tinyprint-panel')) customElements.define('tinyprint-panel', TinyPrintPanel);
