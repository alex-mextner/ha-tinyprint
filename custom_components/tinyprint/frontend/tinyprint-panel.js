/* Native HA web component. No CDN, token storage, history or automatic printing. */
class TinyPrintPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.mode = 'template';
    this.templates = {};
    this.presets = {};
    this.drafts = new Map();
    this.preview = null;
    this.signature = '';
    this.busy = false;
    this.printing = false;
    this.revision = 0;
    this.imageData = null;
    this.imageLoadSequence = 0;
  }

  set hass(value) {
    this._hass = value;
    if (!this.ready && this.isConnected) this.mount();
    const menu = this.shadowRoot.querySelector('ha-menu-button');
    if (menu) menu.hass = value;
    if (this.ready && !this.loaded && !this.loading && !this.loadAttempted) this.loadInfo();
  }
  get hass() { return this._hass; }
  connectedCallback() { if (!this.ready) this.mount(); }
  disconnectedCallback() { this.revision += 1; this.imageLoadSequence += 1; }
  $(selector) { return this.shadowRoot.querySelector(selector); }

  mount() {
    this.ready = true;
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
        .layout { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(310px,.85fr); align-items:start; gap:24px; }
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
        .preview-card { position:sticky; top:18px; } h2 { margin:0; font-size:18px; } .preview-head { display:flex; justify-content:space-between; align-items:center; gap:10px; }
        .canvas { background:var(--secondary-background-color,#edf0f3); border-radius:10px; min-height:300px; max-height:600px; overflow:auto; margin:18px 0 12px; padding:20px 12px; display:flex; align-items:flex-start; justify-content:center; }
        .empty { padding:54px 12px; text-align:center; max-width:270px; color:var(--secondary-text-color,#697580); font-size:14px; }
        .empty .symbol { font-size:40px; display:block; margin-bottom:12px; }
        #raster { display:block; width:100%; max-width:384px; height:auto; background:white; box-shadow:0 4px 15px #0002; image-rendering:pixelated; }
        .caption { display:flex; flex-wrap:wrap; justify-content:space-between; gap:8px; font-size:12px; color:var(--secondary-text-color,#697580); }
        #notice { padding:12px 15px; border-radius:9px; background:var(--secondary-background-color,#e9efee); margin:18px 0; white-space:pre-wrap; overflow-wrap:anywhere; }
        #notice.error { border-left:4px solid var(--error-color,#be3b3b); }
        pre { max-height:300px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font-size:11px; background:var(--secondary-background-color,#edf0f3); padding:12px; border-radius:8px; }
        [hidden] { display:none !important; }
        @media(max-width:760px) { header { padding:14px 12px; gap:10px; } .local { display:none; } main { padding:14px 12px 32px; } .layout { grid-template-columns:1fr; gap:16px; } .card { padding:17px; } .preview-card { position:static; } .devicebar { gap:8px; } .devicebar .button { font-size:12px; } .canvas { max-height:450px; } }
      </style>
      <header><ha-menu-button aria-label="Открыть меню"></ha-menu-button><div class="mark" aria-hidden="true">▤</div><div><div class="eyebrow">ЛОКАЛЬНАЯ ТЕРМОПЕЧАТЬ</div><h1>TinyPrint</h1></div><span class="local">Текст · QR · Изображения</span></header>
      <main>
        <div class="devicebar"><label>Принтер<select id="device"><option value="">Загрузка принтеров…</option></select></label><span id="device-status" class="badge">Подключение к HA</span><button id="refresh" class="button" aria-label="Обновить состояние">Обновить</button><button id="diagnose" class="button">Проверить Bluetooth</button></div>
        <div class="layout">
          <section class="card" aria-label="Редактор этикетки">
            <div class="tabs" role="tablist"><button data-mode="template" role="tab" aria-selected="true">Шаблоны</button><button data-mode="qr" role="tab" aria-selected="false">QR-код</button><button data-mode="image" role="tab" aria-selected="false">Картинка</button></div>
            <fieldset id="editor">
              <div id="template-form"><div class="field"><label for="template">Макет этикетки</label><select id="template"></select></div><div id="fields"></div></div>
              <div id="qr-form" hidden><div class="field"><label for="qr-data">Ссылка или данные QR</label><textarea id="qr-data" maxlength="500" placeholder="https://…"></textarea></div><p class="hint">Целые модули и белое поле сохраняются. Фотофильтры к QR не применяются.</p></div>
              <div id="image-form" hidden>
                <div class="field"><label for="image-file">Изображение с телефона или компьютера</label><input id="image-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"><p id="file-name" class="hint">До 2 МиБ и 4 миллионов пикселей. Файл не сохраняется на сервере.</p></div>
                <div class="field"><label for="preset">Подготовка к термопечати</label><select id="preset" data-option="preset"></select></div>
                <div class="row"><div class="field"><label for="method">Преобразование</label><select id="method" data-option="method"><option value="floyd_steinberg">Floyd–Steinberg</option><option value="threshold">Чёрное / белое</option><option value="ordered">Упорядоченный растр</option></select></div><div class="field"><label for="rotate">Поворот</label><select id="rotate" data-option="rotate" data-number><option value="0">0°</option><option value="90">90°</option><option value="180">180°</option><option value="270">270°</option></select></div></div>
                ${this.range('brightness','Яркость',.25,2,.05,1.1)}${this.range('contrast','Контраст',.25,3,.05,1.25)}${this.range('sharpness','Резкость',0,3,.1,1.2)}${this.range('threshold','Порог чёрного',1,254,1,128)}
                <div class="row"><div class="field"><label for="fit">Масштаб</label><select id="fit" data-option="fit"><option value="width">По заданной ширине</option><option value="original">Без увеличения</option></select></div><div class="field"><label for="width">Ширина, точки</label><input id="width" data-option="width" data-number type="number" min="96" max="384" value="384"></div></div>
                <label class="check"><input type="checkbox" id="invert" data-option="invert">Инвертировать изображение</label>
              </div>
              <div id="type-options"><div class="row"><div class="field"><label for="font_size">Размер текста</label><input id="font_size" data-option="font_size" data-number type="number" min="12" max="48" value="24"></div><div class="field"><label for="align">Выравнивание</label><select id="align" data-option="align"><option value="left">Слева</option><option value="center">По центру</option><option value="right">Справа</option></select></div></div><label class="check"><input type="checkbox" id="border" data-option="border">Рамка этикетки</label></div>
              <details id="qr-options"><summary>Размер и надёжность QR</summary><div class="row"><div class="field"><label for="qr_size">Размер QR, точки</label><input id="qr_size" data-option="qr_size" data-number type="number" min="96" max="360" value="320"></div><div class="field"><label for="qr_correction">Коррекция ошибок</label><select id="qr_correction" data-option="qr_correction"><option value="M">M · стандартная</option><option value="Q">Q · повышенная</option><option value="H">H · максимальная</option></select></div></div></details>
            </fieldset>
            <div class="buttons"><button id="preview" class="button">Предпросмотр</button><button id="print" class="button primary" disabled>Печатать</button><button id="cancel" class="button" hidden>Остановить</button></div>
            <p class="hint">Печать — только по кнопке. После изменения макета обновите предпросмотр.</p>
          </section>
          <section class="card preview-card" aria-label="Предпросмотр печати"><div class="preview-head"><h2>На бумаге</h2><span class="badge">384 точки</span></div><div class="canvas"><div id="empty" class="empty"><span class="symbol" aria-hidden="true">▤</span>Выберите шаблон или картинку<br>и нажмите «Предпросмотр».</div><img id="raster" alt="Готовый чёрно-белый растр для печати" hidden></div><div class="caption"><span id="dimensions">Предпросмотр не отправляет печать</span><span id="ink"></span></div><p class="hint">Это финальный растр. Контраст на настоящей бумаге зависит от принтера и ленты.</p><div id="notice" role="status" aria-live="polite" hidden></div><details id="diagnostics"><summary>Диагностика задания</summary><pre id="diagnostic-data">Проверка ещё не запускалась.</pre></details><details id="automation"><summary>Действие для автоматизации</summary><p class="hint">JSON ниже также допустим в YAML. Изображение включается в данные только при явном копировании.</p><pre id="action-data"></pre><button id="copy" class="button">Скопировать действие</button></details></section>
        </div>
      </main>`;
    this.shadowRoot.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => this.switchMode(button.dataset.mode)));
    this.$('#template').addEventListener('change', () => { this.showFields(); this.invalidate(); });
    this.$('#device').addEventListener('change', () => { this.deviceStatus(); this.invalidate(); });
    this.$('#preset').addEventListener('change', () => this.applyPreset());
    this.$('#image-file').addEventListener('change', event => this.readImage(event));
    this.$('#editor').addEventListener('input', event => {
      const control = event.target;
      if (control.dataset.field) this.drafts.get(this.$('#template').value)[control.dataset.field] = control.value;
      if (control.type === 'range') this.$(`#value-${control.id}`).textContent = control.value;
      this.invalidate();
    });
    this.$('#editor').addEventListener('change', () => this.invalidate());
    this.$('#preview').addEventListener('click', () => this.makePreview());
    this.$('#print').addEventListener('click', () => this.print());
    this.$('#cancel').addEventListener('click', () => this.cancel());
    this.$('#refresh').addEventListener('click', () => this.loadInfo());
    this.$('#diagnose').addEventListener('click', () => this.diagnose());
    this.$('#automation').addEventListener('toggle', () => this.showAction());
    this.$('#copy').addEventListener('click', () => this.copyAction());
    if (this.hass) this.loadInfo();
  }

  range(id, title, min, max, step, value) {
    return `<div class="field"><label for="${id}">${title} <small id="value-${id}">${value}</small></label><input id="${id}" data-option="${id}" data-number type="range" min="${min}" max="${max}" step="${step}" value="${value}"></div>`;
  }
  async call(service, service_data = {}, return_response = true) {
    if (!this.hass) throw new Error('Нет соединения с Home Assistant');
    const result = await this.hass.callWS({ type: 'call_service', domain: 'tinyprint', service, service_data, return_response });
    return result?.response ?? result;
  }
  notice(text, error = false) {
    const target = this.$('#notice');
    target.hidden = !text;
    target.textContent = text;
    target.classList.toggle('error', error);
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
    if (this.loading) return;
    this.loading = true; this.loadAttempted = true;
    try {
      const info = await this.call('printer_info');
      if (!this.isConnected) return;
      this.printers = info.printers;
      const selected = this.$('#device').value;
      this.fillSelect(this.$('#device'), this.printers.filter(p => p.device_id).map(p => [p.device_id, p.name]), selected);
      if (!this.$('#device').options.length) this.fillSelect(this.$('#device'), [['', 'Нет принтера — доступен предпросмотр']], '');
      if (!this.loaded) {
        this.templates = info.templates; this.presets = info.presets;
        this.fillSelect(this.$('#template'), Object.entries(this.templates).map(([key, item]) => [key, item.name]), 'note');
        this.fillSelect(this.$('#preset'), Object.entries(this.presets).map(([key, item]) => [key, item.name]), 'photo');
        this.showFields();
      }
      this.loaded = true;
      if (selected !== this.$('#device').value) this.invalidate();
      this.deviceStatus(); this.buttons();
    } catch (error) { this.notice(error.message || String(error), true); }
    finally { this.loading = false; }
  }
  deviceStatus() {
    const printer = this.printers?.find(p => p.device_id === this.$('#device').value);
    const bridge = printer?.transport?.mode === 'bridge';
    const localLE = printer?.transport?.mode === 'local_le';
    // Bridge reachability says nothing about the printer's own radio link; only diagnose proves that.
    const bridgeStates = {reachable:'Мост доступен', busy:'Мост занят заданием', auth_failed:'Ошибка доступа к мосту', unreachable:'Мост недоступен', misconfigured:'Мост настроен неверно', unchecked:'Проверка моста'};
    this.$('#device-status').textContent = !printer ? 'Офлайн-предпросмотр' : bridge ? (bridgeStates[printer.transport.bridge_state] || 'Состояние моста неизвестно') : localLE ? (printer.visible ? 'Локальный LE · принтер виден' : 'Локальный LE · нет объявления') : (printer.visible ? 'Виден по Bluetooth' : 'Не виден по Bluetooth');
    this.$('#diagnose').textContent = bridge ? 'Проверить через мост' : localLE ? 'Проверить локальный LE' : 'Проверить Bluetooth';
    // In bridge mode Bluetooth fields come from Home Assistant's own radio, not the printer's link.
    if (printer) this.$('#diagnostic-data').textContent = JSON.stringify({status: printer.status, transport: printer.transport, last_error: printer.last_error, [bridge ? 'local_rssi_raw' : 'rssi_raw']: printer.rssi_raw, ...printer.last_result}, null, 2);
  }
  showFields() {
    const key = this.$('#template').value;
    const spec = this.templates[key];
    const box = this.$('#fields'); box.replaceChildren();
    if (!spec) return;
    if (!this.drafts.has(key)) this.drafts.set(key, { ...spec.example });
    const draft = this.drafts.get(key);
    const names = {title:'Заголовок',text:key === 'checklist' ? 'Пункты — каждый с новой строки' : 'Текст',footer:'Подпись снизу',qr:'Ссылка или данные QR',location:'Расположение',material:'Материал',color:'Цвет',weight:'Остаток / масса',nozzle:'Температура сопла'};
    for (const field of spec.fields) {
      const wrap = document.createElement('div'); wrap.className = 'field';
      const label = document.createElement('label'); label.htmlFor = `field-${field}`; label.textContent = names[field] || field;
      const input = document.createElement(field === 'text' ? 'textarea' : 'input');
      input.id = `field-${field}`; input.dataset.field = field; input.maxLength = field === 'qr' ? 500 : 3000;
      input.value = draft[field] || ''; input.required = spec.required.includes(field);
      wrap.append(label, input); box.append(wrap);
    }
  }
  switchMode(mode) {
    if (this.printing) return;
    this.mode = mode;
    for (const id of ['template', 'qr', 'image']) this.$(`#${id}-form`).hidden = id !== mode;
    this.$('#type-options').hidden = mode === 'image';
    this.$('#qr-options').hidden = mode === 'image';
    this.shadowRoot.querySelectorAll('[data-mode]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.mode === mode)));
    this.invalidate();
  }
  applyPreset() {
    const preset = this.presets[this.$('#preset').value];
    if (!preset) return;
    for (const key of ['brightness', 'contrast', 'sharpness', 'method']) {
      this.$(`#${key}`).value = preset[key];
      const label = this.$(`#value-${key}`); if (label) label.textContent = String(preset[key]);
    }
    this.invalidate();
  }
  async readImage(event) {
    const file = event.target.files[0];
    const sequence = ++this.imageLoadSequence;
    this.imageData = null; this.invalidate();
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { this.notice('Изображение должно быть не больше 2 МиБ для отправки через HA. Уменьшите файл или используйте локальный путь через сервис.', true); return; }
    try {
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('Не удалось прочитать файл')); reader.readAsDataURL(file);
      });
      if (sequence !== this.imageLoadSequence || !this.isConnected) return;
      this.imageData = data;
      this.$('#file-name').textContent = `${file.name} · ${Math.ceil(file.size / 1024)} КиБ · не сохраняется на сервере`;
      this.notice('Изображение загружено. Выберите обработку и сделайте предпросмотр.');
    } catch (error) { this.notice(error.message, true); }
  }
  document() {
    const options = {};
    this.shadowRoot.querySelectorAll('[data-option]').forEach(control => {
      if (control.closest('[hidden]')) return;
      if (control.dataset.option === 'preset' && !control.value) return;
      options[control.dataset.option] = control.type === 'checkbox' ? control.checked : (control.hasAttribute('data-number') ? Number(control.value) : control.value);
    });
    if (this.mode === 'image') {
      if (!this.imageData) throw new Error('Сначала выберите изображение.');
      return { kind: 'image', image_base64: this.imageData, options };
    }
    if (this.mode === 'qr') return { kind: 'qr', text: this.$('#qr-data').value, options };
    const template = this.$('#template').value;
    if (!template) throw new Error('Шаблоны ещё не загружены.');
    return { kind: 'template', template, fields: {...this.drafts.get(template)}, options };
  }
  currentSignature() { return JSON.stringify([this.$('#device').value, this.document()]); }
  invalidate() {
    this.revision += 1; this.signature = '';
    if (this.preview) this.$('#dimensions').textContent = 'Макет изменён — обновите предпросмотр';
    this.buttons(); this.showAction();
  }
  buttons() {
    this.$('#preview').disabled = this.busy || !this.loaded;
    this.$('#print').disabled = this.busy || !this.signature || !this.$('#device').value;
    this.$('#diagnose').disabled = this.busy || !this.$('#device').value;
    this.$('#cancel').hidden = !this.printing;
    this.$('#editor').disabled = this.printing;
    this.$('#device').disabled = this.printing;
  }
  async makePreview() {
    if (this.busy) return;
    let doc, signature;
    try { doc = this.document(); signature = this.currentSignature(); }
    catch (error) { this.notice(error.message, true); return; }
    const revision = this.revision;
    this.busy = true; this.buttons(); this.notice('Готовлю финальный растр…');
    try {
      const data = {document: doc}; if (this.$('#device').value) data.device_id = this.$('#device').value;
      const result = await this.call('preview', data);
      if (!this.isConnected || revision !== this.revision) { this.notice('Макет изменился во время подготовки. Повторите предпросмотр.'); return; }
      if (typeof result.png_base64 !== 'string' || !/^[A-Za-z0-9+/=]+$/.test(result.png_base64)) throw new Error('HA вернул некорректное изображение');
      this.preview = result; this.signature = signature;
      this.$('#raster').src = `data:image/png;base64,${result.png_base64}`;
      this.$('#raster').hidden = false; this.$('#empty').hidden = true;
      this.$('#dimensions').textContent = `${result.width} × ${result.height} точек`;
      this.$('#ink').textContent = `Чёрного: ${result.ink_percent}%`;
      this.notice(this.$('#device').value ? 'Предпросмотр готов. Печать ещё не отправлена.' : 'Предпросмотр готов. Для печати добавьте принтер в HA.');
    } catch (error) { this.signature = ''; this.notice(error.message || String(error), true); }
    finally { this.busy = false; this.buttons(); }
  }
  async print() {
    if (this.busy || !this.signature || this.signature !== this.currentSignature()) return;
    this.busy = true; this.printing = true; this.activeDevice = this.$('#device').value;
    this.buttons(); this.notice('Подключаюсь и передаю задание. Автоматических повторов нет.');
    try {
      const result = await this.call('print_document', {device_id: this.activeDevice, document: this.document(), expected_sha256: this.preview.bitmap_sha256});
      this.$('#diagnostic-data').textContent = JSON.stringify(result, null, 2);
      this.notice('Данные переданы принтеру. Проверьте текст и QR на бумаге — BLE не подтверждает качество отпечатка.');
    } catch (error) { this.notice(error.message || String(error), true); }
    finally { this.busy = false; this.printing = false; this.buttons(); await this.loadInfo(); }
  }
  async cancel() {
    if (!this.printing) return;
    this.$('#cancel').disabled = true;
    try { await this.call('cancel', {device_id: this.activeDevice}); this.notice('Задание остановлено. Уже переданную часть отпечатка отменить нельзя.'); }
    catch (error) { this.notice(error.message || String(error), true); }
    finally { this.$('#cancel').disabled = false; }
  }
  async diagnose() {
    if (this.busy || !this.$('#device').value) return;
    this.busy = true; this.buttons(); this.notice('Проверяю GATT-соединение. Команды печати не отправляются.');
    try {
      const result = await this.call('diagnose', {device_id: this.$('#device').value});
      this.$('#diagnostic-data').textContent = JSON.stringify(result, null, 2); this.$('#diagnostics').open = true;
      this.notice(result.supported_profile ? 'GATT-соединение установлено, характеристики найдены. Это ещё не проверка печати.' : 'Соединение установлено, но GATT-профиль отличается от ожидаемого.', !result.supported_profile);
    } catch (error) { this.notice(error.message || String(error), true); }
    finally { this.busy = false; this.buttons(); await this.loadInfo(); }
  }
  action() { return {action: 'tinyprint.print_document', data: {device_id: this.$('#device').value || 'DEVICE_ID', document: this.document()}}; }
  showAction() {
    if (!this.$('#automation')?.open) return;
    try { this.$('#action-data').textContent = this.mode === 'image' ? 'Печать картинки использует тот же сервис с image_base64. Кнопка копирования включит весь файл; для постоянных автоматизаций также поддерживается разрешённый локальный path.' : JSON.stringify(this.action(), null, 2); }
    catch (error) { this.$('#action-data').textContent = error.message; }
  }
  async copyAction() {
    try { await navigator.clipboard.writeText(JSON.stringify(this.action(), null, 2)); this.notice('Действие скопировано. Вставьте его в YAML-редактор автоматизации.'); }
    catch (error) { this.notice('Буфер обмена недоступен в этом браузере. Текст действия можно выделить вручную.', true); }
  }
}
if (!customElements.get('tinyprint-panel')) customElements.define('tinyprint-panel', TinyPrintPanel);
