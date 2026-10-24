// Job sensor state -> Russian label and in-flight check. Plan 3 adds `waiting_for_printer` here.
export const STATUS_TEXT = {
  rendering: 'Подготовка этикетки',
  connecting: 'Подключение к принтеру',
  printing: 'Печать',
};

const SETTLED = new Set(['unknown', 'unavailable', 'idle', 'sent', 'error']);
const UNKNOWN_BUSY_TEXT = 'Принтер занят';

/** `unknown` and `unavailable` are never in flight; the in-flight list comes from `printer_info`. */
export function jobInFlight(stateObject, inFlightStates) {
  return Boolean(stateObject) && inFlightStates.includes(stateObject.state);
}

/** Label for a busy state, null for a settled one; an unlisted state gets a neutral label. */
export function statusText(stateObject) {
  if (!stateObject || SETTLED.has(stateObject.state)) return null;
  return Object.hasOwn(STATUS_TEXT, stateObject.state) ? STATUS_TEXT[stateObject.state] : UNKNOWN_BUSY_TEXT;
}
