"""Parseur d'adresses postales UK pour étiquettes de colis (Royal Mail, DPD, Evri, ...).

Rôle :
  * extraire le code postal (regex officielle UK) ;
  * extraire `building_number` (14, 14A, 12-14), `sub_building` (FLAT 2, UNIT 4),
    `building_name` (ROSE COTTAGE, THE LAURELS) et la rue ;
  * filtrer le bruit des étiquettes (transporteur, suivi, téléphone, poids, bloc expéditeur) ;
  * fabriquer la clé de cache normalisée `POSTCODE:BUILDING_ID` (ex. `LS61AB:14A`).

Le miroir TypeScript (`mobile/src/ukAddressParser.ts`) implémente le même algorithme et est
validé contre les mêmes fixtures (`shared/address_fixtures.json`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Regex officielle imposée par le cahier des charges (appliquée au texte en MAJUSCULES).
POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s*([0-9][A-Z]{2})\b")

# Zones postales (lettres initiales) existantes. Elles éliminent les faux positifs
# (références de colis, codes-barres) qui épousent la forme d'un code postal.
POSTCODE_AREAS = frozenset(
    "AB AL B BA BB BD BH BL BN BR BS BT CA CB CF CH CM CO CR CT CV CW DA DD DE DG DH DL DN DT "
    "DY E EC EH EN EX FK FY G GL GU HA HD HG HP HR HS HU HX IG IP IV KA KT KW KY L LA LD LE LL "
    "LN LS LU M ME MK ML N NE NG NN NP NR NW OL OX PA PE PH PL PO PR RG RH RM S SA SE SG SK SL "
    "SM SN SO SP SR SS ST SW SY TA TD TF TN TQ TR TS TW UB W WA WC WD WF WN WR WS WV YO ZE "
    "BF BX GY IM JE".split()
)

STREET_SUFFIXES = frozenset(
    "STREET ST ROAD RD AVENUE AVE LANE LN CLOSE CL DRIVE DR COURT CT WAY GARDENS GDNS TERRACE "
    "TER TERR PLACE PL SQUARE SQ CRESCENT CRES GROVE GR HILL PARK VIEW ROW WALK GREEN RISE "
    "PARADE EMBANKMENT WHARF YARD MEWS BOULEVARD BROADWAY CIRCUS COMMON CROFT DALE END FIELDS "
    "GATE HEIGHTS MEAD MEADOW MOUNT PATH QUAY RIDGE SIDE VALE VILLAS WOOD WOODS ORCHARD ESTATE "
    "CENTRE BANK BROW CHASE COTTAGES MANSIONS BUILDINGS APPROACH ARCADE BYPASS CAUSEWAY "
    "CLIFF CORNER COVE DOWNS GLADE GLEN HAVEN HOLLOW KNOLL LODGE_ROAD MALL PARKWAY PASSAGE "
    "PIECE PLAZA PROMENADE REACH RIDINGS SPUR STRAND TRACK TURN VISTA".split()
)

SINGLE_WORD_STREETS = frozenset({"BROADWAY", "PARADE", "STRAND", "BYPASS"})

# Mots qui trahissent un nom de maison / de bâtiment.
BUILDING_KEYWORDS = frozenset(
    "COTTAGE HOUSE LODGE VILLA FARM MANOR BARN HALL CHAMBERS CASTLE ABBEY PRIORY RECTORY "
    "VICARAGE GRANGE BUNGALOW CHALET STABLES FARMHOUSE MANSION MANSIONS APARTMENTS BUILDINGS "
    "COURT TOWER".split()
)

# Un suffixe de rue qui peut aussi terminer un nom d'immeuble ("ROSE COURT").
AMBIGUOUS_NAME_SUFFIXES = frozenset(
    "COURT MANSIONS BUILDINGS HEIGHTS VILLAS COTTAGES GARDENS TERRACE SQUARE PLACE HOUSE LODGE".split()
)

TITLES = frozenset("MR MRS MS MISS MX DR PROF REV SIR LADY LORD MASTER CAPT MAJOR".split())

_WS = re.compile(r"\s+")
_TRANSLATE = str.maketrans(
    {
        "’": "'", "‘": "'", "`": "'", "´": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "‐": "-", "‑": "-", " ": " ", "\t": " ",
    }
)

# --- Bruit des étiquettes --------------------------------------------------------------
_HARD_NOISE = re.compile(
    r"""
      \b(?:TEL|TELEPHONE|PHONE|MOB|MOBILE|CALL|CONTACT)\b[:.\s]*(?:\+?\d[\d\s()-]{6,})?
    | (?:\+44|0044)[\s(]*0?[\s)]*\d[\d\s-]{8,}
    | \b0\d{3,4}[\s-]?\d{3}[\s-]?\d{3,4}\b
    | \b(?:ORDER|ORD|REF|REFERENCE|INV|INVOICE|CONSIGNMENT|TRACKING|TRK|AWB|WAYBILL|BARCODE|SKU|QTY|PO)\b
      \s*(?:NO|NUMBER|ID)?\.?\s*[:\#]?\s*[A-Z0-9-]*\d[A-Z0-9-]*
    | \b[A-Z]{0,4}\d{9,}[A-Z]{0,3}\b
    | \b\d{1,3}(?:[.,]\d+)?\s*(?:KG|KGS|LB|LBS)\b
    | \b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b
    | \b(?:ROYAL\s*MAIL|PARCELFORCE|TRACKED(?:\s*\d{1,2})?|SIGNED\s*FOR|SPECIAL\s*DELIVERY
         |(?:1ST|2ND|FIRST|SECOND)\s*CLASS|NEXT\s*DAY|PARCELS?|FRAGILE|HANDLE\s+WITH\s+CARE
         |THIS\s+WAY\s+UP|WEIGHT)\b
    | \S+@\S+
    | HTTPS?://\S+
    | WWW\.\S+
    """,
    re.VERBOSE,
)
_CARRIER_ONLY = re.compile(
    r"""^(?:(?:DPD(?:\s*LOCAL)?|EVRI|HERMES|MYHERMES|AMAZON(?:\s*(?:UK|LOGISTICS|PRIME))?|AMZL
        |YODEL|DHL(?:\s*(?:EXPRESS|PARCEL))?|UPS|FEDEX|TNT|INPOST|COLLECT\+|UK|GB
        |UNITED\s*KINGDOM|ENGLAND|SCOTLAND|WALES|NORTHERN\s*IRELAND)\b[\s\d.,/:\-]*)+$""",
    re.VERBOSE,
)
_SENDER_MARK = re.compile(
    r"^(?:FROM|SENDER|RETURN(?:\s*(?:TO|ADDRESS))?|SOLD\s*BY|SHIPPED\s*BY|DISPATCHED\s*BY)\b\s*[:\-]?\s*"
)
_DELIVER_WORDS = (
    r"(?:S?HIP(?:PING)?|D?ELIVER(?:Y)?)\s*TO|DELIVERY\s*ADDRESS|SEND\s*TO|SHIPPING\s*ADDRESS"
)  # « HIP TO » / « ELIVER TO » : la première lettre est souvent cachée par un pli du film plastique
_STRONG_DELIVER_MARK = re.compile(rf"^(?:{_DELIVER_WORDS})\b\s*[:-]?")
_DELIVER_MARK = re.compile(rf"^(?:(?:{_DELIVER_WORDS})\b\s*[:-]?|(?:TO|ATTN|ATTENTION|ADDRESS)\s*:)\s*")

# --- Segments d'adresse ---------------------------------------------------------------------
_SUB_RE = re.compile(
    r"^(FLAT|APARTMENT|APT|UNIT|SUITE|ROOM|STUDIO|PENTHOUSE|ANNEXE|ANNEX|BASEMENT FLAT|GARDEN FLAT"
    r"|GROUND FLOOR FLAT|FIRST FLOOR FLAT|SECOND FLOOR FLAT|TOP FLOOR FLAT)\b\.?\s*(?:NO\.?\s*)?#?\s*"
    r"((?:[A-Z]\d+|\d+[A-Z]?|[A-Z](?![A-Z0-9]))(?:/\d+)?)?\s*(.*)$"
)
_NUM = r"\d{1,4}[A-Z]?(?:\s*-\s*\d{1,4}[A-Z]?)?"
_NUM_STREET_RE = re.compile(rf"^({_NUM})\s+([A-Z][A-Z0-9'\-. &]*)$")
_NUM_ONLY_RE = re.compile(rf"^{_NUM}$")
# OCR sans espaces : "23MIZZENROADCLOWNE" (le numéro suffit à la clé ; 6 lettres minimum pour ne pas toucher "3RD")
_NUM_GLUED_RE = re.compile(r"^(\d{1,4})([A-Z]{6,}[A-Z0-9'\-. &]*)$")
_NO_PREFIX_RE = re.compile(r"^(?:NO\.?|NUMBER)\s*(?=\d)")


@dataclass(frozen=True, slots=True)
class ParsedAddress:
    postcode: str | None = None  # "LS6 1AB"
    building_number: str | None = None  # "14", "14A", "12-14"
    sub_building: str | None = None  # "FLAT 2"
    building_name: str | None = None  # "ROSE COTTAGE"
    street: str | None = None  # "ALBION AVENUE"
    locality: str | None = None  # dernier segment avant le code postal (ville)
    recipient: str | None = None

    @property
    def building_id(self) -> str | None:
        """Identifiant de bâtiment utilisé pour la clé de cache (niveau bâtiment, pas appartement)."""
        if self.building_number:
            return self.building_number
        if self.building_name:
            return self.building_name
        if self.sub_building and self.street:
            return f"{self.sub_building}@{self.street}"
        return None

    @property
    def is_fast_path_valid(self) -> bool:
        """Code postal valide + numéro / nom de bâtiment exploitable : pas besoin d'image."""
        return self.postcode is not None and self.building_id is not None

    @property
    def cache_key(self) -> str | None:
        bid = self.building_id
        if self.postcode is None or bid is None:
            return None
        return make_cache_key(self.postcode, bid)

    def display(self) -> str:
        """Libellé court pour l'écran du livreur : "FLAT 2, 27 ALBION AVENUE, LS6 1AD"."""
        if self.building_number:
            line = " ".join(p for p in (self.building_number, self.street) if p)
        else:
            line = ", ".join(p for p in (self.building_name, self.street) if p)
        return ", ".join(p for p in (self.sub_building, line, self.postcode) if p)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "postcode": self.postcode,
            "building_number": self.building_number,
            "sub_building": self.sub_building,
            "building_name": self.building_name,
            "street": self.street,
            "locality": self.locality,
            "recipient": self.recipient,
        }


