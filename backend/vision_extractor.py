"""Fallback Vision : Gemini 2.5 Flash lit l'adresse d'une étiquette physiquement endommagée.

N'est appelé que pour ~5 % des colis (le parseur local du mobile a échoué). Le mobile n'envoie que le
ROI de l'adresse en WebP (qualité 70, < 15 Ko). La sortie est contrainte par un schéma Pydantic
(`response_schema`) puis re-normalisée par le même parseur UK que sur le mobile.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from uk_address_parser import ParsedAddress, parse_structured, parse_uk_address

log = logging.getLogger("vision_extractor")

SYSTEM_PROMPT = (
    "You read UK parcel labels (Royal Mail, DPD, Evri, Amazon UK, Yodel, DHL). "
    "Extract ONLY the DELIVERY address of the recipient; ignore the sender / return address, tracking "
    "numbers, barcodes, phone numbers, weights and carrier logos. "
    "Never invent or guess a postcode or a house number: when a character is not legible, return null "
    "for that field. Output the postcode in uppercase with a single space (e.g. LS6 1AB). "
    "building_number is digits with an optional letter or a range (14, 14A, 12-14). "
    "sub_building is a flat or unit such as 'FLAT 2' or 'UNIT 4'. building_name is a house name such as "
    "'ROSE COTTAGE'. If the image is not a parcel label or the address cannot be read, set readable=false."
)


class UKAddressExtraction(BaseModel):
    """Schéma strict renvoyé par Gemini (tous les champs sont obligatoires, nullables)."""

    readable: bool = Field(description="false si l'adresse est illisible ou si l'image n'est pas une étiquette")
    recipient_name: Optional[str] = Field(description="Nom du destinataire")
    sub_building: Optional[str] = Field(description="FLAT 2, UNIT 4, ...")
    building_number: Optional[str] = Field(description="14, 14A, 12-14")
    building_name: Optional[str] = Field(description="ROSE COTTAGE, THE LAURELS")
    street: Optional[str] = Field(description="Nom de la rue en majuscules")
    locality: Optional[str] = Field(description="Quartier / localité")
    post_town: Optional[str] = Field(description="Ville postale")
    postcode: Optional[str] = Field(description="Code postal UK, majuscules, un espace")
    confidence: float = Field(description="Confiance globale entre 0 et 1")


class VisionError(Exception):
    """kind ∈ {disabled, bad_image, timeout, upstream, unreadable}"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class VisionResult:
    parsed: ParsedAddress
    raw: UKAddressExtraction
    ms: float


def sniff_image_mime(data: bytes) -> str | None:
    """WebP (cas nominal), JPEG ou PNG, détectés par signature (le Content-Type n'est pas fiable)."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def to_parsed_address(raw: UKAddressExtraction) -> ParsedAddress:
    """Sortie Gemini -> `ParsedAddress` normalisé (mêmes règles que le parseur local)."""
    building = raw.building_number or raw.building_name
    parsed = parse_structured(raw.postcode, building, raw.sub_building, raw.street)
    if parsed.is_fast_path_valid:
        return parsed
    # Gemini a parfois rangé le code postal ou le numéro dans un autre champ : on retente en texte.
    lines = [
        raw.sub_building,
        " ".join(p for p in (raw.building_number, raw.street) if p) or None,
        raw.building_name,
        raw.locality,
        " ".join(p for p in (raw.post_town, raw.postcode) if p) or None,
    ]
    return parse_uk_address("\n".join(line for line in lines if line))


class GeminiVisionExtractor:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        timeout_s: float = 4.0,
        client: genai.Client | None = None,
    ) -> None:
        self._client = client or genai.Client(api_key=api_key)
        self._model = model
        self._timeout = timeout_s
        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=UKAddressExtraction,
            temperature=0.0,
            max_output_tokens=400,
            # Pas de "réflexion" : une lecture d'étiquette n'en a pas besoin et chaque token coûte du temps.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        )

    async def extract(self, image: bytes) -> VisionResult:
        mime = sniff_image_mime(image)
        if mime is None:
            raise VisionError("bad_image", "format d'image non reconnu (WebP, JPEG ou PNG attendu)")
        t0 = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self._model,
                    contents=[types.Part.from_bytes(data=image, mime_type=mime), "Extract the delivery address."],
                    config=self._config,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise VisionError("timeout", f"Gemini n'a pas répondu en {self._timeout:.1f} s") from exc
        except Exception as exc:  # noqa: BLE001 - erreurs SDK / réseau / quota
            log.warning("Gemini en erreur: %s", exc.__class__.__name__)
            raise VisionError("upstream", f"erreur Gemini: {exc.__class__.__name__}") from exc

        raw = response.parsed if isinstance(response.parsed, UKAddressExtraction) else None
        if raw is None:
            try:
                raw = UKAddressExtraction.model_validate_json(response.text or "")
            except ValueError as exc:
                raise VisionError("upstream", "réponse Gemini hors schéma") from exc
        if not raw.readable:
            raise VisionError("unreadable", "adresse illisible")

        parsed = to_parsed_address(raw)
        if not parsed.is_fast_path_valid:
            raise VisionError("unreadable", "code postal ou bâtiment introuvable dans la réponse")
        return VisionResult(parsed=parsed, raw=raw, ms=(time.perf_counter() - t0) * 1000.0)

