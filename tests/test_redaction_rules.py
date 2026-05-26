import unittest

from csdn_image_mosaic import OcrBox, find_local_mask_ids, load_terms


DEFAULT_BUSINESS_TERMS = [
    "\u8d5b\u90a6",
    "\u4e50\u571f",
    "\u7eaf\u7c73",
    "xxx\u516c\u53f8",
    "xxx\u80a1\u4efd\u6709\u9650\u516c\u53f8",
]


def box(identifier: int, text: str, x: int = 0, y: int = 0, width: int = 100, height: int = 20) -> OcrBox:
    return OcrBox(identifier, text, 0.99, (x, y, x + width, y + height))


class RedactionRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.terms = load_terms(None) + DEFAULT_BUSINESS_TERMS

    def assert_masked_ids(self, boxes: list[OcrBox], expected: list[int], terms: list[str] | None = None) -> None:
        actual = sorted(find_local_mask_ids(boxes, terms or self.terms, False))
        self.assertEqual(actual, expected)

    def test_sap_material_numbers_are_not_redacted_by_default(self) -> None:
        self.assert_masked_ids([box(1, "11206030010002")], [])
        self.assert_masked_ids([box(1, "112060300100021111")], [])
        self.assert_masked_ids([box(1, "BOM\u7ec4\u4ef6\u4e2d\u90fd\u67090\u5c42\u7684\u7269\u659911206030010002")], [])

    def test_sap_material_label_overrides_identity_like_number(self) -> None:
        self.assert_masked_ids(
            [
                box(1, "\u7269\u6599\u53f7"),
                box(2, "110101199001010011", 110, 0),
            ],
            [],
        )

    def test_security_values_next_to_sensitive_labels_are_redacted(self) -> None:
        self.assert_masked_ids(
            [
                box(1, "\u94f6\u884c\u8d26\u53f7"),
                box(2, "6222021234567890123", 110, 0),
            ],
            [2],
        )
        self.assert_masked_ids(
            [
                box(1, "\u5bc6\u7801"),
                box(2, "abc123", 110, 0),
            ],
            [2],
        )
        self.assert_masked_ids(
            [
                box(1, "\u7a0e\u53f7"),
                box(2, "91310000MA1K12345X", 110, 0),
            ],
            [2],
        )

    def test_company_terms_and_explicit_user_terms_still_win(self) -> None:
        self.assert_masked_ids([box(1, "C050\u5e7f\u4e1c\u7eaf\u7c73\u5de5\u5382")], [1])
        self.assert_masked_ids(
            [box(1, "11206030010002")],
            [1],
            self.terms + ["11206030010002"],
        )


if __name__ == "__main__":
    unittest.main()
