from __future__ import annotations

import unittest

from tgdrive.media.ranges import (
    TELEGRAM_CHUNK_SIZE,
    ByteRange,
    MalformedRange,
    RangeNotSatisfiable,
    parse_byte_range,
    plan_telegram_chunks,
)


class ParseByteRangeTests(unittest.TestCase):
    def test_absent_header_selects_full_representation(self) -> None:
        selected = parse_byte_range(None, 10)

        self.assertEqual(selected, ByteRange(0, 9, 10, is_partial=False))
        self.assertEqual(selected.length, 10)
        self.assertIsNone(selected.content_range)

    def test_explicit_and_open_ended_ranges(self) -> None:
        explicit = parse_byte_range("bytes=2-6", 10)
        open_ended = parse_byte_range("bytes=7-", 10)

        self.assertEqual((explicit.start, explicit.end, explicit.length), (2, 6, 5))
        self.assertEqual(explicit.content_range, "bytes 2-6/10")
        self.assertEqual((open_ended.start, open_ended.end), (7, 9))
        self.assertEqual(parse_byte_range("Bytes=0-1", 10).end, 1)

    def test_end_is_clamped_to_eof(self) -> None:
        selected = parse_byte_range("bytes=7-999", 10)
        self.assertEqual((selected.start, selected.end), (7, 9))

    def test_suffix_range_and_oversized_suffix(self) -> None:
        self.assertEqual(
            parse_byte_range("bytes=-3", 10),
            ByteRange(7, 9, 10, is_partial=True),
        )
        self.assertEqual(
            parse_byte_range("bytes=-99", 10),
            ByteRange(0, 9, 10, is_partial=True),
        )

    def test_empty_file_without_range_is_representable(self) -> None:
        selected = parse_byte_range(None, 0)
        self.assertEqual((selected.start, selected.end, selected.length), (0, -1, 0))

    def test_malformed_and_multiple_ranges_are_rejected(self) -> None:
        for value in (
            "items=0-1",
            "bytes=",
            "bytes=-",
            "bytes=1-2,4-5",
            "bytes =1-2",
            "bytes=one-two",
            "bytes= 1-2",
            "",
            "   ",
        ):
            with self.subTest(value=value), self.assertRaises(MalformedRange):
                parse_byte_range(value, 10)

    def test_large_range_numerals_preserve_http_semantics_without_int_overflow(self):
        enormous = "9" * 5000
        self.assertEqual(
            parse_byte_range(f"bytes=2-{enormous}", 10),
            ByteRange(2, 9, 10, is_partial=True),
        )
        self.assertEqual(
            parse_byte_range(f"bytes=-{enormous}", 10),
            ByteRange(0, 9, 10, is_partial=True),
        )
        self.assertEqual(
            parse_byte_range("bytes=" + "0" * 5000 + "2-6", 10),
            ByteRange(2, 6, 10, is_partial=True),
        )
        with self.assertRaises(RangeNotSatisfiable):
            parse_byte_range(f"bytes={enormous}-", 10)
        with self.assertRaises(MalformedRange):
            parse_byte_range("bytes=٢-٦", 10)

    def test_unsatisfiable_ranges_include_416_header_value(self) -> None:
        for value in ("bytes=10-", "bytes=8-2", "bytes=-0"):
            with self.subTest(value=value):
                with self.assertRaises(RangeNotSatisfiable) as raised:
                    parse_byte_range(value, 10)
                self.assertEqual(raised.exception.content_range, "bytes */10")

        with self.assertRaises(RangeNotSatisfiable):
            parse_byte_range("bytes=0-0", 0)


class TelegramChunkPlanTests(unittest.TestCase):
    def test_single_chunk_plan_uses_exact_half_open_cuts(self) -> None:
        selected = ByteRange(100, 199, 1000, is_partial=True)
        plan = plan_telegram_chunks(selected)

        self.assertEqual(plan.offset, 0)
        self.assertEqual(plan.part_count, 1)
        self.assertEqual(plan.first_part_cut, 100)
        self.assertEqual(plan.last_part_cut, 200)
        self.assertEqual(plan.requested_length, 100)
        chunks = list(plan.iter_chunks())
        self.assertEqual(chunks[0].output_length, 100)

    def test_cross_chunk_plan_is_exact(self) -> None:
        chunk = TELEGRAM_CHUNK_SIZE
        selected = ByteRange(chunk - 10, chunk + 19, chunk * 3, is_partial=True)
        plan = plan_telegram_chunks(selected)
        chunks = list(plan.iter_chunks())

        self.assertEqual(plan.offset, 0)
        self.assertEqual(plan.part_count, 2)
        self.assertEqual(plan.first_part_cut, chunk - 10)
        self.assertEqual(plan.last_part_cut, 20)
        self.assertEqual(sum(part.output_length for part in chunks), 30)
        self.assertEqual([part.offset for part in chunks], [0, chunk])

    def test_aligned_multi_chunk_plan_has_no_off_by_one(self) -> None:
        chunk = TELEGRAM_CHUNK_SIZE
        selected = ByteRange(chunk, chunk * 3 - 1, chunk * 4, is_partial=True)
        plan = plan_telegram_chunks(selected)

        self.assertEqual(plan.offset, chunk)
        self.assertEqual(plan.part_count, 2)
        self.assertEqual(plan.first_part_cut, 0)
        self.assertEqual(plan.last_part_cut, chunk)
        self.assertEqual(
            sum(part.output_length for part in plan.iter_chunks()),
            chunk * 2,
        )

    def test_empty_representation_produces_no_reads(self) -> None:
        plan = plan_telegram_chunks(ByteRange(0, -1, 0, is_partial=False))
        self.assertEqual(plan.part_count, 0)
        self.assertEqual(list(plan.iter_chunks()), [])


if __name__ == "__main__":
    unittest.main()