# =============================================================================================
# Normalisation
# =============================================================================================
def _area_of(outward: str) -> str:
    m = re.match(r"[A-Z]+", outward)
    return m.group(0) if m else ""


def normalize_postcode(raw: str | None) -> str | None:
    """'ls61ab' / 'LS6  1AB' -> 'LS6 1AB'. None si invalide (forme ou zone postale inconnue)."""
    if not raw:
        return None
    text = _WS.sub("", raw.upper())
    m = re.fullmatch(r"([A-Z]{1,2}[0-9][A-Z0-9]?)([0-9][A-Z]{2})", text)
    if m is None or _area_of(m.group(1)) not in POSTCODE_AREAS:
        return None
    return f"{m.group(1)} {m.group(2)}"


def compact_postcode(postcode: str) -> str:
    return postcode.replace(" ", "")


def normalize_building_token(raw: str | None) -> str | None:
    """Normalise un numéro ('14 a' -> '14A', '12 - 14' -> '12-14') ou un nom ('the  laurels')."""
    if raw is None:
        return None
    text = _WS.sub(" ", unicodedata.normalize("NFKC", raw).translate(_TRANSLATE).upper()).strip(" ,.;:")
    if not text:
        return None
    text = _NO_PREFIX_RE.sub("", text)
    text = re.sub(r"^(\d{1,4})\s+([A-Z])$", r"\1\2", text)  # "14 A" -> "14A"
    if re.fullmatch(_NUM, text):
        return re.sub(r"\s*-\s*", "-", text).replace(" ", "")
    return text


