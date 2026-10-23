// Single error type of the renderer; the code is the stable, testable contract.
export class RenderError extends Error {
  /**
   * @param {'too_tall'|'blank_qr'|'qr_modules_too_small'|'image_too_large'|'image_unreadable'|'bad_document'|'qr_too_long'|'font_missing'|'font_load_failed'} code
   * @param {string} [message]
   */
  constructor(code, message = code) {
    super(message);
    this.name = 'RenderError';
    this.code = code;
  }
}
