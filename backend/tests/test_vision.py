import asyncio
from types import SimpleNamespace

import pytest

from vision_extractor import (
    GeminiVisionExtractor,
    UKAddressExtraction,
    VisionError,
    sniff_image_mime,
    to_parsed_address,
)

WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32


def extraction(**kw) -> UKAddressExtraction:
    base = dict(
        readable=True, recipient_name="J SMITH", sub_building=None, building_number="14", building_name=None,
        street="ALBION AVENUE", locality=None, post_town="LEEDS", postcode="LS6 1AB", confidence=0.93,
    )
    base.update(kw)
    return UKAddressExtraction(**base)


class FakeModels:
    def __init__(self, result=None, delay=0.0, error=None):
        self.result, self.delay, self.error, self.calls = result, delay, error, []

    async def generate_content(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


def extractor(models: FakeModels, timeout_s: float = 1.0) -> GeminiVisionExtractor:
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return GeminiVisionExtractor("key", "gemini-2.5-flash", timeout_s, client=client)  # type: ignore[arg-type]


def test_sniff_mime():
    assert sniff_image_mime(WEBP) == "image/webp"
    assert sniff_image_mime(b"\xff\xd8\xff\xe0abc") == "image/jpeg"
    assert sniff_image_mime(b"\x89PNG\r\n\x1a\nxx") == "image/png"
    assert sniff_image_mime(b"GIF89a") is None


async def test_extract_success_uses_strict_schema_and_no_thinking():
    models = FakeModels(SimpleNamespace(parsed=extraction(), text=""))
    res = await extractor(models).extract(WEBP)
    assert res.parsed.cache_key == "LS61AB:14" and res.parsed.street == "ALBION AVENUE"
    model, contents, config = models.calls[0]
    assert model == "gemini-2.5-flash"
    assert config.response_schema is UKAddressExtraction
    assert config.response_mime_type == "application/json"
    assert config.thinking_config.thinking_budget == 0 and config.temperature == 0
    assert contents[0].inline_data.mime_type == "image/webp"


async def test_extract_falls_back_to_text_json_when_parsed_missing():
    models = FakeModels(SimpleNamespace(parsed=None, text=extraction(building_number=None, building_name="Rose Cottage").model_dump_json()))
    res = await extractor(models).extract(WEBP)
    assert res.parsed.cache_key == "LS61AB:ROSE-COTTAGE"


async def test_postcode_misplaced_in_post_town_is_recovered():
    raw = extraction(postcode=None, post_town="LEEDS LS6 1AB")
    assert to_parsed_address(raw).cache_key == "LS61AB:14"


@pytest.mark.parametrize(
    "models,kind",
    [
        (FakeModels(SimpleNamespace(parsed=extraction(readable=False), text="")), "unreadable"),
        (FakeModels(SimpleNamespace(parsed=extraction(postcode=None, post_town=None), text="")), "unreadable"),
        (FakeModels(SimpleNamespace(parsed=None, text="not json")), "upstream"),
        (FakeModels(error=RuntimeError("429")), "upstream"),
        (FakeModels(SimpleNamespace(parsed=extraction(), text=""), delay=0.3), "timeout"),
    ],
)
async def test_extract_errors(models, kind):
    with pytest.raises(VisionError) as err:
        await extractor(models, timeout_s=0.05).extract(WEBP)
    assert err.value.kind == kind


async def test_bad_image_never_reaches_gemini():
    models = FakeModels(SimpleNamespace(parsed=extraction(), text=""))
    with pytest.raises(VisionError) as err:
        await extractor(models).extract(b"not an image at all")
    assert err.value.kind == "bad_image" and not models.calls