def make_cache_key(postcode: str, building_id: str) -> str:
    slug = re.sub(r"[^A-Z0-9-]+", "-", building_id.upper()).strip("-")
    return f"{compact_postcode(postcode)}:{slug}"


# =============================================================================================
# Nettoyage des lignes
# =============================================================================================
def _clean_lines(raw: str) -> list[str]:
    text = unicodedata.normalize("NFKC", raw or "").translate(_TRANSLATE).upper()
    lines: list[str] = []
    for part in re.split(r"[\n\r|;]+", text):
        part = _WS.sub(" ", part).strip(" ,.;:|*_-")
        if part:
            lines.append(part)
    return lines


def _strip_noise(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        cleaned = _WS.sub(" ", _HARD_NOISE.sub(" ", line)).strip(" ,.;:|*_-/#")
        if cleaned != line and not re.search(r"[A-Z]", cleaned):
            continue  # la ligne ne contenait que du bruit
        if not cleaned or _CARRIER_ONLY.match(cleaned):
            continue
        out.append(cleaned)
    return out


def _find_postcode(text: str) -> re.Match[str] | None:
    for m in POSTCODE_RE.finditer(text):
        if _area_of(m.group(1)) in POSTCODE_AREAS:
            return m
    return None


def _select_delivery_lines(lines: list[str]) -> tuple[list[str], bool]:
    """Retire le bloc expéditeur ("FROM: ...") et les préfixes "DELIVER TO:".

    Retourne (lignes, ancré) : `ancré` vaut True si un "SHIP TO" / "DELIVER TO" a été trouvé ; tout ce qui le
    précède (expéditeur, service, poids) est alors écarté, et la livraison est la première adresse qui suit.
    """
    anchored = False
    for idx, line in enumerate(lines):
        if _STRONG_DELIVER_MARK.match(line):
            lines = lines[idx:]
            anchored = True
            break
    out: list[str] = []
    in_sender = False
    for line in lines:
        if _SENDER_MARK.match(line):
            rest = _SENDER_MARK.sub("", line).strip()
            in_sender = _find_postcode(rest) is None
            continue
        if in_sender:
            if _DELIVER_MARK.match(line):
                in_sender = False
            else:
                if _find_postcode(line):
                    in_sender = False
                continue
        line = _DELIVER_MARK.sub("", line).strip(" ,.;:-")
        if line:
            out.append(line)
    return out, anchored


# --- Réparation des confusions d'OCR dans le code postal --------------------------------------
_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"}
_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}
_OUTWARD_SHAPES = ("LD", "LDD", "LDL", "LLD", "LLDD", "LLDL")


