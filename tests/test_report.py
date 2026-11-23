import ast
import csv
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from report import allocate, build

AS_OF = date(2026, 11, 10)
CONTRACT_HEADER = "contract_id,contractor,amount,signed_at\n"
WORK_HEADER = "work_id,contract_id,title,price,status,completed_at,cancelled_at\n"
PAYMENT_HEADER = "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.contracts = ["C,Example,10.00,2026-01-01"]
        self.works = ["A,C,Work A,5.00,planned,,", "B,C,Work B,5.00,planned,,"]
        self.payments = []

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        (self.root / "contracts.csv").write_text(CONTRACT_HEADER + "\n".join(self.contracts) + "\n", encoding="utf-8")
        (self.root / "works.csv").write_text(WORK_HEADER + "\n".join(self.works) + "\n", encoding="utf-8")
        (self.root / "payments.csv").write_text(PAYMENT_HEADER + "\n".join(self.payments) + ("\n" if self.payments else ""), encoding="utf-8")
        return build(self.root, AS_OF)

    def payment(self, pid="P", wid="", amount="1.00", status="paid", paid="2026-11-10", refund="", contract="C", created="2026-01-01"):
        return f"{pid},{contract},{wid},Purpose,{amount},{status},{created},{paid},{refund}"

    def work_map(self, rows):
        return {r[1]: r for r in rows}


class PaymentTests(Fixture):
    def test_queued_not_counted(self):
        self.payments = [self.payment(status="queued", paid="")]
        self.assertEqual(self.write()[1][0][5], "0.00")

    def test_payment_after_cutoff_not_counted(self):
        self.payments = [self.payment(paid="2026-11-11")]
        self.assertEqual(self.write()[1][0][5], "0.00")

    def test_payment_on_cutoff_is_included(self):
        self.payments = [self.payment(amount="2.00")]
        self.assertEqual(self.write()[1][0][5], "2.00")

    def test_contract_payment_allocates_to_works(self):
        self.payments = [self.payment(amount="4.00")]
        works, _ = self.write()
        self.assertEqual(sum(Decimal(r[5]) for r in works), Decimal("4.00"))

    def test_direct_payment_is_attached_to_referenced_work(self):
        self.payments = [self.payment(wid="B", amount="2.00")]
        rows, _ = self.write()
        self.assertEqual(self.work_map(rows)["B"][5], "2.00")
        self.assertEqual(self.work_map(rows)["A"][5], "0.00")

    def test_unknown_work_id_paid_fails(self):
        self.assert_invalid_work_fails("missing", "paid", "2026-11-10")

    def test_unknown_work_id_queued_fails(self):
        self.assert_invalid_work_fails("missing", "queued", "")

    def test_unknown_work_id_missing_paid_date_fails(self):
        self.assert_invalid_work_fails("missing", "paid", "")

    def test_unknown_work_id_after_cutoff_fails(self):
        self.assert_invalid_work_fails("missing", "paid", "2026-11-11")

    def assert_invalid_work_fails(self, wid, status, paid):
        self.payments = [self.payment(wid=wid, status=status, paid=paid)]
        with self.assertRaisesRegex(ValueError, "invalid work_id"):
            self.write()

    def test_work_from_other_contract_is_rejected(self):
        self.contracts.append("OTHER,Other,1.00,2026-01-01")
        self.payments = [self.payment(wid="A", contract="OTHER")]
        with self.assertRaisesRegex(ValueError, "invalid work_id"):
            self.write()

    def test_refund_by_cutoff_reverses_payment(self):
        self.payments = [self.payment(status="refunded", amount="3.00", paid="2026-11-01", refund="2026-11-10")]
        self.assertEqual(self.write()[1][0][5], "0.00")

    def test_refund_after_cutoff_does_not_reverse_payment(self):
        self.payments = [self.payment(status="refunded", amount="3.00", paid="2026-11-01", refund="2026-11-11")]
        self.assertEqual(self.write()[1][0][5], "3.00")

    def test_refunded_payment_requires_refunded_at_even_if_not_yet_paid(self):
        self.payments = [self.payment(status="refunded", paid="2026-11-11", refund="")]
        with self.assertRaisesRegex(ValueError, "refunded_at"):
            self.write()

    def test_refunded_at_must_be_valid_date_even_after_cutoff(self):
        self.payments = [self.payment(status="refunded", paid="2026-11-11", refund="not-a-date")]
        with self.assertRaisesRegex(ValueError, "refunded_at"):
            self.write()

    def test_refunded_at_cannot_precede_paid_at(self):
        self.payments = [self.payment(status="refunded", paid="2026-11-02", refund="2026-11-01")]
        with self.assertRaisesRegex(ValueError, "precedes"):
            self.write()

    def test_invalid_amounts_are_rejected(self):
        for amount in ("", "NaN", "Infinity", "-0.01", "0.001", "abc"):
            with self.subTest(amount=amount):
                self.payments = [self.payment(amount=amount)]
                with self.assertRaisesRegex(ValueError, "amount"):
                    self.write()


