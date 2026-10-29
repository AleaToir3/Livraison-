/**
 * Parseur d'adresses UK pour étiquettes de colis : miroir exact de `backend/uk_address_parser.py`.
 *
 * Les deux implémentations sont validées contre les mêmes fixtures (`shared/address_fixtures.json`) :
 * `npm test` côté mobile, `pytest` côté backend. Toute modification doit être faite des deux côtés.
 *
 * Le parseur tourne sur le thread JS (< 1 ms pour une étiquette) : l'OCR, lui, tourne sur le NPU/GPU
 * dans le Frame Processor natif.
 */

/** Regex officielle du cahier des charges, appliquée au texte en MAJUSCULES. */
export const POSTCODE_RE = /\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b/;
const POSTCODE_RE_G = new RegExp(POSTCODE_RE.source, 'g');

const POSTCODE_AREAS = new Set(
  (
    'AB AL B BA BB BD BH BL BN BR BS BT CA CB CF CH CM CO CR CT CV CW DA DD DE DG DH DL DN DT ' +
    'DY E EC EH EN EX FK FY G GL GU HA HD HG HP HR HS HU HX IG IP IV KA KT KW KY L LA LD LE LL ' +
    'LN LS LU M ME MK ML N NE NG NN NP NR NW OL OX PA PE PH PL PO PR RG RH RM S SA SE SG SK SL ' +
    'SM SN SO SP SR SS ST SW SY TA TD TF TN TQ TR TS TW UB W WA WC WD WF WN WR WS WV YO ZE ' +
    'BF BX GY IM JE'
  ).split(' '),
);

const STREET_SUFFIXES = new Set(
  (
    'STREET ST ROAD RD AVENUE AVE LANE LN CLOSE CL DRIVE DR COURT CT WAY GARDENS GDNS TERRACE ' +
    'TER TERR PLACE PL SQUARE SQ CRESCENT CRES GROVE GR HILL PARK VIEW ROW WALK GREEN RISE ' +
    'PARADE EMBANKMENT WHARF YARD MEWS BOULEVARD BROADWAY CIRCUS COMMON CROFT DALE END FIELDS ' +
    'GATE HEIGHTS MEAD MEADOW MOUNT PATH QUAY RIDGE SIDE VALE VILLAS WOOD WOODS ORCHARD ESTATE ' +
    'CENTRE BANK BROW CHASE COTTAGES MANSIONS BUILDINGS APPROACH ARCADE BYPASS CAUSEWAY ' +
    'CLIFF CORNER COVE DOWNS GLADE GLEN HAVEN HOLLOW KNOLL MALL PARKWAY PASSAGE ' +
    'PIECE PLAZA PROMENADE REACH RIDINGS SPUR STRAND TRACK TURN VISTA'
  ).split(' '),
);
const SINGLE_WORD_STREETS = new Set(['BROADWAY', 'PARADE', 'STRAND', 'BYPASS']);
const BUILDING_KEYWORDS = new Set(
  (
    'COTTAGE HOUSE LODGE VILLA FARM MANOR BARN HALL CHAMBERS CASTLE ABBEY PRIORY RECTORY ' +
    'VICARAGE GRANGE BUNGALOW CHALET STABLES FARMHOUSE MANSION MANSIONS APARTMENTS BUILDINGS ' +
    'COURT TOWER'
  ).split(' '),
);
const AMBIGUOUS_NAME_SUFFIXES = new Set(
  'COURT MANSIONS BUILDINGS HEIGHTS VILLAS COTTAGES GARDENS TERRACE SQUARE PLACE HOUSE LODGE'.split(' '),
);
const TITLES = new Set('MR MRS MS MISS MX DR PROF REV SIR LADY LORD MASTER CAPT MAJOR'.split(' '));

