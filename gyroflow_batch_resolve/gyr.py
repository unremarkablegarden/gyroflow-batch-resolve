"""Read an fpSup .GYR take and write the Gyroflow .gcsv for it.

Port of the fpSup web converter (fpSup/gyro/convert/index.html). Two layouts,
both followed by 8-byte records: int16 x, int16 y, int16 tag, int16 z, with
tag 0 = gyro and 1 = accelerometer, interleaved in the order they happened.

    v8    fpSup v1.14         20-byte header, magic 0x9F5BEB0D, clip id is the filename
    GFS7  before fpSup v1.14  64-byte header, clip id at 0x14
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MAGIC_V8 = 0x9F5BEB0D
MAGIC_GFS7 = 0x37534647   # "GFS7"
TAG_GYRO, TAG_ACCEL = 0, 1


@dataclass
class Capture:
    layout: str
    clip: str
    period_s: float
    gscale: float
    ascale: float
    orientation: str
    gyro: np.ndarray = field(repr=False)          # (n, 3) int16
    accel: np.ndarray = field(repr=False)         # (m, 3) int16
    accel_after: np.ndarray = field(repr=False)   # (m,) index of the gyro sample each reading follows

    @property
    def duration_s(self) -> float:
        return len(self.gyro) * self.period_s


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def parse(data: bytes, filename: str) -> Capture:
    if len(data) < 20:
        raise ValueError("shorter than a header")
    (magic,) = struct.unpack_from("<I", data)
    if magic == MAGIC_V8:
        period_ps, gscale, ascale = struct.unpack_from("<Iff", data, 4)
        header, layout, clip, orient = 20, "v8", filename, _cstr(data[16:20])
    elif magic == MAGIC_GFS7:
        if len(data) < 64:
            raise ValueError("shorter than a header")
        period_ps, gscale = struct.unpack_from("<If", data, 8)
        # GFS7 has no ascale field; 1/1024 is the writer's constant (MMA8452Q, 2 g range).
        ascale = 1 / 1024
        header, layout, clip, orient = 64, "GFS7", _cstr(data[20:28]) or filename, _cstr(data[16:20])
    else:
        raise ValueError(f"unknown layout, magic {magic:#010x}")
    if not period_ps:
        raise ValueError("header has no sample period")

    n = (len(data) - header) // 8
    rec = np.frombuffer(data, dtype="<i2", count=n * 4, offset=header).reshape(n, 4)
    tag = rec[:, 2]
    xyz = rec[:, [0, 1, 3]]
    is_gyro = tag == TAG_GYRO
    is_accel = tag == TAG_ACCEL
    # The number of gyro samples before each record is the index an accelerometer
    # reading follows.
    gyro_before = np.cumsum(is_gyro) - is_gyro
    return Capture(layout, clip, period_ps / 1e12, gscale, ascale, orient,
                   gyro=xyz[is_gyro].copy(), accel=xyz[is_accel].copy(),
                   accel_after=gyro_before[is_accel].astype(np.int64))


def read(path: Path) -> Capture:
    return parse(path.read_bytes(), path.stem)


def gcsv(cap: Capture, start_sample: int = 0) -> str:
    """The .gcsv for the samples from `start_sample` on, with t restarting at 0.

    A negative `start_sample` puts that many zero-rate samples first, holding
    the first accelerometer reading, so t = 0 is still the clip's first frame
    when the clip started before the log did.

    The text matches the fpSup converter's, which matches what the gcsv edition
    writes on the camera."""
    head = [
        "GYROFLOW IMU LOG", "version,1.3",
        "id,sigma_fp_v502_internal_icm20321",
        f"orientation,{cap.orientation}",
        f"note,{'fpSup stream' if cap.layout == 'v8' else 'GFS7'} {cap.clip}",
        "fwversion,SIGMA fp 5.02",
        f"videofilename,{cap.clip}",
        # t is the sample number, so tscale is the whole clock.
        f"tscale,{cap.period_s:.12g}",
        f"gscale,{cap.gscale:.12g}",
    ]
    pad = max(0, -start_sample)
    start = max(0, min(start_sample, len(cap.gyro)))
    g = np.vstack([np.zeros((pad, 3), np.int64), cap.gyro[start:].astype(np.int64)])
    t = np.arange(len(g))[:, None]
    if len(cap.accel):
        head += [f"ascale,{cap.ascale:.12g}", "t,gx,gy,gz,ax,ay,az"]
        # Hold the last accelerometer reading, seeded with the first real one.
        # The accelerometer axes differ from the gyro's and gcsv carries one
        # orientation for both, so (x, y) becomes (y, -x), as in the converter.
        idx = np.searchsorted(cap.accel_after, np.arange(start, len(cap.gyro)), side="right") - 1
        idx = np.concatenate([np.full(pad, -1), idx])
        a = cap.accel[np.clip(idx, 0, None)].astype(np.int64)
        rows = np.hstack([t, g, a[:, 1:2], -a[:, 0:1], a[:, 2:3]])
    else:
        head.append("t,gx,gy,gz")
        rows = np.hstack([t, g])
    body = "\n".join(",".join(map(str, r)) for r in rows.tolist())
    return "\n".join(head) + "\n" + body + ("\n" if body else "")
