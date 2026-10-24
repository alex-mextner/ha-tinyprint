// Python's str.strip() whitespace; JS trim() differs (it strips U+FEFF but not U+001C-1F, U+0085).
export const BLANK = /^[\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/;
