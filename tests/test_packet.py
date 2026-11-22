import json
from pathlib import Path
import tempfile
import unittest

from inspect_packet import counts


class PacketChecks(unittest.TestCase):
    def test_packet_reads_with_exact_columns(self):
        root = Path(__file__).resolve().parents[1] / "data"
        observed = counts(root)
        self.assertEqual(set(observed), {path.name for path in root.glob("*.tsv")})
        self.assertTrue(all(total > 0 for total in observed.values()))

    def test_wrong_column_count_is_not_silently_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "layout.json").write_text(json.dumps({"source.tsv": ["id", "code"]}), encoding="utf-8")
            (root / "source.tsv").write_text("id\tcode\na\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source.tsv:2"):
                counts(root)