const TRANSLATE: Record<string, string> = {
  '’': "'", '‘': "'", '`': "'", '´': "'", '“': '"', '”': '"',
  '–': '-', '—': '-', '‐': '-', '‑': '-', ' ': ' ', '\t': ' ',
};
const TRANSLATE_RE = /[’‘`´“”–—‐‑ \t]/g;

const WS = /\s+/g;

// --- Bruit des étiquettes ---------------------------------------------------------------------
const HARD_NOISE = new RegExp(
  [
    String.raw`\b(?:TEL|TELEPHONE|PHONE|MOB|MOBILE|CALL|CONTACT)\b[:.\s]*(?:\+?\d[\d\s()-]{6,})?`,
    String.raw`(?:\+44|0044)[\s(]*0?[\s)]*\d[\d\s-]{8,}`,
    String.raw`\b0\d{3,4}[\s-]?\d{3}[\s-]?\d{3,4}\b`,
    String.raw`\b(?:ORDER|ORD|REF|REFERENCE|INV|INVOICE|CONSIGNMENT|TRACKING|TRK|AWB|WAYBILL|BARCODE|SKU|QTY|PO)\b` +
      String.raw`\s*(?:NO|NUMBER|ID)?\.?\s*[:#]?\s*[A-Z0-9-]*\d[A-Z0-9-]*`,
    String.raw`\b[A-Z]{0,4}\d{9,}[A-Z]{0,3}\b`,
    String.raw`\b\d{1,3}(?:[.,]\d+)?\s*(?:KG|KGS|LB|LBS)\b`,
    String.raw`\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b`,
    String.raw`\b(?:ROYAL\s*MAIL|PARCELFORCE|TRACKED(?:\s*\d{1,2})?|SIGNED\s*FOR|SPECIAL\s*DELIVERY` +
      String.raw`|(?:1ST|2ND|FIRST|SECOND)\s*CLASS|NEXT\s*DAY|PARCELS?|FRAGILE|HANDLE\s+WITH\s+CARE` +
      String.raw`|THIS\s+WAY\s+UP|WEIGHT)\b`,
    String.raw`\S+@\S+`,
    String.raw`HTTPS?://\S+`,
    String.raw`WWW\.\S+`,
  ].join('|'),
  'g',
);
const CARRIER_ONLY = new RegExp(
  String.raw`^(?:(?:DPD(?:\s*LOCAL)?|EVRI|HERMES|MYHERMES|AMAZON(?:\s*(?:UK|LOGISTICS|PRIME))?|AMZL` +
    String.raw`|YODEL|DHL(?:\s*(?:EXPRESS|PARCEL))?|UPS|FEDEX|TNT|INPOST|COLLECT\+|UK|GB` +
    String.raw`|UNITED\s*KINGDOM|ENGLAND|SCOTLAND|WALES|NORTHERN\s*IRELAND)\b[\s\d.,/:\-]*)+$`,
);
const SENDER_MARK = /^(?:FROM|SENDER|RETURN(?:\s*(?:TO|ADDRESS))?|SOLD\s*BY|SHIPPED\s*BY|DISPATCHED\s*BY)\b\s*[:-]?\s*/;
// « HIP TO » / « ELIVER TO » : la première lettre est souvent cachée par un pli du film plastique.
const DELIVER_WORDS =
  String.raw`(?:S?HIP(?:PING)?|D?ELIVER(?:Y)?)\s*TO|DELIVERY\s*ADDRESS|SEND\s*TO|SHIPPING\s*ADDRESS`;
const STRONG_DELIVER_MARK = new RegExp(String.raw`^(?:${DELIVER_WORDS})\b\s*[:-]?`);
const DELIVER_MARK = new RegExp(
  String.raw`^(?:(?:${DELIVER_WORDS})\b\s*[:-]?|(?:TO|ATTN|ATTENTION|ADDRESS)\s*:)\s*`,
);

