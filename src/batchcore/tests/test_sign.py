"""The signing layer, checked against fixed input vectors.

Every assertion here is on a value written out in full, so a change in the
canonical form shows up as a broken signature instead of a call the service
refuses at three in the morning. No request is made.
"""

import re
from datetime import datetime

from batchcore.shared.hwc import sign

HOST = "obs.cn-north-4.myhuaweicloud.com"
SECRET = "unit-test-secret-key"
REQUEST_TIME = "20260101T000000Z"

# Written out by hand, segment by segment, exactly as the scheme documents it:
# method, canonical uri, canonical query, canonical headers, signed headers,
# hash of an empty payload.
CANONICAL = "\n".join(
    [
        "GET",
        "/inbound/transactions/",
        "marker=txn&prefix=inbound",
        "host:%s\nx-sdk-date:%s\n" % (HOST, REQUEST_TIME),
        "host;x-sdk-date",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ]
)
CANONICAL_HASH = "3a83e163f1d10402a7b3888386acf3ce1f959fd34ffaa738b72624ebf8992ba6"
SIGNATURE = "eea931c5cdd67ee4309e4af569586c25d48f864658d85a9777713b73b78ed0bd"


class FrozenMoment:
    """The clock the signing layer reads, stopped at one instant."""

    @staticmethod
    def utcnow():
        return datetime(2026, 1, 1, 0, 0, 0)


def test_percent_encoding_keeps_a_tilde_bare():
    assert sign.uri_encode("a b/c~d_e.tar.gz") == "a%20b%2Fc~d_e.tar.gz"
    assert sign.uri_encode("") == ""


def test_every_path_gets_a_trailing_slash_and_its_own_encoding():
    assert sign.canonical_uri("/inbound/transactions") == "/inbound/transactions/"
    assert sign.canonical_uri("/one two/three") == "/one%20two/three/"
    assert sign.canonical_uri("") == "/"
    # A value the caller already encoded is not encoded a second time.
    assert sign.canonical_uri("/a%20b/") == "/a%20b/"


def test_the_query_is_rendered_in_sorted_order_with_both_sides_encoded():
    pairs = [("prefix", "inbound/transactions"), ("marker", "a b"), ("empty", "")]
    assert sign.canonical_query(pairs) == "empty=&marker=a%20b&prefix=inbound%2Ftransactions"
    assert sign.canonical_query(None) == ""


def test_a_repeated_query_key_keeps_every_value_in_order():
    assert sign.canonical_query([("tag", ["c", "a", "b"])]) == "tag=a&tag=b&tag=c"


def test_a_header_with_an_underscore_in_its_name_is_not_signed():
    names = sign.signed_header_names({"Host": HOST, "My_Header": "x", "X-Sdk-Date": REQUEST_TIME})
    assert names == ["host", "x-sdk-date"]


def test_canonical_headers_are_lower_cased_trimmed_and_end_with_a_newline():
    headers = {"Host": " %s " % HOST, "X-Sdk-Date": REQUEST_TIME, "My_Header": "ignored"}
    assert sign.canonical_headers(headers, ["host", "x-sdk-date"]) == (
        "host:%s\nx-sdk-date:%s\n" % (HOST, REQUEST_TIME)
    )


