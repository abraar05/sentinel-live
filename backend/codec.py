"""H.264 / H.265 elementary-stream helpers.

We ship raw Annex-B over a WebSocket and let the browser's WebCodecs
``VideoDecoder`` handle it. That requires knowing the codec string and
(usually) an ``avcC``/``hvcC`` description, so this module extracts the
parameter sets and measures the picture size.
"""

from __future__ import annotations

import struct

START_CODE = b"\x00\x00\x00\x01"
SHORT_START_CODE = b"\x00\x00\x01"

NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9
NAL_H265_VPS = 32
NAL_H265_SPS = 33
NAL_H265_PPS = 34


class BitReader:
    __slots__ = ("data", "pos", "bit", "last_ue_value")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.bit = 0
        self.last_ue_value = 0

    def read_bit(self) -> int:
        if self.pos >= len(self.data):
            raise ValueError("bitstream overrun")
        value = (self.data[self.pos] >> (7 - self.bit)) & 1
        self.bit += 1
        if self.bit == 8:
            self.bit = 0
            self.pos += 1
        return value

    def read_bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.read_bit()
        return value

    def skip(self, count: int) -> None:
        for _ in range(count):
            self.read_bit()

    def ue(self) -> int:
        leading = 0
        while self.read_bit() == 0:
            leading += 1
            if leading > 32:
                raise ValueError("invalid exp-golomb code")
        if leading == 0:
            self.last_ue_value = 0
            return 0
        value = (1 << leading) - 1 + self.read_bits(leading)
        self.last_ue_value = value
        return value

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // -2 if value % 2 else (value + 1) // 2

    def byte_aligned(self) -> bool:
        return self.bit == 0


def split_annexb(buf: bytes):
    """Yield ``(start_code, nalu)`` pairs for every *complete* NAL unit.

    The trailing unterminated NAL is deliberately not yielded: in a live
    stream the buffer is cut wherever the transport happened to split it, and
    that fragment must be carried into the next call rather than emitted and
    then re-emitted.
    """
    positions = []
    i = 0
    n = len(buf)
    while i + 2 < n:
        if buf[i] == 0 and buf[i + 1] == 0:
            if buf[i + 2] == 1:
                positions.append((i, 3))
                i += 3
                continue
            if buf[i + 2] == 0 and i + 3 < n and buf[i + 3] == 1:
                positions.append((i, 4))
                i += 4
                continue
        i += 1

    for index in range(len(positions) - 1):
        pos, sc_len = positions[index]
        start = pos + sc_len
        stop = positions[index + 1][0]
        while stop > start and buf[stop - 1] == 0:
            stop -= 1
        if stop > start:
            yield buf[pos:pos + sc_len], buf[start:stop]


def iter_nalus(buf: bytes):
    for _sc, nalu in split_annexb(buf):
        if nalu:
            yield nalu


def nal_type(nalu: bytes, codec: str = "h264") -> int:
    """NAL unit type. H.264 uses 5 bits; H.265 hides them behind a 2-byte header."""
    if codec == "h265":
        return (nalu[0] >> 1) & 0x3F
    return nalu[0] & 0x1F


def _second_header_byte_h265(nalu: bytes) -> int:
    return ((nalu[0] & 0x80) >> 0) | (nalu[1] if len(nalu) > 1 else 0)


class ParameterSets:
    """Parameter sets discovered in a stream, plus derived decoder config."""

    def __init__(self):
        self.sps: bytes | None = None
        self.pps: bytes | None = None
        self.vps: bytes | None = None
        self.codec = "h264"
        self.width = 0
        self.height = 0
        self.codec_string = ""
        self.description: bytes | None = None

    @property
    def ready(self) -> bool:
        if self.codec == "h265":
            return bool(self.vps and self.sps and self.pps)
        return bool(self.sps and self.pps)

    def to_dict(self) -> dict:
        return {
            "codec": self.codec,
            "codecString": self.codec_string,
            "width": self.width,
            "height": self.height,
            "hasDescription": self.description is not None,
        }