// --- Segments d'adresse -----------------------------------------------------------------------
const SUB_RE = new RegExp(
  String.raw`^(FLAT|APARTMENT|APT|UNIT|SUITE|ROOM|STUDIO|PENTHOUSE|ANNEXE|ANNEX|BASEMENT FLAT|GARDEN FLAT` +
    String.raw`|GROUND FLOOR FLAT|FIRST FLOOR FLAT|SECOND FLOOR FLAT|TOP FLOOR FLAT)\b\.?\s*(?:NO\.?\s*)?#?\s*` +
    String.raw`((?:[A-Z]\d+|\d+[A-Z]?|[A-Z](?![A-Z0-9]))(?:/\d+)?)?\s*(.*)$`,
);
const NUM = String.raw`\d{1,4}[A-Z]?(?:\s*-\s*\d{1,4}[A-Z]?)?`;
const NUM_STREET_RE = new RegExp(String.raw`^(${NUM})\s+([A-Z][A-Z0-9'\-. &]*)$`);
const NUM_ONLY_RE = new RegExp(`^${NUM}$`);
// OCR sans espaces : "23MIZZENROADCLOWNE" (le numéro suffit à la clé ; 6 lettres minimum pour ne pas toucher "3RD")
const NUM_GLUED_RE = /^(\d{1,4})([A-Z]{6,}[A-Z0-9'\-. &]*)$/;
const NO_PREFIX_RE = /^(?:NO\.?|NUMBER)\s*(?=\d)/;

export interface ParsedAddress {
  postcode: string | null; // "LS6 1AB"
  buildingNumber: string | null; // "14", "14A", "12-14"
  subBuilding: string | null; // "FLAT 2"
  buildingName: string | null; // "ROSE COTTAGE"
  street: string | null; // "ALBION AVENUE"
  locality: string | null;
  recipient: string | null;
}

const EMPTY: ParsedAddress = {
  postcode: null, buildingNumber: null, subBuilding: null, buildingName: null,
  street: null, locality: null, recipient: null,
};

// =============================================================================================
// Aides
// =============================================================================================
function stripChars(text: string, chars: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && chars.includes(text[start]!)) start++;
  while (end > start && chars.includes(text[end - 1]!)) end--;
  return text.slice(start, end);
}

function collapseNumber(text: string): string {
  return text.replace(/\s*-\s*/g, '-').replace(/ /g, '');
}

function areaOf(outward: string): string {
  const m = /^[A-Z]+/.exec(outward);
  return m ? m[0] : '';
}

export function normalizePostcode(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const text = raw.toUpperCase().replace(WS, '');
  const m = /^([A-Z]{1,2}[0-9][A-Z0-9]?)([0-9][A-Z]{2})$/.exec(text);
  if (!m || !POSTCODE_AREAS.has(areaOf(m[1]!))) return null;
  return `${m[1]} ${m[2]}`;
}

export function compactPostcode(postcode: string): string {
  return postcode.replace(/ /g, '');
}

export function normalizeBuildingToken(raw: string | null | undefined): string | null {
  if (raw === null || raw === undefined) return null;
  let text = stripChars(
    raw.normalize('NFKC').replace(TRANSLATE_RE, (c) => TRANSLATE[c] ?? c).toUpperCase().replace(WS, ' '),
    ' ,.;:',
  );
  if (!text) return null;
  text = text.replace(NO_PREFIX_RE, '');
  text = text.replace(/^(\d{1,4})\s+([A-Z])$/, '$1$2');
  if (new RegExp(`^${NUM}$`).test(text)) return collapseNumber(text);
  return text;
}

export function makeCacheKey(postcode: string, buildingId: string): string {
  const slug = stripChars(buildingId.toUpperCase().replace(/[^A-Z0-9-]+/g, '-'), '-');
  return `${compactPostcode(postcode)}:${slug}`;
}

export function buildingId(a: ParsedAddress): string | null {
  if (a.buildingNumber) return a.buildingNumber;
  if (a.buildingName) return a.buildingName;
  if (a.subBuilding && a.street) return `${a.subBuilding}@${a.street}`;
  return null;
}

/** Code postal valide + numéro / nom de bâtiment exploitable : pas besoin d'image. */
export function isFastPathValid(a: ParsedAddress): boolean {
  return a.postcode !== null && buildingId(a) !== null;
}

export function cacheKey(a: ParsedAddress): string | null {
  const id = buildingId(a);
  return a.postcode !== null && id !== null ? makeCacheKey(a.postcode, id) : null;
}

/** Libellé court pour l'écran du livreur : "FLAT 2, 27 ALBION AVENUE, LS6 1AD". */
export function displayAddress(a: ParsedAddress): string {
  const line = a.buildingNumber
    ? [a.buildingNumber, a.street].filter(Boolean).join(' ')
    : [a.buildingName, a.street].filter(Boolean).join(', ');
  return [a.subBuilding, line, a.postcode].filter(Boolean).join(', ');
}

// =============================================================================================
// Nettoyage des lignes
// =============================================================================================
function cleanLines(raw: string): string[] {
  const text = (raw || '').normalize('NFKC').replace(TRANSLATE_RE, (c) => TRANSLATE[c] ?? c).toUpperCase();
  const lines: string[] = [];
  for (const part of text.split(/[\n\r|;]+/)) {
    const cleaned = stripChars(part.replace(WS, ' '), ' ,.;:|*_-');
    if (cleaned) lines.push(cleaned);
  }
  return lines;
}

function stripNoise(lines: string[]): string[] {
  const out: string[] = [];
  for (const line of lines) {
    const cleaned = stripChars(line.replace(HARD_NOISE, ' ').replace(WS, ' '), ' ,.;:|*_-/#');
    if (cleaned !== line && !/[A-Z]/.test(cleaned)) continue; // la ligne ne contenait que du bruit
    if (!cleaned || CARRIER_ONLY.test(cleaned)) continue;
    out.push(cleaned);
  }
  return out;
}

interface PostcodeMatch {
  outward: string;
  inward: string;
  index: number;
}

function findPostcode(text: string): PostcodeMatch | null {
  for (const m of text.matchAll(POSTCODE_RE_G)) {
    if (POSTCODE_AREAS.has(areaOf(m[1]!))) return { outward: m[1]!, inward: m[2]!, index: m.index ?? 0 };
  }
  return null;
}

/**
 * Retire le bloc expéditeur ("FROM: ...") et les préfixes "DELIVER TO:". `anchored` vaut true si un
 * "SHIP TO" / "DELIVER TO" a été trouvé : tout ce qui le précède (expéditeur, service, poids) est écarté et
 * la livraison est la première adresse qui suit.
 */
function selectDeliveryLines(input: string[]): { lines: string[]; anchored: boolean } {
  let lines = input;
  let anchored = false;
  for (let idx = 0; idx < lines.length; idx++) {
    if (STRONG_DELIVER_MARK.test(lines[idx]!)) {
      lines = lines.slice(idx);
      anchored = true;
      break;
    }
  }
  const out: string[] = [];
  let inSender = false;
  for (let line of lines) {
    if (SENDER_MARK.test(line)) {
      const rest = line.replace(SENDER_MARK, '').trim();
      inSender = findPostcode(rest) === null;
      continue;
    }
    if (inSender) {
      if (DELIVER_MARK.test(line)) {
        inSender = false;
      } else {
        if (findPostcode(line)) inSender = false;
        continue;
      }
    }
    line = stripChars(line.replace(DELIVER_MARK, '').trim(), ' ,.;:-');
    if (line) out.push(line);
  }
  return { lines: out, anchored };
}

// --- Réparation des confusions d'OCR dans le code postal ------------------------------------------
const TO_DIGIT: Record<string, string> = { O: '0', Q: '0', D: '0', I: '1', L: '1', Z: '2', S: '5', B: '8', G: '6' };
const TO_LETTER: Record<string, string> = { '0': 'O', '1': 'I', '2': 'Z', '5': 'S', '8': 'B', '6': 'G' };
const OUTWARD_SHAPES = ['LD', 'LDD', 'LDL', 'LLD', 'LLDD', 'LLDL'];

function coerce(chars: string, shape: string): [string, number] | null {
  let out = '';
  let changes = 0;
  for (let i = 0; i < shape.length; i++) {
    const ch = chars[i]!;
    let fixed: string | undefined;
    if (shape[i] === 'L') fixed = /[A-Z]/.test(ch) ? ch : TO_LETTER[ch];
    else fixed = /[0-9]/.test(ch) ? ch : TO_DIGIT[ch];
    if (fixed === undefined) return null;
    if (fixed !== ch) changes++;
    out += fixed;
  }
  return [out, changes];
}

/** Corrige UNE confusion d'OCR dans un code postal presque lisible ("ALBION" ne devient jamais "AL8 1ON"). */
function repairPostcodeToken(rawToken: string): string | null {
  const token = rawToken.replace(/[^A-Z0-9]/g, '');
  if (token.length < 5 || token.length > 7 || !/[0-9]/.test(token)) return null;
  const inward = coerce(token.slice(-3), 'DLL');
  if (inward === null) return null;
  let best: [number, string] | null = null;
  for (const shape of OUTWARD_SHAPES) {
    if (shape.length !== token.length - 3) continue;
    const outward = coerce(token.slice(0, -3), shape);
    if (outward === null || !POSTCODE_AREAS.has(areaOf(outward[0]))) continue;
    const cost = outward[1] + inward[1];
    if (cost <= 1 && (best === null || cost < best[0])) best = [cost, `${outward[0]} ${inward[0]}`];
  }
  return best ? best[1] : null;
}

interface Located {
  postcode: string;
  index: number;
  prefix: string;
  start: number; // première ligne du bloc d'adresse
}

/**
 * Plusieurs codes postaux valides sans marqueur (étiquette UPS : expéditeur en haut, sans "FROM:") : on
 * retient le DERNIER, car le destinataire est écrit sous l'expéditeur, et le bloc d'adresse commence après le
 * code postal précédent. Avec un marqueur "SHIP TO" (`anchored`), c'est la première adresse qui suit.
 */
function locatePostcode(lines: string[], anchored: boolean): Located | null {
  const found: { idx: number; m: PostcodeMatch }[] = [];
  lines.forEach((line, idx) => {
    const m = findPostcode(line);
    if (m) found.push({ idx, m });
  });
  if (found.length > 0) {
    const pick = anchored ? found[0]! : found[found.length - 1]!;
    const start = anchored || found.length === 1 ? 0 : found[found.length - 2]!.idx + 1;
    return {
      postcode: `${pick.m.outward} ${pick.m.inward}`,
      index: pick.idx,
      prefix: stripChars(lines[pick.idx]!.slice(0, pick.m.index), ' ,.-'),
      start,
    };
  }
  // Aucun code postal strict : réparation OCR depuis la dernière ligne ; la ville peut suivre le code postal.
  for (let idx = lines.length - 1; idx >= 0; idx--) {
    const tokens = lines[idx]!.split(' ');
    for (let start = 0; start < tokens.length; start++) {
      for (const take of [2, 1]) {
        if (start + take > tokens.length) continue;
        const candidate = repairPostcodeToken(tokens.slice(start, start + take).join(''));
        if (candidate) {
          return { postcode: candidate, index: idx, prefix: stripChars(tokens.slice(0, start).join(' '), ' ,.-'), start: 0 };
        }
      }
    }
  }
  return null;
}

// =============================================================================================
// Classification des segments
// =============================================================================================
function lastWord(words: string[]): string {
  return words[words.length - 1]!.replace(/\.+$/, '');
}

function isStreetLike(words: string[]): boolean {
  const last = lastWord(words);
  return STREET_SUFFIXES.has(last) && (words.length >= 2 || SINGLE_WORD_STREETS.has(last));
}

function isNameLike(words: string[]): boolean {
  return BUILDING_KEYWORDS.has(lastWord(words)) || words[0] === 'THE';
}

function splitNameAndStreet(words: string[]): [string, string] | null {
  for (let i = 0; i < words.length - 1; i++) {
    if (BUILDING_KEYWORDS.has(words[i]!) && words.length - i - 1 >= 2) {
      return [words.slice(0, i + 1).join(' '), words.slice(i + 1).join(' ')];
    }
  }
  return null;
}

function isRecipient(first: string, rest: string[]): boolean {
  if (/\d/.test(first) || SUB_RE.test(first)) return false;
  const words = first.split(' ');
  const hasTitle = TITLES.has(words[0]!.replace(/\.+$/, ''));
  const hasInitial = words.length >= 2 && words[0]!.replace(/\.+$/, '').length === 1;
  const anchored = rest.some((seg) => NUM_STREET_RE.test(seg) || SUB_RE.test(seg));
  const clue = anchored || rest.some((seg) => isStreetLike(seg.split(' ')) || isNameLike(seg.split(' ')));
  if (hasTitle || hasInitial) return clue;
  if (isNameLike(words) || words.length < 2) return false;
  if (isStreetLike(words) && !anchored) return false;
  return clue;
}

function classifySegments(segs: string[]): Omit<ParsedAddress, 'postcode'> {
  let recipient: string | null = null;
  const queue = [...segs];
  if (queue.length > 0 && isRecipient(queue[0]!, queue.slice(1))) recipient = queue.shift()!;

  let sub: string | null = null;
  let number: string | null = null;
  let name: string | null = null;
  let street: string | null = null;
  let loose: string[] = [];
  let locality: string[] = [];

  for (let seg of queue) {
    seg = seg.replace(NO_PREFIX_RE, '').trim();
    const subMatch = SUB_RE.exec(seg);
    if (subMatch && sub === null) {
      sub = subMatch[2] ? `${subMatch[1]} ${subMatch[2]}`.trim() : subMatch[1]!;
      seg = stripChars((subMatch[3] ?? '').trim(), ' ,.-');
      if (!seg) continue;
    }
    const numStreet = NUM_STREET_RE.exec(seg) ?? NUM_GLUED_RE.exec(seg);
    if (
      numStreet &&
      (street === null || (number === null && name === null && AMBIGUOUS_NAME_SUFFIXES.has(lastWord(street.split(' ')))))
    ) {
      if (street !== null) name = street; // "ROSE COURT" était l'immeuble, "14 ALBION AVENUE" la vraie adresse
      number = collapseNumber(numStreet[1]!);
      street = numStreet[2]!.trim();
      continue;
    }
    if (NUM_ONLY_RE.test(seg) && number === null && street === null) {
      number = collapseNumber(seg);
      continue;
    }
    const words = seg.split(' ');
    if (street !== null) {
      if (
        number === null &&
        name === null &&
        isStreetLike(words) &&
        AMBIGUOUS_NAME_SUFFIXES.has(lastWord(street.split(' ')))
      ) {
        name = street; // "ROSE COURT" était un immeuble, la vraie rue suit
        street = seg;
      } else {
        locality.push(seg);
      }
      continue;
    }
    if (isStreetLike(words)) {
      const split = splitNameAndStreet(words);
      if (split && name === null) {
        name = split[0];
        street = split[1];
        const inner = NUM_STREET_RE.exec(street);
        if (inner && number === null) {
          number = collapseNumber(inner[1]!);
          street = inner[2]!.trim();
        }
      } else {
        street = seg;
      }
    } else if (isNameLike(words)) {
      name = seg;
    } else {
      loose.push(seg);
    }
  }

  if (street !== null) {
    if (name === null && number === null && loose.length > 0) {
      name = loose[loose.length - 1]!;
      loose = loose.slice(0, -1);
    }
    locality = [...loose, ...locality];
  } else {
    locality = [...loose, ...locality];
  }

  // Un "nom" avec chiffres ou trop long est un fragment mal découpé : mieux vaut le chemin lent.
  if (name !== null && (/\d/.test(name) || name.split(' ').length > 5)) name = null;

  return {
    recipient,
    subBuilding: sub,
    buildingNumber: number,
    buildingName: name,
    street,
    locality: locality.length > 0 ? locality[locality.length - 1]! : null,
  };
}

// =============================================================================================
// API publique
// =============================================================================================
/** Parse le texte OCR brut d'une étiquette. `postcode` vaut null si aucun code postal valide n'est trouvé. */
export function parseUkAddress(rawText: string): ParsedAddress {
  const { lines, anchored } = selectDeliveryLines(stripNoise(cleanLines(rawText)));
  const located = locatePostcode(lines, anchored);
  if (located === null) return { ...EMPTY };
  const postcode = normalizePostcode(located.postcode);
  if (postcode === null) return { ...EMPTY };
  const preLines = [...lines.slice(located.start, located.index), ...(located.prefix ? [located.prefix] : [])];
  const segments: string[] = [];
  for (const line of preLines) {
    for (const piece of line.split(',')) {
      const seg = stripChars(piece, ' ,.-');
      if (seg) segments.push(seg);
    }
  }
  return { postcode, ...classifySegments(segments) };
}

/** Construit un `ParsedAddress` depuis des champs déjà extraits (réponse du fallback vision, tests). */
export function parseStructured(
  postcode: string | null,
  building: string | null,
  subBuilding: string | null = null,
  street: string | null = null,
): ParsedAddress {
  const token = normalizeBuildingToken(building);
  let number: string | null = null;
  let name: string | null = null;
  let parsedStreet = normalizeBuildingToken(street);
  if (token) {
    const m = NUM_STREET_RE.exec(token);
    if (new RegExp(`^${NUM}$`).test(token)) {
      number = token;
    } else if (m) {
      number = collapseNumber(m[1]!);
      parsedStreet = parsedStreet ?? m[2]!.trim();
    } else {
      name = token;
    }
  }
  return {
    postcode: normalizePostcode(postcode),
    buildingNumber: number,
    subBuilding: normalizeBuildingToken(subBuilding),
    buildingName: name,
    street: parsedStreet,
    locality: null,
    recipient: null,
  };
}
