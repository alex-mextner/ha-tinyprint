"""FrameCounter counts completed host frames from arbitrary write chunking."""

from shared_loader import load

p = load("protocol")


def chunks(data: bytes, size: int = 20):
    return [data[i : i + size] for i in range(0, len(data), size)]


def test_counts_frames_split_across_20_byte_writes():
    frames = list(p.print_frames(bytes(48 * 3), 3, feed=64))
    counter = p.FrameCounter()
    total = sum(counter.feed(chunk) for chunk in chunks(b"".join(frames)))
    assert total == len(frames)


def test_returns_count_completed_by_each_chunk():
    frame = p.frame(0xA2, bytes(48))
    counter = p.FrameCounter()
    assert counter.feed(frame[:10]) == 0
    assert counter.feed(frame[10:] + frame[:5]) == 1
    assert counter.feed(frame[5:]) == 1


def test_two_frames_in_one_chunk():
    frame = p.frame(0xA1, b"\x40\x00")
    assert p.FrameCounter().feed(frame + frame) == 2


def test_garbage_prefix_clears_buffer_then_recovers():
    counter = p.FrameCounter()
    assert counter.feed(b"garbage-bytes") == 0
    assert counter.feed(p.frame(0xA1, b"\x40\x00")) == 1


def test_oversized_length_clears_instead_of_reserving():
    counter = p.FrameCounter()
    assert counter.feed(b"\x51\x78\xa2\x00\xff\xff") == 0
    assert counter.feed(p.frame(0xA1, b"\x40\x00")) == 1


def test_buffer_stays_bounded_on_endless_partial_frames():
    counter = p.FrameCounter()
    counter.feed(b"\x51\x78\xa2\x00\x00\x02")  # promises 520 bytes
    for _ in range(100):
        counter.feed(b"\x00" * 20)
    assert len(counter._pending) <= 520