class WorkAndContractTests(Fixture):
    def test_cancelled_work_receives_no_contract_allocation(self):
        self.works[1] = "B,C,Work B,5.00,cancelled,,2026-11-10"
        self.payments = [self.payment(amount="4.00")]
        rows, _ = self.write()
        self.assertEqual(self.work_map(rows)["B"][5], "0.00")

    def test_direct_payment_to_cancelled_work_is_preserved(self):
        self.works[1] = "B,C,Work B,5.00,cancelled,,2026-11-10"
        self.payments = [self.payment(wid="B", amount="2.00")]
        rows, _ = self.write()
        self.assertEqual(self.work_map(rows)["B"][5], "2.00")

    def test_status_is_reconstructed_at_report_date(self):
        self.works = ["A,C,Accepted,5.00,done,2026-11-10,", "B,C,Future,5.00,done,2026-11-11,"]
        rows, _ = self.write()
        self.assertEqual(self.work_map(rows)["A"][3], "done")
        self.assertEqual(self.work_map(rows)["B"][3], "planned")

    def test_all_work_values_in_contract_check_include_cancelled(self):
        self.works[1] = "B,C,Work B,5.00,cancelled,,2026-11-10"
        _, contracts = self.write()
        self.assertEqual(contracts[0][3], "5.00")
        self.assertEqual(contracts[0][4], "5.00")
        self.assertEqual(contracts[0][7], "0.00")

    def test_contract_without_works_is_supported(self):
        self.works = []
        _, contracts = self.write()
        self.assertEqual(contracts[0][3:5], ["0.00", "0.00"])

    def test_contract_with_all_works_cancelled_is_supported(self):
        self.works = ["A,C,Work A,5.00,cancelled,,2026-01-01"]
        self.payments = [self.payment(amount="2.00")]
        works, contracts = self.write()
        self.assertEqual(works[0][5], "0.00")
        self.assertEqual(contracts[0][5], "2.00")

    def test_malformed_dates_rejected(self):
        self.works[0] = "A,C,Work A,5.00,done,2026-02-30,"
        with self.assertRaisesRegex(ValueError, "completed_at"):
            self.write()

    def test_done_work_requires_completed_at(self):
        self.works[0] = "A,C,Work A,5.00,done,,"
        with self.assertRaisesRegex(ValueError, "missing completed_at"):
            self.write()

    def test_cancelled_work_requires_cancelled_at(self):
        self.works[0] = "A,C,Work A,5.00,cancelled,,"
        with self.assertRaisesRegex(ValueError, "missing cancelled_at"):
            self.write()