def _coerce(chars: str, shape: str) -> tuple[str, int] | None:
    out: list[str] = []
    changes = 0
    for ch, kind in zip(chars, shape):
        if kind == "L":
            fixed = ch if ch.isalpha() else _TO_LETTER.get(ch)
        else:
            fixed = ch if ch.isdigit() else _TO_DIGIT.get(ch)
        if fixed is None:
            return None
        changes += fixed != ch
        out.append(fixed)
    return "".join(out), changes


def _repair_postcode_token(token: str) -> str | None:
    """Corrige UNE confusion d'OCR (1/l/I, 0/O, 5/S, 8/B...) dans un code postal presque lisible.

    Garde-fous contre les faux positifs ("ALBION" ne doit jamais devenir "AL8 1ON") : le jeton doit
    déjà contenir un vrai chiffre, et au plus un caractère peut être corrigé.
    """
    token = re.sub(r"[^A-Z0-9]", "", token)
    if not 5 <= len(token) <= 7 or not any(ch.isdigit() for ch in token):
        return None
    inward = _coerce(token[-3:], "DLL")
    if inward is None:
        return None
    best: tuple[int, str] | None = None
    for shape in _OUTWARD_SHAPES:
        if len(shape) != len(token) - 3:
            continue
        outward = _coerce(token[:-3], shape)
        if outward is None or _area_of(outward[0]) not in POSTCODE_AREAS:
            continue
        cost = outward[1] + inward[1]
        if cost <= 1 and (best is None or cost < best[0]):
            best = (cost, f"{outward[0]} {inward[0]}")
    return best[1] if best else None


