import functools
import random
import shutil
import struct
import subprocess

MAGIC = b"\xff\xff\xff\x88"
FPS = 15
FRAME_US = 1_000_000 // FPS
AUDIO_STEP_US = 20000
AUDIO_SLICE = b"\x3c" + bytes(31)
AUD = b"\x00\x00\x00\x01\x09"
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@functools.lru_cache(maxsize=None)
def h264_units(seconds: int = 12, size: str = "1280x720") -> tuple[bytes, ...]:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={FPS}:duration={seconds}",
         "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", str(FPS * 2),
         "-b:v", "2M", "-x264-params", "aud=1", "-f", "h264", "-"],
        capture_output=True, check=True, timeout=120)
    return tuple(p[1:] for p in r.stdout.split(AUD) if p)


def frame(video: bytes, pts_us: int, slices: tuple[bytes, ...] = (AUDIO_SLICE,), pts2_us: int | None = None) -> bytes:
    pts2 = pts_us if pts2_us is None else pts2_us
    head = MAGIC + struct.pack("<BBHIqq", 0, 0, len(slices), len(video), pts_us, pts2)
    return head + struct.pack("<%dH" % len(slices), *map(len, slices)) + b"".join(slices) + video


def amr_frames(n: int, seed: int = 0) -> tuple[bytes, ...]:
    rnd = random.Random(seed)
    return tuple(b"\x3c" + rnd.randbytes(31) for _ in range(n))


@functools.lru_cache(maxsize=None)
def av_blob(seconds: int = 12) -> bytes:
    out = []
    for i, u in enumerate(h264_units(seconds)):
        a, b = i * FRAME_US // AUDIO_STEP_US, (i + 1) * FRAME_US // AUDIO_STEP_US
        out.append(frame(u, i * FRAME_US, amr_frames(b - a, i), a * AUDIO_STEP_US))
    return b"".join(out)


def trafs(buf: bytes) -> dict[int, list[tuple[int, int]]]:
    out = {}
    i = 0
    while i + 8 <= len(buf):
        n, kind = struct.unpack_from(">I4s", buf, i)
        if kind == b"moof":
            t = buf.index(b"tfhd", i)
            d = buf.index(b"tfdt", i)
            r = buf.index(b"trun", i)
            track = struct.unpack_from(">I", buf, t + 8)[0]
            out.setdefault(track, []).append((struct.unpack_from(">Q", buf, d + 8)[0],
                                              struct.unpack_from(">I", buf, r + 8)[0]))
        i += n
    return out
