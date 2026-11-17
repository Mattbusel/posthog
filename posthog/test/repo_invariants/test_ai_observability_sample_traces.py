import json
import hashlib

from django.test import SimpleTestCase

from products.ai_observability.backend.sample_traces import SAMPLE_TRACES_DIR

AGENTS_HINT = "Sample trace fixtures are generated; see products/ai_observability/fixtures/sample_traces/AGENTS.md."


def canonical_sha(obj: object) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class TestSampleTraceChecksums(SimpleTestCase):
    def test_generated_files_match_checksums(self) -> None:
        checksums = json.loads((SAMPLE_TRACES_DIR / "checksums.json").read_text())
        units = {p.stem: canonical_sha(json.loads(p.read_text())) for p in (SAMPLE_TRACES_DIR / "units").glob("*.json")}
        media = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (SAMPLE_TRACES_DIR / "media").iterdir()}
        sets = canonical_sha(json.loads((SAMPLE_TRACES_DIR / "sets.json").read_text()))

        mismatched = [
            f"{kind}/{name}"
            for kind, actual, expected in (("units", units, checksums["units"]), ("media", media, checksums["media"]))
            for name in sorted(actual.keys() | expected.keys())
            if actual.get(name) != expected.get(name)
        ]
        if sets != checksums["sets"]:
            mismatched.append("sets.json")

        self.assertEqual(mismatched, [], AGENTS_HINT)