class MoneyAndInputTests(Fixture):
    def test_largest_remainder_is_deterministic_and_conserves_cents(self):
        works = [{"work_id": "b", "price_cents": 1}, {"work_id": "a", "price_cents": 1}, {"work_id": "c", "price_cents": 1}]
        expected = {"a": 1, "b": 1, "c": 0}
        for _ in range(5):
            result = allocate(2, works)
            self.assertEqual(result, expected)
            self.assertEqual(sum(result.values()), 2)

    def test_large_pool_allocation_sums_exactly(self):
        works = [{"work_id": str(i), "price_cents": i + 1} for i in range(7)]
        self.assertEqual(sum(allocate(12345, works).values()), 12345)

    def test_overpayment_is_not_capped(self):
        self.payments = [self.payment(amount="12.00")]
        works, contracts = self.write()
        self.assertEqual(contracts[0][6], "-2.00")
        self.assertEqual(sum(Decimal(row[6]) for row in works), Decimal("-2.00"))

    def test_no_float_used_for_money(self):
        source = (Path(__file__).resolve().parents[1] / "report.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(node, ast.Constant) and isinstance(node.value, float) for node in ast.walk(tree)))
        self.assertNotIn("float(", source)

    def test_duplicate_contract_id_rejected(self):
        self.contracts.append(self.contracts[0])
        with self.assertRaisesRegex(ValueError, "Duplicate/empty contract_id"):
            self.write()

    def test_duplicate_work_id_rejected(self):
        self.works.append(self.works[0])
        with self.assertRaisesRegex(ValueError, "Duplicate/empty work_id"):
            self.write()

    def test_duplicate_payment_id_rejected(self):
        self.payments = [self.payment(), self.payment()]
        with self.assertRaisesRegex(ValueError, "Duplicate/empty payment_id"):
            self.write()

    def test_unknown_payment_contract_rejected(self):
        self.payments = [self.payment(contract="MISSING")]
        with self.assertRaisesRegex(ValueError, "unknown contract"):
            self.write()

    def test_work_with_unknown_contract_rejected(self):
        self.works = ["A,MISSING,Work,1.00,planned,,"]
        with self.assertRaisesRegex(ValueError, "unknown contract"):
            self.write()

    def test_empty_required_value_rejected(self):
        self.contracts = [",Example,10.00,2026-01-01"]
        with self.assertRaisesRegex(ValueError, "contract_id"):
            self.write()

    def test_empty_contractor_and_work_title_rejected(self):
        self.contracts = ["C,,10.00,2026-01-01"]
        with self.assertRaisesRegex(ValueError, "contractor"):
            self.write()
        self.contracts = ["C,Example,10.00,2026-01-01"]
        self.works[0] = "A,C,,5.00,planned,,"
        with self.assertRaisesRegex(ValueError, "title"):
            self.write()

    def test_invalid_contract_date_rejected(self):
        self.contracts = ["C,Example,10.00,2026-02-30"]
        with self.assertRaisesRegex(ValueError, "signed_at"):
            self.write()

    def test_invalid_payment_date_rejected_even_after_cutoff(self):
        self.payments = [self.payment(paid="not-a-date")]
        with self.assertRaisesRegex(ValueError, "paid_at"):
            self.write()

    def test_short_malformed_payment_row_reports_missing_field(self):
        self.payments = ["P,C,,Purpose,1.00,paid"]
        with self.assertRaisesRegex(ValueError, "missing fields paid_at"):
            self.write()

    def test_invalid_cents_rejected_in_work_price(self):
        self.works[0] = "A,C,Work A,1.001,planned,,"
        with self.assertRaisesRegex(ValueError, "amount"):
            self.write()


class SourceDataTests(unittest.TestCase):
    def test_source_report_expected_totals_and_checks(self):
        data = Path(__file__).resolve().parents[1] / "data"
        works, contracts = build(data, AS_OF)
        by_id = {row[0]: row for row in contracts}
        self.assertEqual(by_id["D-1"][2:9], ["600000.00", "500000.00", "100000.00", "315000.00", "285000.00", "0.00", "0.00"])
        self.assertEqual(by_id["D-2"][2:9], ["90000.00", "90000.00", "0.00", "75000.00", "15000.00", "0.00", "0.00"])
        by_work = {row[1]: row for row in works}
        self.assertEqual(by_work["W-13"][3], "cancelled")
        self.assertEqual(by_work["W-23"][3], "planned")
        self.assertEqual(by_work["W-14"][5], "75000.00")
        for cid, contract in by_id.items():
            total_paid = sum(Decimal(row[5]) for row in works if row[0] == cid)
            self.assertEqual(total_paid, Decimal(contract[5]))


if __name__ == "__main__":
    unittest.main()