def _locate_postcode(lines: list[str], anchored: bool = False) -> tuple[str, int, str, int] | None:
    """Retourne (code postal normalisé, index de ligne, texte avant le code postal, début du bloc d'adresse).

    S'il y a plusieurs codes postaux valides (étiquette UPS : expéditeur en haut, sans "FROM:"), on retient
    le DERNIER : le destinataire est écrit sous l'expéditeur. Le bloc d'adresse commence après le code
    postal précédent, pour ne pas mélanger les deux adresses.
    """
    found: list[tuple[int, re.Match[str]]] = []
    for idx, line in enumerate(lines):
        m = _find_postcode(line)
        if m:
            found.append((idx, m))
    if found and anchored:  # après "SHIP TO:" : la première adresse est celle du destinataire
        idx, m = found[0]
        return f"{m.group(1)} {m.group(2)}", idx, lines[idx][: m.start()].strip(" ,.-"), 0
    if found:
        idx, m = found[-1]
        start = found[-2][0] + 1 if len(found) > 1 else 0
        return f"{m.group(1)} {m.group(2)}", idx, lines[idx][: m.start()].strip(" ,.-"), start
    # Aucun code postal strict : tenter une réparation OCR, en partant de la dernière ligne ;
    # la ville peut suivre le code postal ("LS6 lAD LEEDS"), donc on teste toutes les fenêtres.
    for idx in range(len(lines) - 1, -1, -1):
        tokens = lines[idx].split()
        for start in range(len(tokens)):
            for take in (2, 1):
                if start + take > len(tokens):
                    continue
                candidate = _repair_postcode_token("".join(tokens[start : start + take]))
                if candidate:
                    return candidate, idx, " ".join(tokens[:start]).strip(" ,.-"), 0
    return None


# =============================================================================================
# Classification des segments
# =============================================================================================
def _is_street_like(words: list[str]) -> bool:
    last = words[-1].rstrip(".")
    if last in STREET_SUFFIXES and (len(words) >= 2 or last in SINGLE_WORD_STREETS):
        return True
    return False


def _is_name_like(words: list[str]) -> bool:
    last = words[-1].rstrip(".")
    return last in BUILDING_KEYWORDS or words[0] == "THE"


def _split_name_and_street(words: list[str]) -> tuple[str, str] | None:
    """'ROSE COTTAGE PRIMROSE CLOSE' -> ('ROSE COTTAGE', 'PRIMROSE CLOSE')."""
    for i, word in enumerate(words[:-1]):
        if word in BUILDING_KEYWORDS and len(words) - i - 1 >= 2:
            return " ".join(words[: i + 1]), " ".join(words[i + 1 :])
    return None


def _is_recipient(first: str, rest: list[str]) -> bool:
    if re.search(r"\d", first) or _SUB_RE.match(first):
        return False
    words = first.split()
    has_title = words[0].rstrip(".") in TITLES
    has_initial = len(words) >= 2 and len(words[0].rstrip(".")) == 1
    anchored = any(_NUM_STREET_RE.match(seg) or _SUB_RE.match(seg) for seg in rest)
    clue = anchored or any(_is_street_like(seg.split()) or _is_name_like(seg.split()) for seg in rest)
    if has_title or has_initial:  # "MRS F GREEN", "MS I WOOD" : le nom l'emporte sur le suffixe de rue
        return clue
    if _is_name_like(words) or len(words) < 2:
        return False
    if _is_street_like(words) and not anchored:  # "JOHN GREEN" seul n'est pas distinguable de "ROSE HILL"
        return False
    return clue