def test_the_empty_payload_has_the_hash_the_scheme_documents():
    assert sign.EMPTY_PAYLOAD_HASH == sign.sha256_hex(b"")
    assert sign.sha256_hex("") == sign.EMPTY_PAYLOAD_HASH
    assert sign.sha256_hex("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_the_canonical_request_matches_the_form_written_out_by_hand():
    built = sign.canonical_request(
        "get",
        "/inbound/transactions/",
        [("prefix", "inbound"), ("marker", "txn")],
        {"Host": HOST, "X-Sdk-Date": REQUEST_TIME},
        ["host", "x-sdk-date"],
        None,
    )
    assert built == CANONICAL


def test_the_string_to_sign_names_the_algorithm_and_the_request_time():
    assert sign.string_to_sign(CANONICAL, REQUEST_TIME) == "\n".join(
        ["SDK-HMAC-SHA256", REQUEST_TIME, CANONICAL_HASH]
    )


def test_the_signature_of_the_fixed_vector_is_the_expected_digest():
    message = sign.string_to_sign(CANONICAL, REQUEST_TIME)
    assert sign.sign(SECRET, message) == SIGNATURE


def test_the_authorization_header_is_built_from_its_three_parts():
    assert sign.header_value("AKTEST", ["host", "x-sdk-date"], SIGNATURE) == (
        "SDK-HMAC-SHA256 Access=AKTEST, SignedHeaders=host;x-sdk-date, Signature=%s" % SIGNATURE
    )


def test_a_whole_request_signs_to_the_expected_vector(monkeypatch):
    monkeypatch.setattr(sign, "datetime", FrozenMoment)
    headers = sign.sign_request(
        "GET",
        HOST,
        "/inbound/transactions/",
        [("prefix", "inbound"), ("marker", "txn")],
        None,
        None,
        ("AKTEST", SECRET, None),
    )
    assert headers["Host"] == HOST
    assert headers["X-Sdk-Date"] == REQUEST_TIME
    assert headers["Authorization"] == (
        "SDK-HMAC-SHA256 Access=AKTEST, SignedHeaders=host;x-sdk-date, Signature=%s" % SIGNATURE
    )
    assert headers["Content-Length"] == "0"
    assert "X-Security-Token" not in headers


def test_a_token_issued_by_the_runtime_is_carried_and_signed(monkeypatch):
    monkeypatch.setattr(sign, "datetime", FrozenMoment)
    headers = sign.sign_request(
        "POST", HOST, "/v2/x", None, None, b'{"a": 1}', ("AK", SECRET, "TOKEN")
    )
    assert headers["X-Security-Token"] == "TOKEN"
    assert "content-type" in headers["Authorization"]
    assert "x-security-token" in headers["Authorization"]
    assert headers["Content-Length"] == "8"
    assert headers["Content-Type"] == "application/json"


def test_the_signature_follows_the_headers_that_were_actually_sent(monkeypatch):
    monkeypatch.setattr(sign, "datetime", FrozenMoment)
    headers = sign.sign_request(
        "GET", HOST, "/x", None, {"Accept": "application/json"}, None, ("AK", SECRET, "TOKEN")
    )
    signed = headers["Authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")
    assert signed == sorted(signed)
    assert "accept" in signed
    # The content length is written after signing, because the transport sets
    # it itself and a disagreement would invalidate the signature.
    assert "content-length" not in signed


def test_a_recomputed_signature_matches_the_one_the_layer_produced(monkeypatch):
    monkeypatch.setattr(sign, "datetime", FrozenMoment)
    body = b"hello"
    headers = sign.sign_request("POST", HOST, "/a/b", [("k", "v")], None, body, ("AK", SECRET, None))
    names = sign.signed_header_names(
        {k: v for k, v in headers.items() if k != "Authorization" and k != "Content-Length"}
    )
    canonical = sign.canonical_request("POST", "/a/b", [("k", "v")], headers, names, body)
    expected = sign.sign(SECRET, sign.string_to_sign(canonical, headers["X-Sdk-Date"]))
    assert headers["Authorization"].endswith(expected)


def test_the_time_used_in_the_string_to_sign_is_the_time_that_was_signed():
    # The request time is read back out of the signed header set, so the two
    # can never disagree.
    headers = sign.sign_request("GET", HOST, "/x", None, None, None, ("AK", SECRET, None))
    assert re.match(r"^\d{8}T\d{6}Z$", headers["X-Sdk-Date"])
    signed = headers["Authorization"].split("SignedHeaders=")[1].split(",")[0]
    assert "x-sdk-date" in signed
    assert headers["Authorization"].split("Signature=")[1] != ""