def _strip_epb(data: bytes) -> bytes:
    """Remove emulation prevention bytes (0x03 after 0x00 0x00)."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def parse_sps(sps: bytes) -> tuple[int, int, int, int]:
    """Return ``(width, height, profile_idc, level_idc)`` from an H.264 SPS."""
    rbsp = _strip_epb(sps[1:])
    r = BitReader(rbsp)
    profile_idc = r.read_bits(8)
    r.skip(8)  # constraint flags + reserved
    level_idc = r.read_bits(8)
    r.ue()  # seq_parameter_set_id

    chroma_format_idc = 1
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = r.ue()
        if chroma_format_idc == 3:
            r.skip(1)
        r.ue()  # bit_depth_luma_minus8
        r.ue()  # bit_depth_chroma_minus8
        r.skip(1)  # qpprime_y_zero_transform_bypass_flag
        if r.read_bit():  # seq_scaling_matrix_present_flag
            count = 8 if chroma_format_idc != 3 else 12
            for i in range(count):
                if r.read_bit():
                    _skip_scaling_list(r, 16 if i < 6 else 64)

    r.ue()  # log2_max_frame_num_minus4
    poc_type = r.ue()
    if poc_type == 0:
        r.ue()
    elif poc_type == 1:
        r.skip(1)
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()  # max_num_ref_frames
    r.skip(1)  # gaps_in_frame_num_value_allowed_flag
    width_mbs = r.ue() + 1
    height_map = r.ue() + 1
    frame_mbs_only = r.read_bit()
    if not frame_mbs_only:
        r.skip(1)
    r.skip(1)  # direct_8x8_inference_flag

    crop_l = crop_r = crop_t = crop_b = 0
    if r.read_bit():  # frame_cropping_flag
        crop_l, crop_r, crop_t, crop_b = r.ue(), r.ue(), r.ue(), r.ue()

    sub_w = 2 if chroma_format_idc in (1, 2) else 1
    sub_h = 2 if chroma_format_idc == 1 else 1
    width = width_mbs * 16 - (crop_l + crop_r) * sub_w
    height = (2 - frame_mbs_only) * height_map * 16 - (crop_t + crop_b) * sub_h
    return width, height, profile_idc, level_idc


def _skip_scaling_list(r: BitReader, size: int) -> None:
    last = 8
    next_scale = 8
    for _ in range(size):
        if next_scale:
            next_scale = (last + r.se() + 256) % 256
        last = next_scale if next_scale else last


def _hexcodec_string(sps: bytes) -> str:
    """'avc1.PPCCLL' hex form, as WebCodecs expects."""
    if len(sps) < 4:
        return "avc1.42E01E"
    return "avc1.{:02X}{:02X}{:02X}".format(sps[1], sps[2], sps[3])


def _hexcodec_string_hevc(vps: bytes) -> str:
    if len(vps) < 6:
        return "hvc1.1.6.L93.B0"
    # vps_id(4) | max_layers(2) | max_sub_layers(3) | temporal_id_nesting(1)
    byte = vps[1]
    general_profile_space = (byte >> 6) & 0x03
    general_tier = (byte >> 5) & 0x01
    general_profile_idc = byte & 0x1F
    compat = (vps[2] << 24) | (vps[3] << 16) | (vps[4] << 8) | vps[5]
    tier = "H" if general_tier else "L"
    constraints = "".join(
        f"{byte:02X}" for byte in struct.pack(">I", compat)
    ).rstrip("0") or "0"
    return (
        f"hvc1.{general_profile_space}.{general_profile_idc}."
        f"{tier}{constraints}.B0"
    )


def _build_avcc(sps: bytes, pps: bytes) -> bytes:
    out = bytearray()
    out.append(1)  # configurationVersion
    out.append(sps[1])  # AVCProfileIndication
    out.append(sps[2])  # profile_compatibility
    out.append(sps[3])  # AVCLevelIndication
    out.append(0xFF)  # 6 bits reserved + lengthSizeMinusOne = 3
    out.append(0xE1)  # 3 bits reserved + numOfSequenceParameterSets = 1
    out += struct.pack(">H", len(sps)) + sps
    out.append(1)
    out += struct.pack(">H", len(pps)) + pps
    return bytes(out)


def _build_hvcc(vps: bytes, sps: bytes, pps: bytes) -> bytes:
    out = bytearray()
    out.append(1)  # configurationVersion
    out += b"\x01"  # general_profile_space/tier/profile_idc (minimally valid)
    out += struct.pack(">I", 0x60000000)  # general_profile_compatibility_flags
    out += b"\x00" * 6  # constraint flags
    out.append(0x0F)  # general_level_idc 3.1
    out += b"\xFC\xFC\xFC"  # min_spatial_segmentation (reserved bits set)
    out.append(0xFC)  # parallelismType
    out.append(0xFC)  # chromaFormat 4:2:0
    out.append(0xF8)  # bitDepthLumaMinus8 = 0
    out.append(0xF8)  # bitDepthChromaMinus8 = 0
    out += struct.pack(">H", 0)  # avgFrameRate
    out.append(0x0F)  # constantFrameRate/numTemporalLayers/temporalIdNested/lengthSizeMinusOne
    out.append(3)  # numOfArrays
    for nal_type, nal in ((32, vps), (33, sps), (34, pps)):
        out.append(0x80 | nal_type)  # array_completeness + NAL type
        out += struct.pack(">H", len(nal))
        out += nal
    return bytes(out)


def extract_parameter_sets(chunk: bytes, sets: ParameterSets | None = None) -> ParameterSets:
    """Scan *chunk* for parameter sets and update *sets* in place."""
    sets = sets or ParameterSets()
    for nalu in iter_nalus(chunk):
        t = nal_type(nalu, sets.codec)
        if sets.codec == "h264":
            if t == NAL_SPS and sets.sps is None:
                sets.sps = nalu
            elif t == NAL_PPS and sets.pps is None:
                sets.pps = nalu
        else:
            if t == NAL_H265_VPS and sets.vps is None:
                sets.vps = nalu
            elif t == NAL_H265_SPS and sets.sps is None:
                sets.sps = nalu
            elif t == NAL_H265_PPS and sets.pps is None:
                sets.pps = nalu

    if sets.ready:
        try:
            if sets.codec == "h264":
                width, height, _, _ = parse_sps(sets.sps)
                sets.width, sets.height = width, height
                sets.codec_string = _hexcodec_string(sets.sps)
                sets.description = _build_avcc(sets.sps, sets.pps)
            else:
                sets.width, sets.height = 0, 0
                sets.codec_string = _hexcodec_string_hevc(sets.vps)
                sets.description = _build_hvcc(sets.vps, sets.sps, sets.pps)
        except (ValueError, IndexError, struct.error):
            pass
    return sets


def looks_like_h265(chunk: bytes) -> bool:
    """Sniff HEVC from the head of the stream.

    Only the leading NAL units are considered: parameter sets always precede
    slices, and running the 2-byte H.265 header formula across H.264 slice
    *payloads* produces convincing false positives (an H.264 slice whose first
    payload byte is 0x41 reads as nal_unit_type 32, i.e. VPS).
    """
    head = []
    for nalu in iter_nalus(chunk):
        head.append(nalu)
        if len(head) >= 8:
            break
    if not head:
        return False
    hevc_types = {((nalu[0] >> 1) & 0x3F) for nalu in head if len(nalu) > 1}
    found = hevc_types & {NAL_H265_VPS, NAL_H265_SPS, NAL_H265_PPS}
    if len(found) < 2:
        return False
    return all((nalu[0] & 0x1F) != NAL_SPS for nalu in head)


def first_mb_in_slice(nalu: bytes, codec: str = "h264") -> int:
    """first_mb_in_slice for H.264, or first_slice_segment_in_pic_flag for H.265.

    A value of 0 means this NAL opens a new picture, which is the only
    reliable access-unit boundary - frames routinely carry many slices.
    """
    if codec == "h265":
        return (nalu[1] >> 7) & 1 if len(nalu) > 1 else 0

    reader = BitReader(_strip_epb(nalu[1:]))
    try:
        reader.ue()
    except ValueError:
        return 0
    return reader.last_ue_value


def contains_keyframe(chunk: bytes, codec: str = "h264") -> bool:
    for nalu in iter_nalus(chunk):
        t = nal_type(nalu, codec)
        if t in (NAL_IDR, 19, 20, 21):
            return True
        # H.265 IRAP units live in the second header byte.
        if codec == "h265" and len(nalu) > 1 and ((nalu[1] >> 1) & 0x3F) in (16, 19, 20, 21):
            return True
    return False


def to_access_units(chunk: bytes, codec: str = "h264") -> list[bytes]:
    """Split a byte run into per-frame access units.

    Boundaries are driven by slice headers (first_mb_in_slice == 0) with
    parameter sets folded into the picture that follows them. Splitting on
    IDR alone is wrong: x264 emits ~10 slices per frame by default.
    """
    units: list[bytes] = []
    current = bytearray()
    has_vcl = False

    def flush():
        nonlocal current, has_vcl
        if current:
            units.append(bytes(current))
        current = bytearray()
        has_vcl = False

    for nalu in iter_nalus(chunk):
        t = nal_type(nalu, codec)
        is_vcl = (
            t in (1, 2, 4, 5) if codec == "h264"
            else t in (0, 1, 2, 9, 19, 20, 21, 22, 23)
        )

        if t == NAL_AUD:
            flush()
            current += START_CODE + nalu
            continue

        if not is_vcl:
            # Parameter sets / SEI belong to the picture that follows them.
            # Only close an open picture; consecutive header NALs must
            # accumulate into one pending unit, not flush each other.
            if has_vcl:
                flush()
            current += START_CODE + nalu
            continue

        if has_vcl and first_mb_in_slice(nalu, codec) == 0:
            flush()
        current += START_CODE + nalu
        has_vcl = True

    flush()
    return units