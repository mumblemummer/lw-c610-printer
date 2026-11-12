"""
Epson LW-C610 (Model 1301) Bluetooth Label Printer Protocol Driver for Windows PC.

Reverse-engineered from Epson iLabel Android SDK (com.epson.ilabel v1.8.0):
  - classes2.dex:
      * com.epson.lwprint.sdk.LWPrintProductInformation (Model "LW-C610", ModelNumber=1301,
        Resolution=360 DPI, StatusLength=78, CommandLevel=5, Halfcut="NO", LowSpeed="NO",
        Kind of Tape="1,2,3,4,5,6")
      * com.epson.lwprint.sdk.LWPrintDotbydotDraw (Pads the horizontal bitmap on BOTH left and right
        by _marginValue dots: _topFrameRect.width = imageWidth + 2 * _marginValue, draws bitmap at
        dstRect.left = _marginValue, and passes _topFrameRect.width as L and _marginValue as T)
      * com.epson.lwprint.sdk.android.androidcore.AccessManagerBluetooth2 (Bluetooth Protocol v2,
        13-byte "@BDC ST2\\r\\nP\\x00;" prefix + 78-byte status payload = 91 bytes total)
      * com.epson.lwprint.sdk.android.androidcore.PrintController (Job/Page/Raster/Feed sequences)
  - lib/x86_64/libLWPrint.so:
      * CommandFilter::makeCommand (0x2f0e0, jump table at 0x589a4, ESC { [len] [cmd] [payload...] [checksum] })
      * CommandFilter::makeJobEnvironmentCommand (0x2eff0: '@', '{', 'C', 'D', 'G')
      * CommandFilter::makePageEnvironmentCommand (0x31340: 'L', 'T' for CommandLevel 5)
      * CommandFilter::bitmapToRaster (0x31b40: MSB-first 1bpp raster packing, threshold < 0x8C)
      * CommandFilter::tapeWidthFromLWStatusTWCode (0x33680: TW byte -> TapeWidth code mapping)
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import serial
import serial.tools.list_ports
from PIL import Image


# ============================================================================
# Constants for Epson LW-C610 (Model 1301)
# ============================================================================

MODEL_NAME = "LW-C610"
MODEL_NUMBER = 1301
DPI = 360
COMMAND_LEVEL = 5

# Bluetooth Protocol v2 Status Framing (AccessManagerBluetooth2)
STATUS_PREFIX_LENGTH = 13
STATUS_PAYLOAD_LENGTH = 78
STATUS_TOTAL_LENGTH = STATUS_PREFIX_LENGTH + STATUS_PAYLOAD_LENGTH  # 91 bytes

# Fixed Control Commands
CMD_REQUEST_STATUS = bytes([0x1B, 0x7B, 0x05, 0x49, 0x00, 0x00, 0x49, 0x7D])       # ESC { 05 'I' 00 00 49 }
CMD_SET_REQUEST_STATUS = bytes([0x1B, 0x7B, 0x05, 0x49, 0x05, 0x00, 0x4E, 0x7D])   # ESC { 05 'I' 05 00 4E }
CMD_FEED_AND_CUT = bytes([0x1B, 0x7B, 0x04, 0x2B, 0x01, 0x2C, 0x7D])               # ESC { 04 '+' 01 2C }
CMD_FEED_ONLY = bytes([0x1B, 0x7B, 0x04, 0x2B, 0x00, 0x2B, 0x7D])                  # ESC { 04 '+' 00 2B }
CMD_PRINT_END = bytes([0x1B, 0x7B, 0x03, 0x40, 0x40, 0x7D])                        # ESC { 03 '@' 40 }
CMD_FORM_FEED = b"\x0c"                                                            # 0x0C

# Printable dots across tape at 360 DPI for LW-C610 (LWPrintProductInformation.getPrintableSize)
# Note: For model 1301 (LW-C610), 24mm tape (code 6) is explicitly overridden to 256 dots!
TAPE_PRINTABLE_DOTS: Dict[int, int] = {
    1: 44,   # 4 mm  ->  6 bytes/row
    2: 72,   # 6 mm  ->  9 bytes/row
    3: 108,  # 9 mm  -> 14 bytes/row
    4: 144,  # 12 mm -> 18 bytes/row
    5: 216,  # 18 mm -> 27 bytes/row
    6: 256,  # 24 mm -> 32 bytes/row (LW-C610 1301 special case)
}

TAPE_WIDTH_MM: Dict[int, int] = {
    0: 0,
    1: 4,
    2: 6,
    3: 9,
    4: 12,
    5: 18,
    6: 24,
    7: 36,
    8: 24,
    9: 36,
    12: 50,
}

TAPE_WIDTH_LABELS: Dict[int, str] = {
    0: "테이프 없음 (None)",
    1: "4 mm (44 dots)",
    2: "6 mm (72 dots)",
    3: "9 mm (108 dots)",
    4: "12 mm (144 dots)",
    5: "18 mm (216 dots)",
    6: "24 mm (256 dots)",
    7: "36 mm",
    8: "24 mm (열수축/특수)",
    9: "36 mm (특수)",
    10: "케이블 래핑",
    11: "다이컷 라벨",
    12: "50 mm",
    -1: "알 수 없음 (Unknown)",
}

# CommandFilter::tapeWidthFromLWStatusTWCode (0x33680 in libLWPrint.so)
TW_RAW_TO_TAPE_CODE: Dict[int, int] = {
    0x00: 0,   # None
    0x0B: 1,   # 4 mm
    0x5B: 1,   # 4 mm
    0x01: 2,   # 6 mm
    0x51: 2,   # 6 mm
    0x02: 3,   # 9 mm
    0x52: 3,   # 9 mm
    0x03: 4,   # 12 mm
    0x53: 4,   # 12 mm
    0x04: 5,   # 18 mm
    0x54: 5,   # 18 mm
    0x05: 6,   # 24 mm
    0x55: 6,   # 24 mm
    0x06: 7,   # 36 mm
    0x56: 7,   # 36 mm
    0x07: 12,  # 50 mm
    0x57: 12,  # 50 mm
    0x11: 8,   # 24 mm special
    0x12: 9,   # 36 mm special
    0x21: 10,  # Cable wrap
    0x23: 11,  # Die-cut
}

# PrintCoreStatus (ST byte at payload[1])
STATUS_CODE_NAMES: Dict[int, str] = {
    0x00: "대기 중 / 준비 완료 (Idle)",
    0x01: "테이프 피드 중 (Feeding)",
    0x02: "인쇄 중 (Printing)",
    0x03: "데이터 수신 중 (Data Sending)",
    0x04: "피드 완료 (Feed End)",
    0x05: "인쇄 완료 (Print End)",
}

# PrintCoreDeviceStatus (ER byte at payload[2])
ERROR_CODE_NAMES: Dict[int, str] = {
    0x00: "정상 (No Error)",
    0x01: "커터 오류 (Cutter Error)",
    0x02: "배터리 부족 / 전압 오류 (Voltage Error)",
    0x03: "고전압 오류 (High Voltage Error)",
    0x06: "테이프 카트리지 없음 (No Tape Cartridge)",
    0x07: "모터 오류 (Motor Error)",
    0x12: "파라미터 오류 (Parameter Error)",
    0x13: "프레임/체크섬 오류 (Frame/Checksum Error)",
    0x15: "프린트 헤드 과열 (Head Overheated)",
    0x17: "온도 센서 오류 (Thermistor Error)",
    0x21: "커버 열림 (Cover Open)",
    0x25: "테이프 폭 불일치 (Tape Width Error)",
    0x31: "전원 꺼짐 (Power Off)",
    0x32: "작업 취소됨 (Cancel)",
    0x33: "테이프 배출 오류 (Tape Eject Error)",
    0x37: "수신 버퍼 가득 참 (Buffer Full)",
    0x41: "리본 끝남 (Ribbon End)",
    0x42: "테이프 끝남 (Tape End)",
    0x43: "다이컷 라벨 오류 (Die-Cut Label Error)",
    0x44: "테이프 카트리지 식별 불가 (Unknown Cartridge)",
    0x45: "테이프 슬랙 오류 (Tape Slack Error)",
}


def make_command(cmd_char: str | int, payload: bytes = b"") -> bytes:
    """
    Construct an Epson LabelWorks ESC { ... } framed command:
      1B 7B [len] [cmd] [payload...] [checksum] 7D
    where:
      len = 1 (len byte) + 1 (cmd byte) + len(payload) + 1 (checksum byte) = len(payload) + 3
      checksum = (cmd + sum(payload)) & 0xFF
    """
    cmd_byte = ord(cmd_char) if isinstance(cmd_char, str) else int(cmd_char)
    length_byte = (len(payload) + 3) & 0xFF
    checksum = (cmd_byte + sum(payload)) & 0xFF
    return bytes([0x1B, 0x7B, length_byte, cmd_byte]) + payload + bytes([checksum, 0x7D])


def make_job_environment_commands(
    cut_each_label: bool = True,
    density: int = 0,
) -> bytes:
    """
    Build the exact 5-command Job Environment sequence from CommandFilter::makeJobEnvironmentCommand
    (0x2eff0 in libLWPrint.so) and CommandFilter::makeCommand (0x2f0e0):
      1. '@' (0x40 -> 0x300f5): Reset / Initialize -> 1B 7B 03 40 40 7D (6 bytes)
      2. '{' (0x7B -> 0x2f1b0): Enter Remote ST mode -> 1B 7B 07 7B 00 00 53 54 22 7D (10 bytes)
      3. 'C' (0x43 -> 0x2f2c5): Cut mode (len=7, 4-byte payload):
           - Auto-cut each label (TapeCut=0, HalfCut=0): 01 01 01 01 -> 1B 7B 07 43 01 01 01 01 47 7D
           - No cut (TapeCut=2, HalfCut=0):              00 00 00 00 -> 1B 7B 07 43 00 00 00 00 43 7D
      4. 'D' (0x44 -> 0x2fc37): Print density (len=4, 1-byte payload = density + 5, range 0..10, default 5)
      5. 'G' (0x47 -> 0x2fde2): Job graphics/mode header (len=3, 0-byte payload) -> 1B 7B 03 47 47 7D
    """
    density_mapped = max(0, min(10, int(density) + 5))
    cut_payload = bytes([0x01, 0x01, 0x01, 0x01]) if cut_each_label else bytes([0x00, 0x00, 0x00, 0x00])

    cmds = [
        make_command("@", b""),
        make_command("{", bytes([0x00, 0x00, 0x53, 0x54])),  # 00 00 'S' 'T'
        make_command("C", cut_payload),
        make_command("D", bytes([density_mapped])),
        make_command("G", b""),
    ]
    return b"".join(cmds)


def make_page_environment_commands(
    total_page_length_dots: int,
    margin_dots: int,
) -> bytes:
    """
    Build the Page Environment sequence from CommandFilter::makePageEnvironmentCommand
    (0x31340 in libLWPrint.so) for CommandLevel 5 (LW-C610):
      1. 'L' (0x4C -> 0x2fe4b): Total page length in dots (uint32 little-endian, len=7)
      2. 'T' (0x54 -> 0x2fff0): Margin in dots (uint16 little-endian, len=5)
      (Note: 'B' 0x42 is a no-op in CommandFilter::makeCommand's jump table at 0x589a4)
    """
    length_le32 = struct.pack("<I", max(1, int(total_page_length_dots)))
    margin_le16 = struct.pack("<H", max(0, min(0xFFFF, int(margin_dots))))

    cmds = [
        make_command("L", length_le32),
        make_command("T", margin_le16),
    ]
    return b"".join(cmds)


def make_raster_line_header(printable_dots: int) -> bytes:
    """
    Build the 8-byte ESC . raster line header from PrintController.makeRasterCommand:
      1B 2E 00 00 00 01 [dots_low] [dots_high]
    """
    return bytes([
        0x1B,
        0x2E,
        0x00,
        0x00,
        0x00,
        0x01,
        printable_dots & 0xFF,
        (printable_dots >> 8) & 0xFF,
    ])


def mm_to_dots(mm: float) -> int:
    """Convert millimeters to dots at 360 DPI (matching LWPrintDotbydotDraw)."""
    return int(round((float(mm) / 25.4) * DPI))


def dots_to_mm(dots: int) -> float:
    """Convert 360 DPI dots to millimeters."""
    return (float(dots) * 25.4) / DPI


def pad_bitmap_with_margins(
    label_image: Image.Image,
    printable_dots: int,
    lead_margin_dots: int,
    trail_margin_dots: Optional[int] = None,
) -> Image.Image:
    """
    Replicate LWPrintDotbydotDraw.beginPage & drawContent (dex_dump2.txt lines 5106-5160):
      - Creates a white horizontal canvas of width = imageWidth + lead_margin_dots + trail_margin_dots
        and height = printable_dots.
      - Draws label_image starting at x = lead_margin_dots, vertically centered.
      - This ensures the printer feeds `lead_margin_dots` of blank tape before printing the
        first character so the left side of the first character is never clipped!
    """
    if trail_margin_dots is None:
        trail_margin_dots = lead_margin_dots

    gray = label_image.convert("L")
    w, h = gray.size
    lead = max(0, int(lead_margin_dots))
    trail = max(0, int(trail_margin_dots))
    total_w = w + lead + trail

    canvas = Image.new("L", (total_w, printable_dots), 255)
    y_off = (printable_dots - h) // 2
    canvas.paste(gray, (lead, y_off))
    return canvas


def bitmap_to_raster_lines(
    label_image: Image.Image,
    printable_dots: int,
    lead_margin_dots: int = 0,
    trail_margin_dots: Optional[int] = None,
    threshold: int = 140,
) -> Tuple[List[bytes], int]:
    """
    Convert a horizontal label PIL Image into LW-C610 raster packets, including
    the LWPrintDotbydotDraw left/right margin padding (`lead_margin_dots` / `trail_margin_dots`).

    Follows LWPrintDotbydotDraw + PrintController.getImageData + CommandFilter::bitmapToRaster (0x31b40):
      - Pads the horizontal bitmap with `lead_margin_dots` white columns on the left and
        `trail_margin_dots` white columns on the right.
      - Rotates 90 degrees clockwise so each scanline represents one vertical slice across
        the tape (width = printable_dots, height = total_length_dots).
      - Each pixel with grayscale luminance < threshold (default 0x8C = 140) sets bit=1 (black).
      - Bits are packed MSB-first (bit 7 = 0x80 is pixel 0, bit 0 = 0x01 is pixel 7).
      - Each scanline is prefixed with make_raster_line_header(printable_dots).
    """
    padded = pad_bitmap_with_margins(
        label_image=label_image,
        printable_dots=printable_dots,
        lead_margin_dots=lead_margin_dots,
        trail_margin_dots=trail_margin_dots,
    )

    # Rotate 90 degrees clockwise (matching PrintController.getImageData postRotate(90.0f))
    rotated = padded.transpose(Image.Transpose.ROTATE_270)
    rot_w, rot_h = rotated.size  # rot_w == printable_dots, rot_h == total_length_dots
    raw_pixels = rotated.tobytes()

    bytes_per_row = (printable_dots + 7) // 8
    header = make_raster_line_header(printable_dots)
    raster_lines: List[bytes] = []

    for row_idx in range(rot_h):
        row_start = row_idx * rot_w
        row_bytes = bytearray(bytes_per_row)
        for x in range(rot_w):
            if raw_pixels[row_start + x] < threshold:
                row_bytes[x >> 3] |= (0x80 >> (x & 7))
        raster_lines.append(header + bytes(row_bytes))

    return raster_lines, rot_h


@dataclass
class PrinterStatus:
    """Decoded 91-byte Bluetooth Protocol v2 status from LW-C610."""
    raw_packet: bytes
    prefix_ascii: str
    st: int = 0
    er: int = 0
    tw_raw: int = 0
    tr_raw: int = 0
    ei_raw: int = 0
    to_raw: int = 0
    ir_raw: int = 0
    rr_raw: int = 0
    ej_raw: int = 0
    pp: int = 0
    tc_raw: int = 0
    rc_raw: int = 0
    rb: int = 0
    serial_number: str = ""
    tape_code: int = 0
    tape_width_mm: int = 0
    printable_dots: int = 144

    @property
    def status_text(self) -> str:
        return STATUS_CODE_NAMES.get(self.st, f"상태 코드 0x{self.st:02X}")

    @property
    def error_text(self) -> str:
        return ERROR_CODE_NAMES.get(self.er, f"오류 코드 0x{self.er:02X}")

    @property
    def tape_label(self) -> str:
        return TAPE_WIDTH_LABELS.get(self.tape_code, f"코드 0x{self.tw_raw:02X}")

    @property
    def is_ready(self) -> bool:
        return self.er == 0x00 and self.st in (0x00, 0x04, 0x05)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "PrinterStatus":
        if len(raw) < STATUS_TOTAL_LENGTH:
            raise ValueError(f"상태 패킷 길이 부족: {len(raw)} bytes (예상: {STATUS_TOTAL_LENGTH} bytes)")

        prefix = raw[:STATUS_PREFIX_LENGTH]
        payload = raw[STATUS_PREFIX_LENGTH:STATUS_TOTAL_LENGTH]

        prefix_str = prefix.decode("ascii", errors="replace").replace("\r", "\\r").replace("\n", "\\n")
        st = payload[1]
        er = payload[2]
        tw_raw = payload[3]
        tr_raw = payload[4]
        ei_raw = payload[5]
        to_raw = payload[8]
        ir_raw = payload[9]
        rr_raw = payload[10]
        ej_raw = payload[11]
        pp = (payload[12] << 8) | payload[13]
        tc_raw = payload[17]
        rc_raw = payload[18]
        rb = (payload[25] << 24) | (payload[26] << 16) | (payload[27] << 8) | payload[28]

        bsn_bytes = bytes(b for b in payload[66:78] if 32 <= b < 127)
        serial_str = bsn_bytes.decode("ascii", errors="ignore").strip()

        tape_code = TW_RAW_TO_TAPE_CODE.get(tw_raw, -1)
        tape_mm = TAPE_WIDTH_MM.get(tape_code, 12)
        dots = TAPE_PRINTABLE_DOTS.get(tape_code, 144)

        return cls(
            raw_packet=raw[:STATUS_TOTAL_LENGTH],
            prefix_ascii=prefix_str,
            st=st,
            er=er,
            tw_raw=tw_raw,
            tr_raw=tr_raw,
            ei_raw=ei_raw,
            to_raw=to_raw,
            ir_raw=ir_raw,
            rr_raw=rr_raw,
            ej_raw=ej_raw,
            pp=pp,
            tc_raw=tc_raw,
            rc_raw=rc_raw,
            rb=rb,
            serial_number=serial_str,
            tape_code=tape_code,
            tape_width_mm=tape_mm,
            printable_dots=dots,
        )


def find_lwc610_ports() -> List[Tuple[str, str]]:
    """
    Return a list of (port_name, description) tuples for available serial ports,
    sorting LW-C610 / Bluetooth SPP ports (especially MAC BC:31:98:A6:DB:48 / COM4) first.
    """
    ports = list(serial.tools.list_ports.comports())
    result: List[Tuple[str, str]] = []
    preferred: List[Tuple[str, str]] = []

    for p in ports:
        hwid = (p.hwid or "").upper()
        desc = p.description or "Serial Port"
        if "BC3198A6DB48" in hwid or "LW-C610" in desc.upper():
            preferred.append((p.device, f"{p.device} - LW-C610@a6db48 ({desc})"))
        elif p.device.upper() == "COM4" and "BTHENUM" in hwid:
            preferred.append((p.device, f"{p.device} - LW-C610 Bluetooth ({desc})"))
        else:
            result.append((p.device, f"{p.device} - {desc}"))

    combined = preferred + result
    if not any(dev.upper() == "COM4" for dev, _ in combined):
        combined.insert(0, ("COM4", "COM4 - LW-C610@a6db48 (기본 포트)"))
    return combined


class LWC610Printer:
    """High-level controller for the Epson LW-C610 Bluetooth Label Printer."""

    def __init__(self, port: str = "COM4", baudrate: int = 115200, timeout: float = 4.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout

    def _open_serial(self) -> serial.Serial:
        return serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )

    @staticmethod
    def _read_status_packet(ser: serial.Serial, timeout: float = 4.0) -> Optional[PrinterStatus]:
        """Read a 91-byte "@BDC ST2..." status packet from the serial stream."""
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline:
            chunk = ser.read(max(1, STATUS_TOTAL_LENGTH - len(buf)))
            if chunk:
                buf.extend(chunk)
                # Synchronize on '@BDC ST2' if any leading noise bytes exist
                idx = buf.find(b"@BDC")
                if idx > 0:
                    del buf[:idx]
                if len(buf) >= STATUS_TOTAL_LENGTH:
                    return PrinterStatus.from_bytes(bytes(buf[:STATUS_TOTAL_LENGTH]))
            else:
                time.sleep(0.03)
        return None

    def get_status(self) -> PrinterStatus:
        """Query live printer status (read-only) via CMD_REQUEST_STATUS."""
        with self._open_serial() as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(CMD_REQUEST_STATUS)
            ser.flush()
            status = self._read_status_packet(ser, timeout=self.timeout)
            if status is None:
                raise TimeoutError(
                    f"{self.port} 포트에서 LW-C610 응답이 없습니다. 프린터 전원이 켜져 있는지 확인해 주세요."
                )
            return status

    def feed_tape(self, cut: bool = True) -> PrinterStatus:
        """
        Execute Tape Feed (with or without cutter) following PrintController.executeTapeFeed.
        """
        with self._open_serial() as ser:
            ser.reset_input_buffer()
            ser.write(CMD_REQUEST_STATUS)
            ser.flush()
            status = self._read_status_packet(ser, timeout=self.timeout)
            if status is None:
                raise TimeoutError("프린터 상태 응답 시간 초과")
            if status.er != 0:
                raise RuntimeError(f"프린터 오류 상태: {status.error_text}")

            ser.write(CMD_SET_REQUEST_STATUS)
            ser.flush()
            time.sleep(0.05)

            feed_cmd = CMD_FEED_AND_CUT if cut else CMD_FEED_ONLY
            ser.write(feed_cmd)
            ser.flush()

            final_status = status
            deadline = time.time() + 12.0
            while time.time() < deadline:
                time.sleep(0.3)
                ser.write(CMD_REQUEST_STATUS)
                ser.flush()
                st_pkt = self._read_status_packet(ser, timeout=2.0)
                if st_pkt is not None:
                    final_status = st_pkt
                    if st_pkt.er != 0:
                        break
                    if st_pkt.st in (0x00, 0x04):
                        break

            ser.write(CMD_REQUEST_STATUS)
            ser.flush()
            return final_status

    def print_bitmap(
        self,
        label_image: Image.Image,
        cut: bool = True,
        density: int = 0,
        margin_mm: float = 0.0,
        lead_offset_mm: float = 0.0,
        copies: int = 1,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> PrinterStatus:
        """
        Print a horizontal label PIL Image on the LW-C610 following LWPrintDotbydotDraw + PrintController.executePrint:
          1. Query printer status to obtain live TW, TR, IR and verify readiness
          2. Pad label_image with `lead_margin_dots` on the left and `trail_margin_dots` on the right
             (matching LWPrintDotbydotDraw._topFrameRect = imageWidth + 2 * _marginValue, plus any
             extra `lead_offset_mm` compensation to prevent left-side clipping of the first character),
             and convert to 360 DPI 1bpp raster lines.
          3. Send CMD_SET_REQUEST_STATUS
          4. Send Job Environment Commands ('@', '{', 'C', 'D', 'G')
          5. Send CMD_SET_REQUEST_STATUS
          6. For each copy (1..copies):
             a. Send Page Environment Commands ('L' = total_length_dots, 'T' = margin_dots)
             b. Send raster lines in bands of up to 1024 lines
             c. Send Form Feed (0x0C)
          7. Send CMD_PRINT_END (1B 7B 03 40 40 7D)
          8. Poll status until printing finishes (ST == 0x00 Idle) and reset status request
        """
        copies = max(1, min(99, int(copies)))
        margin_dots = max(0, mm_to_dots(margin_mm))
        extra_lead_dots = max(0, mm_to_dots(lead_offset_mm))
        lead_margin_dots = margin_dots + extra_lead_dots
        trail_margin_dots = margin_dots

        if progress_callback:
            progress_callback(0.05, "프린터 상태 확인 중...")

        with self._open_serial() as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            # 1. Initial status check
            ser.write(CMD_REQUEST_STATUS)
            ser.flush()
            status = self._read_status_packet(ser, timeout=self.timeout)
            if status is None:
                raise TimeoutError("프린터 상태 응답이 없습니다.")
            if status.er != 0:
                raise RuntimeError(f"프린터 오류: {status.error_text}")
            if status.tape_code <= 0:
                raise RuntimeError(f"테이프 카트리지가 감지되지 않았습니다 ({status.tape_label}).")

            printable_dots = status.printable_dots
            raster_lines, total_length_dots = bitmap_to_raster_lines(
                label_image=label_image,
                printable_dots=printable_dots,
                lead_margin_dots=lead_margin_dots,
                trail_margin_dots=trail_margin_dots,
            )
            if total_length_dots <= 0:
                raise ValueError("인쇄할 이미지 길이가 0입니다.")

            if progress_callback:
                progress_callback(
                    0.12,
                    f"작업 초기화 중 ({status.tape_width_mm}mm 테이프, 총 {total_length_dots}x{printable_dots} dots)...",
                )

            # 2. Set Request Status & Send Job Environment
            ser.write(CMD_SET_REQUEST_STATUS)
            ser.flush()
            time.sleep(0.05)

            job_env = make_job_environment_commands(
                cut_each_label=cut,
                density=density,
            )
            ser.write(job_env)
            ser.flush()
            time.sleep(0.05)

            ser.write(CMD_SET_REQUEST_STATUS)
            ser.flush()
            time.sleep(0.05)

            page_env = make_page_environment_commands(
                total_page_length_dots=total_length_dots,
                margin_dots=margin_dots,
            )

            # 3. Send copies & bands (PRINT_CONTROLLER_BANDING_SIZE = 1024)
            band_size = 1024
            total_bands = ((total_length_dots + band_size - 1) // band_size) * copies
            bands_sent = 0

            for copy_idx in range(copies):
                ser.write(page_env)
                ser.flush()

                for start_row in range(0, total_length_dots, band_size):
                    band = b"".join(raster_lines[start_row : start_row + band_size])
                    ser.write(band)
                    ser.flush()
                    bands_sent += 1

                    if progress_callback:
                        frac = 0.15 + 0.65 * (bands_sent / max(1, total_bands))
                        progress_callback(
                            frac,
                            f"래스터 데이터 전송 중 ({copy_idx + 1}/{copies}매, {min(total_length_dots, start_row + band_size)}/{total_length_dots} rows)...",
                        )

                # Form Feed at end of page
                ser.write(CMD_FORM_FEED)
                ser.flush()

            # 4. Send Print End
            if progress_callback:
                progress_callback(0.85, "인쇄 및 커팅 완료 대기 중...")

            ser.write(CMD_PRINT_END)
            ser.flush()

            # 5. Poll status until printer finishes printing and returns to Idle (0x00)
            final_status = status
            saw_busy = False
            deadline = time.time() + max(20.0, 8.0 * copies)
            while time.time() < deadline:
                time.sleep(0.35)
                if ser.in_waiting < STATUS_TOTAL_LENGTH:
                    ser.write(CMD_REQUEST_STATUS)
                    ser.flush()
                st_pkt = self._read_status_packet(ser, timeout=1.5)
                if st_pkt is not None:
                    final_status = st_pkt
                    if st_pkt.er != 0:
                        raise RuntimeError(f"인쇄 중 프린터 오류 발생: {st_pkt.error_text}")
                    if st_pkt.st in (0x01, 0x02, 0x03, 0x05):
                        saw_busy = True
                    elif st_pkt.st == 0x00 and (saw_busy or time.time() + 15.0 > deadline):
                        break

            # Reset status request
            ser.write(CMD_REQUEST_STATUS)
            ser.flush()

            if progress_callback:
                progress_callback(1.0, "인쇄 완료!")

            return final_status
