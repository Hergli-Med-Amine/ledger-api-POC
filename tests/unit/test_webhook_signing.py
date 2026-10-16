from ledger_api.outbox import SIGNATURE_TOLERANCE_SECONDS, sign, verify_signature

SECRET = "whsec_test"
BODY = b'{"id":"evt","type":"payment.created"}'
NOW = 1_800_000_000


def test_a_fresh_signature_over_the_exact_body_verifies() -> None:
    assert verify_signature(SECRET, sign(SECRET, NOW, BODY), BODY, NOW)


def test_tampered_body_wrong_secret_or_garbage_header_fail() -> None:
    header = sign(SECRET, NOW, BODY)

    assert not verify_signature(SECRET, header, BODY.replace(b"payment", b"refund"), NOW)
    assert not verify_signature("whsec_other", header, BODY, NOW)
    assert not verify_signature(SECRET, "garbage", BODY, NOW)
    assert not verify_signature(SECRET, "", BODY, NOW)


def test_old_signature_is_rejected_so_captured_requests_cannot_be_replayed() -> None:
    old = sign(SECRET, NOW - SIGNATURE_TOLERANCE_SECONDS - 1, BODY)

    assert not verify_signature(SECRET, old, BODY, NOW)


def test_changing_the_timestamp_breaks_the_signature() -> None:
    header = sign(SECRET, NOW - 3600, BODY)
    forged = header.replace(f"t={NOW - 3600}", f"t={NOW}")

    assert not verify_signature(SECRET, forged, BODY, NOW)