def _classify_segments(segs: list[str]) -> dict[str, str | None]:
    recipient = None
    queue = list(segs)
    if queue and _is_recipient(queue[0], queue[1:]):
        recipient = queue.pop(0)

    sub: str | None = None
    number: str | None = None
    name: str | None = None
    street: str | None = None
    loose: list[str] = []
    locality: list[str] = []

    for seg in queue:
        seg = _NO_PREFIX_RE.sub("", seg).strip()
        m = _SUB_RE.match(seg)
        if m and sub is None:
            sub = f"{m.group(1)} {m.group(2)}".strip() if m.group(2) else m.group(1)
            seg = m.group(3).strip(" ,.-")
            if not seg:
                continue
        m = _NUM_STREET_RE.match(seg) or _NUM_GLUED_RE.match(seg)
        if m and (
            street is None
            or (number is None and name is None and street.split()[-1] in AMBIGUOUS_NAME_SUFFIXES)
        ):
            if street is not None:  # "ROSE COURT" était l'immeuble, "14 ALBION AVENUE" la vraie adresse
                name = street
            number = re.sub(r"\s*-\s*", "-", m.group(1)).replace(" ", "")
            street = m.group(2).strip()
            continue
        if _NUM_ONLY_RE.match(seg) and number is None and street is None:
            number = re.sub(r"\s*-\s*", "-", seg).replace(" ", "")
            continue
        words = seg.split()
        if street is not None:
            if (
                number is None
                and name is None
                and _is_street_like(words)
                and street.split()[-1] in AMBIGUOUS_NAME_SUFFIXES
            ):
                name, street = street, seg  # "ROSE COURT" était un immeuble, la vraie rue suit
            else:
                locality.append(seg)
            continue
        if _is_street_like(words):
            split = _split_name_and_street(words)
            if split and name is None:
                name, street = split
                num_street = _NUM_STREET_RE.match(street)
                if num_street and number is None:  # "ROSE HOUSE 14 ALBION ROAD"
                    number = re.sub(r"\s*-\s*", "-", num_street.group(1)).replace(" ", "")
                    street = num_street.group(2).strip()
            else:
                street = seg
        elif _is_name_like(words):
            name = seg
        else:
            loose.append(seg)

    if street is not None:
        # Un mot isolé avant la rue ("OAKFIELD") est un nom de maison s'il n'y a pas de numéro.
        if name is None and number is None and loose:
            name = loose[-1]
            loose = loose[:-1]
        locality = loose + locality
    else:
        locality = loose + locality

    # Un "nom" avec chiffres ou trop long est un fragment mal découpé : mieux vaut passer au
    # chemin lent (vision) qu'envoyer une fausse clé au géocodeur.
    if name is not None and (re.search(r"\d", name) or len(name.split()) > 5):
        name = None

    return {
        "recipient": recipient,
        "sub_building": sub,
        "building_number": number,
        "building_name": name,
        "street": street,
        "locality": locality[-1] if locality else None,
    }


# =============================================================================================
# API publique
# =============================================================================================
def parse_uk_address(raw_text: str) -> ParsedAddress:
    """Parse le texte OCR brut d'une étiquette. Retourne un `ParsedAddress` (postcode=None si absent)."""
    lines, anchored = _select_delivery_lines(_strip_noise(_clean_lines(raw_text)))
    located = _locate_postcode(lines, anchored)
    if located is None:
        return ParsedAddress()
    postcode, idx, prefix, start = located
    normalized = normalize_postcode(postcode)
    if normalized is None:
        return ParsedAddress()
    pre_lines = lines[start:idx] + ([prefix] if prefix else [])
    segments = [s for line in pre_lines for s in (p.strip(" ,.-") for p in line.split(",")) if s]
    return ParsedAddress(postcode=normalized, **_classify_segments(segments))


def parse_structured(
    postcode: str | None,
    building: str | None,
    sub_building: str | None = None,
    street: str | None = None,
) -> ParsedAddress:
    """Construit un `ParsedAddress` depuis les champs déjà extraits par le mobile (payload WebSocket).

    `building` est soit un numéro (`14A`, `12-14`) soit un nom de maison (`ROSE COTTAGE`).
    """
    pc = normalize_postcode(postcode)
    token = normalize_building_token(building)
    number: str | None = None
    name: str | None = None
    parsed_street = normalize_building_token(street)
    if token:
        m = _NUM_STREET_RE.match(token)
        if re.fullmatch(_NUM, token):
            number = token
        elif m:  # le mobile a envoyé "14 ALBION AVENUE"
            number = re.sub(r"\s*-\s*", "-", m.group(1)).replace(" ", "")
            parsed_street = parsed_street or m.group(2).strip()
        else:
            name = token
    return ParsedAddress(
        postcode=pc,
        building_number=number,
        sub_building=normalize_building_token(sub_building),
        building_name=name,
        street=parsed_street,
    )
