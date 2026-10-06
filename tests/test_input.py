from talli_flug.input import LineBuffer, MAX_LINE, parse_line
import pytest

SAMPLE = b"*8D440DA5F82300030049B8930905;FE3418B8;06;057A;"


def test_extended_avr():
    frame = parse_line(SAMPLE + b"\r\n", "roof")
    assert frame.raw == "8D440DA5F82300030049B8930905"
    assert frame.metadata == ("FE3418B8", "06", "057A")
    assert frame.receiver_id == "roof"
    assert frame.received_at > 0
    assert frame.received_monotonic > 0


@pytest.mark.parametrize("raw", ["5D4840D6202CC3", "8D440DA5F82300030049B8930905"])
def test_short_and_long(raw):
    frame = parse_line(f"*{raw.lower()};".encode(), "r1")
    assert frame.raw == raw
    assert frame.metadata == ()


def test_opaque_metadata():
    frame = parse_line(b"*5D4840D6202CC3;Abc;;unknown value;", "r1")
    assert frame.metadata == ("Abc", "", "unknown value")


@pytest.mark.parametrize("line", [
    b"", b"garbage", b"*123;", b"*5D4840D6202CCZ;", b"5D4840D6202CC3;",
    b"*5D4840D6202CC3", b"*5D4840D6202CC3;unterminated", b"*5D4840D6202CC3;\xff;",
    b"*5D4840D6202CC3;\x00;", b"*8D440DA5F82300;", b"*5D4840D6202CC3000000000000000;",
    b"*5D4840D6202CC3;" + b"X" * MAX_LINE + b";",
])
def test_malformed(line):
    assert parse_line(line, "r1") is None


def test_fragmented_coalesced_and_oversized_lines():
    parser = LineBuffer()
    assert parser.feed(SAMPLE[:10]) == []
    assert parser.feed(SAMPLE[10:] + b"\r") == [SAMPLE]
    assert parser.feed(b"\n" + SAMPLE + b"\n" + SAMPLE + b"\r\n") == [SAMPLE, SAMPLE]
    assert parser.feed(b"x" * (MAX_LINE + 1)) == []
    assert len(parser.buffer) == 0
    assert parser.feed(SAMPLE + b"\n" + SAMPLE + b"\n") == [SAMPLE]
