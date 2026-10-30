from pathlib import Path
import struct
import tempfile
import unittest
import warnings
import zipfile

from scripts.patch_systemui import patch_systemui, validate_dex_header, verify_apk_payload


def dex_bytes() -> bytes:
    result = bytearray(116)
    result[:8] = b"dex\n039\0"
    struct.pack_into("<III", result, 32, len(result), 112, 0x12345678)
    return bytes(result)


class PatchSystemUiTest(unittest.TestCase):
    def test_dex039_header_rejects_historical_dex041_and_size_corruption(self):
        data = dex_bytes()
        self.assertEqual(validate_dex_header(data),
                         {"version": "039", "header_size": 112, "file_size": len(data)})
        cases = {"dex041-with-112-header": (0, b"dex\n041\0"),
                 "wrong-header-size": (36, struct.pack("<I", 120)),
                 "wrong-file-size": (32, struct.pack("<I", len(data) + 1)),
                 "wrong-byte-order": (40, struct.pack("<I", 0))}
        for name, (offset, replacement) in cases.items():
            with self.subTest(name=name):
                changed = bytearray(data)
                changed[offset:offset + len(replacement)] = replacement
                with self.assertRaises(ValueError):
                    validate_dex_header(bytes(changed))
        with self.assertRaises(ValueError):
            validate_dex_header(data[:100])

    def test_payload_only_allows_target_dex_and_exact_compiled_bridge(self):
        source = {"AndroidManifest.xml": b"binary manifest", "resources.arsc": b"resource table",
                  "classes.dex": b"unchanged original dex", "classes2.dex": b"original target",
                  "META-INF/OLD.SF": b"old signature", "META-INF/MANIFEST.MF": b"old manifest"}
        payload = {name: value for name, value in source.items() if not name.startswith("META-INF/")}
        payload.update({"classes2.dex": b"patched target", "classes3.dex": b"compiled bridge"})
        with tempfile.TemporaryDirectory(prefix="systemui payload ") as temporary:
            root = Path(temporary)
            original, candidate = root / "source.apk", root / "candidate.apk"

            def write(path, entries):
                with zipfile.ZipFile(path, "w") as archive:
                    for name, value in entries.items():
                        archive.writestr(name, value)

            write(original, source)
            write(candidate, payload)
            proof = verify_apk_payload(original, candidate, "classes2.dex", "classes3.dex", b"compiled bridge")
            self.assertEqual(proof["changed_original_entries"], ["classes2.dex"])
            self.assertTrue(proof["manifest_and_resources_identical"])
            for name in ["AndroidManifest.xml", "resources.arsc", "classes.dex", "classes3.dex", "extra-payload"]:
                with self.subTest(changed=name):
                    changed = dict(payload, **{name: b"unintended change"})
                    write(candidate, changed)
                    with self.assertRaises(ValueError):
                        verify_apk_payload(original, candidate, "classes2.dex", "classes3.dex", b"compiled bridge")
            for name in ["AndroidManifest.xml", "classes3.dex"]:
                with self.subTest(missing=name):
                    changed = dict(payload)
                    del changed[name]
                    write(candidate, changed)
                    with self.assertRaises(ValueError):
                        verify_apk_payload(original, candidate, "classes2.dex", "classes3.dex", b"compiled bridge")
            write(candidate, payload)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(candidate, "a") as archive:
                    archive.writestr("AndroidManifest.xml", b"ambiguous duplicate")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                verify_apk_payload(original, candidate, "classes2.dex", "classes3.dex", b"compiled bridge")

    def test_nonempty_output_is_rejected_without_altering_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.apk"
            source.write_bytes(b"original bytes")
            output = root / "output"
            output.mkdir()
            existing = output / "caller-file"
            existing.write_text("retain me")
            with self.assertRaisesRegex(ValueError, "empty"):
                patch_systemui(source, output, root, root, [], root / "key", root / "cert", {})
            self.assertEqual(existing.read_text(), "retain me")
            self.assertEqual(source.read_bytes(), b"original bytes")


if __name__ == "__main__":
    unittest.main()
