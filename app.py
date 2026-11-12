"""
Epson LW-C610 (LW-C610@a6db48) PC Label Printer Studio (GUI + CLI).

Usage:
  GUI Mode (default):
    python app.py

  CLI Mode examples:
    python app.py --status
    python app.py --text "서버실랙 #01" --frame "둥근 사각형 (Rounded)" --margin 6
    python app.py --text "Wi-Fi 접속" --qr "WIFI:S:MyOffice;T:WPA;P:secret;;"
    python app.py --feed
"""

from __future__ import annotations

import argparse
import datetime
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from PIL import Image, ImageTk

from lwc610_protocol import (
    LWC610Printer,
    PrinterStatus,
    TAPE_PRINTABLE_DOTS,
    TAPE_WIDTH_MM,
    dots_to_mm,
    find_lwc610_ports,
    mm_to_dots,
)
from lwc610_render import (
    FRAME_STYLES,
    LINE_RATIO_MODES,
    WINDOWS_FONT_MAP,
    LabelDesign,
    render_label_bitmap,
    render_preview_image,
)


class LWC610App:
    """Desktop GUI Application for the Epson LW-C610 Label Printer."""

    def __init__(self, root: tk.Tk, initial_port: str = "COM4"):
        self.root = root
        self.root.title("Epson LW-C610 라벨프린터 스튜디오 (LW-C610@a6db48)")
        self.root.geometry("1080x800")
        self.root.minsize(940, 700)
        self.root.configure(bg="#F3F5F8")

        self.last_status: Optional[PrinterStatus] = None
        self.current_bitmap: Optional[Image.Image] = None
        self.current_preview: Optional[Image.Image] = None
        self.tk_preview: Optional[ImageTk.PhotoImage] = None
        self._busy = False

        # Tkinter state variables
        self.port_var = tk.StringVar(value=initial_port)
        self.status_badge_var = tk.StringVar(value="연결 확인 중...")
        self.tape_info_var = tk.StringVar(value="감지 전 (기본 12 mm / 144 dots)")
        self.error_info_var = tk.StringVar(value="-")
        self.raw_info_var = tk.StringVar(value="-")

        self.font_name_var = tk.StringVar(value="맑은 고딕 (Malgun Gothic)")
        self.auto_size_var = tk.BooleanVar(value=True)
        self.font_size_var = tk.IntVar(value=84)
        self.bold_var = tk.BooleanVar(value=True)
        self.invert_var = tk.BooleanVar(value=False)
        self.align_var = tk.StringVar(value="center")
        self.frame_var = tk.StringVar(value="없음 (None)")

        self.line1_var = tk.StringVar(value="안녕하세요 LW-C610")
        self.line2_var = tk.StringVar(value="")
        self.line_ratio_var = tk.StringVar(value=LINE_RATIO_MODES[0])
        self.line_gap_var = tk.IntVar(value=8)
        self.line_divider_var = tk.BooleanVar(value=False)
        self._syncing_text = False

        self.qr_text_var = tk.StringVar(value="")
        self.qr_pos_var = tk.StringVar(value="left")
        self.image_path_var = tk.StringVar(value="")
        self.image_dither_var = tk.BooleanVar(value=False)

        self.tape_override_var = tk.StringVar(value="자동 감지 (프린터 기준)")
        self.margin_mm_var = tk.DoubleVar(value=0.0)
        self.lead_offset_mm_var = tk.DoubleVar(value=0.0)
        self.fixed_len_mm_var = tk.DoubleVar(value=0.0)
        self.density_var = tk.IntVar(value=0)
        self.cut_var = tk.BooleanVar(value=True)
        self.copies_var = tk.IntVar(value=1)

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="준비 완료")
        self.dimensions_var = tk.StringVar(value="")

        self._setup_styles()
        self._build_ui()
        self._refresh_ports(initial_port)
        self.update_preview()

        # Automatically query COM4 status 300ms after window opens
        self.root.after(300, self.query_printer_status_async)

    def _setup_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        style.configure("Card.TLabelframe", background="#FFFFFF", padding=10)
        style.configure("Card.TLabelframe.Label", font=("Malgun Gothic", 10, "bold"), foreground="#1E293B")
        style.configure("TLabel", font=("Malgun Gothic", 9))
        style.configure("Header.TLabel", font=("Malgun Gothic", 13, "bold"), foreground="#0F172A")
        style.configure("SubHeader.TLabel", font=("Malgun Gothic", 9), foreground="#475569")
        style.configure("Action.TButton", font=("Malgun Gothic", 10, "bold"), padding=6)

    def _build_ui(self) -> None:
        # Top Header & Printer Connection Bar
        top_frame = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        top_frame.pack(fill=tk.X)

        title_box = ttk.Frame(top_frame)
        title_box.pack(side=tk.LEFT)
        ttk.Label(
            title_box,
            text="Epson LW-C610 블루투스 라벨프린터 스튜디오",
            style="Header.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            title_box,
            text="장치: LW-C610@a6db48 (360 DPI · Command Level 5 · 좌측 잘림 방지 여백 보정 적용)",
            style="SubHeader.TLabel",
        ).pack(anchor=tk.W)

        conn_box = ttk.Frame(top_frame)
        conn_box.pack(side=tk.RIGHT)
        ttk.Label(conn_box, text="포트:").pack(side=tk.LEFT, padx=(0, 4))
        self.port_combo = ttk.Combobox(conn_box, textvariable=self.port_var, width=32, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(conn_box, text="포트 스캔", command=self._refresh_ports).pack(side=tk.LEFT, padx=(0, 6))
        self.btn_status = ttk.Button(
            conn_box,
            text="프린터 상태 새로고침",
            command=self.query_printer_status_async,
        )
        self.btn_status.pack(side=tk.LEFT)

        # Live Printer Status Banner
        status_card = ttk.LabelFrame(self.root, text="프린터 실시간 상태 (LW-C610@a6db48)", padding=(12, 8))
        status_card.pack(fill=tk.X, padx=14, pady=(0, 8))

        grid_st = ttk.Frame(status_card)
        grid_st.pack(fill=tk.X)
        for col in range(4):
            grid_st.columnconfigure(col * 2 + 1, weight=1)

        ttk.Label(grid_st, text="동작 상태:", font=("Malgun Gothic", 9, "bold")).grid(row=0, column=0, sticky=tk.W, padx=(0, 6))
        self.lbl_status_val = ttk.Label(grid_st, textvariable=self.status_badge_var, foreground="#0284C7", font=("Malgun Gothic", 9, "bold"))
        self.lbl_status_val.grid(row=0, column=1, sticky=tk.W, padx=(0, 16))

        ttk.Label(grid_st, text="장착된 테이프:", font=("Malgun Gothic", 9, "bold")).grid(row=0, column=2, sticky=tk.W, padx=(0, 6))
        ttk.Label(grid_st, textvariable=self.tape_info_var, foreground="#059669", font=("Malgun Gothic", 9, "bold")).grid(row=0, column=3, sticky=tk.W, padx=(0, 16))

        ttk.Label(grid_st, text="오류 상태:", font=("Malgun Gothic", 9, "bold")).grid(row=0, column=4, sticky=tk.W, padx=(0, 6))
        ttk.Label(grid_st, textvariable=self.error_info_var).grid(row=0, column=5, sticky=tk.W, padx=(0, 16))

        ttk.Label(grid_st, text="상세 응답:", font=("Malgun Gothic", 9, "bold")).grid(row=0, column=6, sticky=tk.W, padx=(0, 6))
        ttk.Label(grid_st, textvariable=self.raw_info_var, foreground="#64748B").grid(row=0, column=7, sticky=tk.W)

        # Main Split Area: Left = Design Controls, Right = Preview & Print Actions
        main_pane = ttk.Frame(self.root, padding=(14, 0, 14, 10))
        main_pane.pack(fill=tk.BOTH, expand=True)
        main_pane.columnconfigure(0, weight=5)
        main_pane.columnconfigure(1, weight=6)
        main_pane.rowconfigure(0, weight=1)

        left_col = ttk.Frame(main_pane)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        right_col = ttk.Frame(main_pane)
        right_col.grid(row=0, column=1, sticky="nsew")

        # --- LEFT COLUMN: Text, Font, QR/Image, and Print Settings ---
        # 1. Text Input Card (1-line / 2-line / multi-line)
        text_card = ttk.LabelFrame(left_col, text="1. 라벨 텍스트 입력 (1줄 / 2줄 전용 입력 & 다중 행)", padding=10)
        text_card.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        tpl_bar = ttk.Frame(text_card)
        tpl_bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(tpl_bar, text="빠른 템플릿:").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(tpl_bar, text="1줄 이름표", width=9, command=lambda: self._apply_template("name")).pack(side=tk.LEFT, padx=2)
        ttk.Button(tpl_bar, text="2줄 라벨", width=8, command=lambda: self._apply_template("twoline")).pack(side=tk.LEFT, padx=2)
        ttk.Button(tpl_bar, text="2줄+구분선", width=9, command=lambda: self._apply_template("twoline_div")).pack(side=tk.LEFT, padx=2)
        ttk.Button(tpl_bar, text="보관/날짜", width=8, command=lambda: self._apply_template("date")).pack(side=tk.LEFT, padx=2)
        ttk.Button(tpl_bar, text="QR 안내", width=7, command=lambda: self._apply_template("qr")).pack(side=tk.LEFT, padx=2)
        ttk.Button(tpl_bar, text="지우기", width=6, command=lambda: self._apply_template("clear")).pack(side=tk.RIGHT)

        # Dedicated 1st Line & 2nd Line quick entry boxes
        two_line_box = ttk.Frame(text_card)
        two_line_box.pack(fill=tk.X, pady=(0, 6))
        two_line_box.columnconfigure(1, weight=1)

        ttk.Label(two_line_box, text="1째 줄 (상단):", font=("Malgun Gothic", 9, "bold")).grid(row=0, column=0, sticky=tk.W, pady=2)
        self.entry_line1 = ttk.Entry(two_line_box, textvariable=self.line1_var, font=("Malgun Gothic", 10))
        self.entry_line1.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=2)
        self.entry_line1.bind("<KeyRelease>", lambda _e: self._on_line_entries_changed())

        ttk.Label(two_line_box, text="2째 줄 (하단):", font=("Malgun Gothic", 9, "bold")).grid(row=1, column=0, sticky=tk.W, pady=2)
        self.entry_line2 = ttk.Entry(two_line_box, textvariable=self.line2_var, font=("Malgun Gothic", 10))
        self.entry_line2.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=2)
        self.entry_line2.bind("<KeyRelease>", lambda _e: self._on_line_entries_changed())

        # 2-Line Layout Options Row (Ratio, Line Gap, Divider)
        line_opt_bar = ttk.Frame(text_card)
        line_opt_bar.pack(fill=tk.X, pady=(2, 6))
        ttk.Label(line_opt_bar, text="2줄 비율:").pack(side=tk.LEFT)
        ratio_combo = ttk.Combobox(
            line_opt_bar,
            textvariable=self.line_ratio_var,
            values=LINE_RATIO_MODES,
            state="readonly",
            width=22,
        )
        ratio_combo.pack(side=tk.LEFT, padx=(4, 10))
        ratio_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_preview())

        ttk.Label(line_opt_bar, text="줄 간격(dots):").pack(side=tk.LEFT)
        spin_line_gap = ttk.Spinbox(
            line_opt_bar,
            from_=0,
            to=40,
            increment=2,
            textvariable=self.line_gap_var,
            width=4,
            command=self.update_preview,
        )
        spin_line_gap.pack(side=tk.LEFT, padx=(4, 10))
        spin_line_gap.bind("<KeyRelease>", lambda _e: self.update_preview())

        ttk.Checkbutton(
            line_opt_bar,
            text="줄 사이 구분선",
            variable=self.line_divider_var,
            command=self.update_preview,
        ).pack(side=tk.LEFT)

        ttk.Label(text_card, text="전체 텍스트 편집 (엔터로 2줄/3줄 이상 자유 입력 가능):", foreground="#475569").pack(anchor=tk.W, pady=(0, 2))
        self.text_editor = tk.Text(
            text_card,
            height=3,
            font=("Malgun Gothic", 11),
            undo=True,
            wrap=tk.NONE,
            relief=tk.SOLID,
            borderwidth=1,
            padx=6,
            pady=4,
        )
        self.text_editor.pack(fill=tk.BOTH, expand=True)
        self.text_editor.insert("1.0", "안녕하세요 LW-C610")
        self.text_editor.bind("<KeyRelease>", lambda _e: self._on_text_editor_changed())

        # 2. Font & Frame Style Card
        style_card = ttk.LabelFrame(left_col, text="2. 글꼴 및 테두리 스타일", padding=10)
        style_card.pack(fill=tk.X, pady=(0, 8))
        style_card.columnconfigure(1, weight=1)

        ttk.Label(style_card, text="글꼴(Font):").grid(row=0, column=0, sticky=tk.W, pady=3)
        font_combo = ttk.Combobox(
            style_card,
            textvariable=self.font_name_var,
            values=list(WINDOWS_FONT_MAP.keys()),
            state="readonly",
        )
        font_combo.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=3)
        font_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_preview())

        ttk.Label(style_card, text="글자 크기:").grid(row=1, column=0, sticky=tk.W, pady=3)
        size_row = ttk.Frame(style_card)
        size_row.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=3)
        ttk.Checkbutton(
            size_row,
            text="테이프 폭에 자동 맞춤",
            variable=self.auto_size_var,
            command=self._on_auto_size_toggle,
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(size_row, text="수동(px):").pack(side=tk.LEFT)
        self.spin_font_size = ttk.Spinbox(
            size_row,
            from_=14,
            to=240,
            increment=2,
            textvariable=self.font_size_var,
            width=6,
            command=self.update_preview,
            state="disabled",
        )
        self.spin_font_size.pack(side=tk.LEFT, padx=4)
        self.spin_font_size.bind("<KeyRelease>", lambda _e: self.update_preview())

        ttk.Label(style_card, text="스타일/정렬:").grid(row=2, column=0, sticky=tk.W, pady=3)
        opt_row = ttk.Frame(style_card)
        opt_row.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=3)
        ttk.Checkbutton(opt_row, text="굵게(Bold)", variable=self.bold_var, command=self.update_preview).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Checkbutton(opt_row, text="흑백 반전", variable=self.invert_var, command=self.update_preview).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(opt_row, text="좌측", value="left", variable=self.align_var, command=self.update_preview).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(opt_row, text="가운데", value="center", variable=self.align_var, command=self.update_preview).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(opt_row, text="우측", value="right", variable=self.align_var, command=self.update_preview).pack(side=tk.LEFT, padx=2)

        ttk.Label(style_card, text="테두리(Frame):").grid(row=3, column=0, sticky=tk.W, pady=3)
        frame_combo = ttk.Combobox(
            style_card,
            textvariable=self.frame_var,
            values=FRAME_STYLES,
            state="readonly",
        )
        frame_combo.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(6, 0), pady=3)
        frame_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_preview())

        # 3. QR Code & Image Card
        extra_card = ttk.LabelFrame(left_col, text="3. QR 코드 및 이미지 삽입 (선택)", padding=10)
        extra_card.pack(fill=tk.X, pady=(0, 8))
        extra_card.columnconfigure(1, weight=1)

        ttk.Label(extra_card, text="QR 데이터:").grid(row=0, column=0, sticky=tk.W, pady=3)
        qr_entry = ttk.Entry(extra_card, textvariable=self.qr_text_var)
        qr_entry.grid(row=0, column=1, sticky="ew", padx=(6, 6), pady=3)
        qr_entry.bind("<KeyRelease>", lambda _e: self.update_preview())

        qr_pos_box = ttk.Frame(extra_card)
        qr_pos_box.grid(row=0, column=2, sticky=tk.E, pady=3)
        ttk.Radiobutton(qr_pos_box, text="좌측", value="left", variable=self.qr_pos_var, command=self.update_preview).pack(side=tk.LEFT)
        ttk.Radiobutton(qr_pos_box, text="우측", value="right", variable=self.qr_pos_var, command=self.update_preview).pack(side=tk.LEFT)
        ttk.Radiobutton(qr_pos_box, text="QR만", value="only", variable=self.qr_pos_var, command=self.update_preview).pack(side=tk.LEFT)

        ttk.Label(extra_card, text="이미지 파일:").grid(row=1, column=0, sticky=tk.W, pady=3)
        img_entry = ttk.Entry(extra_card, textvariable=self.image_path_var, state="readonly")
        img_entry.grid(row=1, column=1, sticky="ew", padx=(6, 6), pady=3)

        img_btn_box = ttk.Frame(extra_card)
        img_btn_box.grid(row=1, column=2, sticky=tk.E, pady=3)
        ttk.Button(img_btn_box, text="찾아보기", width=8, command=self._browse_image).pack(side=tk.LEFT, padx=(0, 3))
        ttk.Button(img_btn_box, text="제거", width=5, command=self._clear_image).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Checkbutton(img_btn_box, text="디더링", variable=self.image_dither_var, command=self.update_preview).pack(side=tk.LEFT)

        # 4. Print Parameters Card
        param_card = ttk.LabelFrame(left_col, text="4. 테이프 및 인쇄 옵션 (360 DPI)", padding=10)
        param_card.pack(fill=tk.X)
        param_card.columnconfigure(1, weight=1)
        param_card.columnconfigure(3, weight=1)

        ttk.Label(param_card, text="테이프 폭:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.tape_combo = ttk.Combobox(
            param_card,
            textvariable=self.tape_override_var,
            values=[
                "자동 감지 (프린터 기준)",
                "4 mm (44 dots)",
                "6 mm (72 dots)",
                "9 mm (108 dots)",
                "12 mm (144 dots)",
                "18 mm (216 dots)",
                "24 mm (256 dots)",
            ],
            state="readonly",
            width=18,
        )
        self.tape_combo.current(0)
        self.tape_combo.grid(row=0, column=1, sticky="ew", padx=(6, 12), pady=3)
        self.tape_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_preview())

        ttk.Label(param_card, text="기본 여백(mm):").grid(row=0, column=2, sticky=tk.W, pady=3)
        spin_margin = ttk.Spinbox(
            param_card,
            from_=0.0,
            to=21.0,
            increment=1.0,
            textvariable=self.margin_mm_var,
            width=7,
            command=self.update_preview,
        )
        spin_margin.grid(row=0, column=3, sticky=tk.W, padx=(6, 0), pady=3)
        spin_margin.bind("<KeyRelease>", lambda _e: self.update_preview())

        ttk.Label(param_card, text="좌측 보정(mm):").grid(row=1, column=0, sticky=tk.W, pady=3)
        spin_lead = ttk.Spinbox(
            param_card,
            from_=0.0,
            to=15.0,
            increment=0.5,
            textvariable=self.lead_offset_mm_var,
            width=8,
            command=self.update_preview,
        )
        spin_lead.grid(row=1, column=1, sticky=tk.W, padx=(6, 12), pady=3)
        spin_lead.bind("<KeyRelease>", lambda _e: self.update_preview())

        ttk.Label(param_card, text="인쇄 농도(-5~5):").grid(row=1, column=2, sticky=tk.W, pady=3)
        spin_density = ttk.Spinbox(
            param_card,
            from_=-5,
            to=5,
            increment=1,
            textvariable=self.density_var,
            width=7,
        )
        spin_density.grid(row=1, column=3, sticky=tk.W, padx=(6, 0), pady=3)

        cut_row = ttk.Frame(param_card)
        cut_row.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(
            cut_row,
            text="자동 커팅 (Auto Cut)",
            variable=self.cut_var,
        ).pack(side=tk.LEFT)

        ttk.Label(cut_row, text="매수:").pack(side=tk.LEFT, padx=(14, 4))
        spin_copies = ttk.Spinbox(
            cut_row,
            from_=1,
            to=99,
            increment=1,
            textvariable=self.copies_var,
            width=5,
        )
        spin_copies.pack(side=tk.LEFT)

        ttk.Label(cut_row, text="고정 길이(mm, 0=자동):").pack(side=tk.RIGHT, padx=(8, 4))
        spin_fixed = ttk.Spinbox(
            cut_row,
            from_=0.0,
            to=300.0,
            increment=5.0,
            textvariable=self.fixed_len_mm_var,
            width=6,
            command=self.update_preview,
        )
        spin_fixed.pack(side=tk.RIGHT)
        spin_fixed.bind("<KeyRelease>", lambda _e: self.update_preview())

        # --- RIGHT COLUMN: Live 360 DPI Preview & Print Execution ---
        preview_card = ttk.LabelFrame(
            right_col,
            text="실시간 360 DPI 인쇄 미리보기 (빨간 점선: 여백 경계 / 파란 점선: 인쇄 영역)",
            padding=10,
        )
        preview_card.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.preview_canvas = tk.Canvas(
            preview_card,
            bg="#E8ECF2",
            highlightthickness=1,
            highlightbackground="#CBD5E1",
        )
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        self.preview_canvas.bind("<Configure>", lambda _e: self._redraw_canvas_image())

        info_bar = ttk.Frame(preview_card)
        info_bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(info_bar, textvariable=self.dimensions_var, foreground="#334155", font=("Malgun Gothic", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(info_bar, text="미리보기 이미지 저장 (PNG)", command=self.save_preview_png).pack(side=tk.RIGHT)

        # Action & Progress Card
        action_card = ttk.LabelFrame(right_col, text="프린터 제어 및 인쇄 실행", padding=10)
        action_card.pack(fill=tk.X)

        btn_row = ttk.Frame(action_card)
        btn_row.pack(fill=tk.X, pady=(0, 8))

        self.btn_print = tk.Button(
            btn_row,
            text="🖨️ 라벨 인쇄하기 (Print)",
            font=("Malgun Gothic", 11, "bold"),
            bg="#0284C7",
            fg="#FFFFFF",
            activebackground="#0369A1",
            activeforeground="#FFFFFF",
            relief=tk.FLAT,
            padx=18,
            pady=8,
            cursor="hand2",
            command=self.print_label_async,
        )
        self.btn_print.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.btn_feed_cut = ttk.Button(
            btn_row,
            text="✂️ 테이프 피드 & 커트",
            style="Action.TButton",
            command=lambda: self.feed_tape_async(cut=True),
        )
        self.btn_feed_cut.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_feed_only = ttk.Button(
            btn_row,
            text="⏩ 테이프 피드만",
            style="Action.TButton",
            command=lambda: self.feed_tape_async(cut=False),
        )
        self.btn_feed_only.pack(side=tk.LEFT)

        prog_row = ttk.Frame(action_card)
        prog_row.pack(fill=tk.X)
        self.progress_bar = ttk.Progressbar(prog_row, variable=self.progress_var, maximum=100.0)
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        ttk.Label(prog_row, textvariable=self.progress_text_var, width=32, anchor=tk.E).pack(side=tk.RIGHT)

    def _get_selected_port(self) -> str:
        raw = self.port_var.get().strip()
        if " - " in raw:
            return raw.split(" - ", 1)[0].strip()
        return raw or "COM4"

    def _refresh_ports(self, prefer_port: Optional[str] = None) -> None:
        ports = find_lwc610_ports()
        display_list = [desc for _, desc in ports]
        self.port_combo["values"] = display_list
        if prefer_port:
            for dev, desc in ports:
                if dev.upper() == prefer_port.upper():
                    self.port_var.set(desc)
                    return
        if display_list:
            self.port_var.set(display_list[0])

    def _on_auto_size_toggle(self) -> None:
        if self.auto_size_var.get():
            self.spin_font_size.configure(state="disabled")
        else:
            self.spin_font_size.configure(state="normal")
        self.update_preview()

    def _browse_image(self) -> None:
        path = filedialog.askopenfilename(
            title="라벨에 삽입할 이미지 선택",
            filetypes=[("이미지 파일", "*.png;*.jpg;*.jpeg;*.bmp;*.gif"), ("모든 파일", "*.*")],
        )
        if path:
            self.image_path_var.set(path)
            self.update_preview()

    def _clear_image(self) -> None:
        self.image_path_var.set("")
        self.update_preview()

    def _on_line_entries_changed(self) -> None:
        if self._syncing_text:
            return
        self._syncing_text = True
        try:
            l1 = self.line1_var.get()
            l2 = self.line2_var.get()
            existing_lines = self.text_editor.get("1.0", "end-1c").splitlines()
            extra_lines = existing_lines[2:] if len(existing_lines) > 2 else []
            new_lines = [l1]
            if l2 or extra_lines:
                new_lines.append(l2)
            new_lines.extend(extra_lines)
            combined = "\n".join(new_lines)
            self.text_editor.delete("1.0", tk.END)
            self.text_editor.insert("1.0", combined)
        finally:
            self._syncing_text = False
        self.update_preview()

    def _on_text_editor_changed(self) -> None:
        if self._syncing_text:
            return
        self._syncing_text = True
        try:
            lines = self.text_editor.get("1.0", "end-1c").splitlines()
            self.line1_var.set(lines[0] if len(lines) >= 1 else "")
            self.line2_var.set(lines[1] if len(lines) >= 2 else "")
        finally:
            self._syncing_text = False
        self.update_preview()

    def _set_text_lines(self, text: str) -> None:
        self._syncing_text = True
        try:
            self.text_editor.delete("1.0", tk.END)
            self.text_editor.insert("1.0", text)
            lines = text.splitlines()
            self.line1_var.set(lines[0] if len(lines) >= 1 else "")
            self.line2_var.set(lines[1] if len(lines) >= 2 else "")
        finally:
            self._syncing_text = False

    def _apply_template(self, kind: str) -> None:
        today = datetime.date.today().strftime("%Y-%m-%d")
        if kind == "name":
            self._set_text_lines("홍 길 동")
            self.frame_var.set("둥근 사각형 (Rounded)")
            self.line_divider_var.set(False)
            self.qr_text_var.set("")
        elif kind == "twoline":
            self._set_text_lines("서버실 메인 스위치\n192.168.1.1 (Gi0/1)")
            self.frame_var.set("둥근 사각형 (Rounded)")
            self.line_ratio_var.set(LINE_RATIO_MODES[1])  # 1줄 크게 (6:4)
            self.line_divider_var.set(False)
            self.qr_text_var.set("")
        elif kind == "twoline_div":
            self._set_text_lines("자산번호: IT-2026-01\n관리부서: 인프라팀")
            self.frame_var.set("직사각형 (Rectangle)")
            self.line_ratio_var.set(LINE_RATIO_MODES[0])  # 동일 크기 (1:1)
            self.line_divider_var.set(True)
            self.qr_text_var.set("")
        elif kind == "date":
            self._set_text_lines(f"보관일: {today}\n개봉후 냉장보관")
            self.frame_var.set("없음 (None)")
            self.line_ratio_var.set(LINE_RATIO_MODES[0])
            self.line_divider_var.set(False)
            self.qr_text_var.set("")
        elif kind == "qr":
            self._set_text_lines("Epson LW-C610\n스캔하여 확인")
            self.frame_var.set("둥근 사각형 (Rounded)")
            self.line_ratio_var.set(LINE_RATIO_MODES[0])
            self.line_divider_var.set(False)
            self.qr_text_var.set("https://epson.co.kr")
            self.qr_pos_var.set("left")
        elif kind == "clear":
            self._set_text_lines("")
            self.qr_text_var.set("")
            self.image_path_var.set("")
            self.frame_var.set("없음 (None)")
            self.line_divider_var.set(False)
        self.update_preview()

    def _resolve_active_tape_code(self) -> int:
        choice = self.tape_override_var.get()
        mapping = {
            "4 mm": 1,
            "6 mm": 2,
            "9 mm": 3,
            "12 mm": 4,
            "18 mm": 5,
            "24 mm": 6,
        }
        for prefix, code in mapping.items():
            if choice.startswith(prefix):
                return code
        if self.last_status and self.last_status.tape_code in TAPE_PRINTABLE_DOTS:
            return self.last_status.tape_code
        return 4  # Default 12mm

    def build_label_design(self) -> LabelDesign:
        raw_text = self.text_editor.get("1.0", "end-1c")
        try:
            margin_mm = float(self.margin_mm_var.get())
        except Exception:
            margin_mm = 0.0
        try:
            lead_offset_mm = float(self.lead_offset_mm_var.get())
        except Exception:
            lead_offset_mm = 0.0
        try:
            fixed_mm = float(self.fixed_len_mm_var.get())
        except Exception:
            fixed_mm = 0.0
        try:
            fsize = int(self.font_size_var.get())
        except Exception:
            fsize = 84
        try:
            line_gap = int(self.line_gap_var.get())
        except Exception:
            line_gap = 8

        return LabelDesign(
            text=raw_text,
            font_name=self.font_name_var.get(),
            auto_font_size=bool(self.auto_size_var.get()),
            font_size_px=max(12, fsize),
            bold=bool(self.bold_var.get()),
            invert_colors=bool(self.invert_var.get()),
            h_align=self.align_var.get(),
            frame_style=self.frame_var.get(),
            line_ratio_mode=self.line_ratio_var.get(),
            line_gap_dots=max(0, min(60, line_gap)),
            line_divider=bool(self.line_divider_var.get()),
            qr_text=self.qr_text_var.get(),
            qr_position=self.qr_pos_var.get(),
            image_path=self.image_path_var.get(),
            image_dither=bool(self.image_dither_var.get()),
            tape_code=self._resolve_active_tape_code(),
            fixed_length_mm=max(0.0, fixed_mm),
            margin_mm=max(0.0, min(21.0, margin_mm)),
            lead_offset_mm=max(0.0, min(15.0, lead_offset_mm)),
        )

    def update_preview(self) -> None:
        design = self.build_label_design()
        self.current_bitmap = render_label_bitmap(design)
        self.current_preview = render_preview_image(
            self.current_bitmap,
            tape_code=design.tape_code,
            margin_mm=design.margin_mm,
            lead_offset_mm=design.lead_offset_mm,
        )

        w_dots, h_dots = self.current_bitmap.size
        margin_dots = max(0, mm_to_dots(design.margin_mm))
        lead_dots = margin_dots + max(0, mm_to_dots(design.lead_offset_mm))
        total_dots = w_dots + lead_dots + margin_dots
        content_mm = dots_to_mm(w_dots)
        total_mm = dots_to_mm(total_dots)
        tape_mm = TAPE_WIDTH_MM.get(design.tape_code, 12)

        self.dimensions_var.set(
            f"테이프: {tape_mm}mm (인쇄폭 {h_dots} dots)  |  "
            f"인쇄 영역: {content_mm:.1f}mm ({w_dots} dots)  |  "
            f"총 배출 길이(여백 포함): 약 {total_mm:.1f}mm ({total_dots} dots)"
        )
        self._redraw_canvas_image()

    def _redraw_canvas_image(self) -> None:
        if self.current_preview is None:
            return
        cw = max(100, self.preview_canvas.winfo_width() - 24)
        ch = max(80, self.preview_canvas.winfo_height() - 24)
        pw, ph = self.current_preview.size

        scale = min(cw / max(1, pw), ch / max(1, ph), 2.2)
        new_w = max(1, int(round(pw * scale)))
        new_h = max(1, int(round(ph * scale)))

        resized = self.current_preview.resize((new_w, new_h), Image.Resampling.LANCZOS)
        self.tk_preview = ImageTk.PhotoImage(resized)

        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(
            self.preview_canvas.winfo_width() // 2,
            self.preview_canvas.winfo_height() // 2,
            image=self.tk_preview,
            anchor=tk.CENTER,
        )

    def save_preview_png(self) -> None:
        if self.current_preview is None:
            return
        path = filedialog.asksaveasfilename(
            title="미리보기 이미지 저장",
            defaultextension=".png",
            filetypes=[("PNG 이미지", "*.png")],
            initialfile="lwc610_label_preview.png",
        )
        if path:
            self.current_preview.save(path)
            messagebox.showinfo("저장 완료", f"미리보기 이미지를 저장했습니다:\n{path}")

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_status.configure(state=state)
        self.btn_print.configure(state=state)
        self.btn_feed_cut.configure(state=state)
        self.btn_feed_only.configure(state=state)
        if message:
            self.progress_text_var.set(message)

    def _apply_status_to_ui(self, status: PrinterStatus) -> None:
        self.last_status = status
        self.status_badge_var.set(status.status_text)
        self.tape_info_var.set(status.tape_label)
        self.error_info_var.set(status.error_text)
        self.raw_info_var.set(
            f"ST=0x{status.st:02X}, ER=0x{status.er:02X}, TW=0x{status.tw_raw:02X}, TR=0x{status.tr_raw:02X}"
        )
        if status.er == 0:
            self.lbl_status_val.configure(foreground="#059669")
        else:
            self.lbl_status_val.configure(foreground="#DC2626")
        self.update_preview()

    def query_printer_status_async(self) -> None:
        if self._busy:
            return
        port = self._get_selected_port()
        self._set_busy(True, f"{port} 상태 조회 중...")
        self.progress_var.set(25.0)

        def worker() -> None:
            try:
                printer = LWC610Printer(port=port)
                status = printer.get_status()
                self.root.after(0, lambda: self._on_status_success(status))
            except Exception as exc:
                self.root.after(0, lambda: self._on_status_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_status_success(self, status: PrinterStatus) -> None:
        self._apply_status_to_ui(status)
        self.progress_var.set(100.0)
        self._set_busy(False, f"연결됨 ({status.tape_width_mm}mm 테이프)")

    def _on_status_error(self, exc: Exception) -> None:
        self.status_badge_var.set("연결 대기 / 응답 없음")
        self.lbl_status_val.configure(foreground="#D97706")
        self.error_info_var.set(str(exc))
        self.progress_var.set(0.0)
        self._set_busy(False, "프린터 연결 확인 필요")

    def feed_tape_async(self, cut: bool = True) -> None:
        if self._busy:
            return
        port = self._get_selected_port()
        action_name = "테이프 피드 & 커트" if cut else "테이프 피드"
        self._set_busy(True, f"{action_name} 실행 중...")
        self.progress_var.set(30.0)

        def worker() -> None:
            try:
                printer = LWC610Printer(port=port)
                status = printer.feed_tape(cut=cut)
                self.root.after(0, lambda: self._on_feed_done(status, action_name))
            except Exception as exc:
                self.root.after(0, lambda: self._on_job_error(action_name, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_feed_done(self, status: PrinterStatus, action_name: str) -> None:
        self._apply_status_to_ui(status)
        self.progress_var.set(100.0)
        self._set_busy(False, f"{action_name} 완료!")

    def print_label_async(self) -> None:
        if self._busy:
            return
        self.update_preview()
        if self.current_bitmap is None:
            return

        port = self._get_selected_port()
        cut = bool(self.cut_var.get())
        density = int(self.density_var.get())
        margin_mm = float(self.margin_mm_var.get())
        lead_offset_mm = float(self.lead_offset_mm_var.get())
        copies = int(self.copies_var.get())
        bitmap_to_send = self.current_bitmap.copy()

        self._set_busy(True, "라벨 인쇄 시작...")
        self.progress_var.set(5.0)

        def progress_cb(frac: float, msg: str) -> None:
            self.root.after(
                0,
                lambda: (
                    self.progress_var.set(max(0.0, min(100.0, frac * 100.0))),
                    self.progress_text_var.set(msg),
                ),
            )

        def worker() -> None:
            try:
                printer = LWC610Printer(port=port)
                status = printer.print_bitmap(
                    label_image=bitmap_to_send,
                    cut=cut,
                    density=density,
                    margin_mm=margin_mm,
                    lead_offset_mm=lead_offset_mm,
                    copies=copies,
                    progress_callback=progress_cb,
                )
                self.root.after(0, lambda: self._on_print_done(status))
            except Exception as exc:
                self.root.after(0, lambda: self._on_job_error("라벨 인쇄", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_print_done(self, status: PrinterStatus) -> None:
        self._apply_status_to_ui(status)
        self.progress_var.set(100.0)
        self._set_busy(False, "라벨 인쇄 완료!")

    def _on_job_error(self, title: str, exc: Exception) -> None:
        self.progress_var.set(0.0)
        self._set_busy(False, f"{title} 실패")
        messagebox.showerror(f"{title} 오류", f"{title} 중 오류가 발생했습니다:\n\n{exc}")


def run_cli(args: argparse.Namespace) -> int:
    printer = LWC610Printer(port=args.port)

    if args.status:
        st = printer.get_status()
        print(f"=== Epson LW-C610 ({args.port}) 상태 ===")
        print(f"  동작 상태 : {st.status_text} (ST=0x{st.st:02X})")
        print(f"  오류 상태 : {st.error_text} (ER=0x{st.er:02X})")
        print(f"  장착 테이프: {st.tape_label} (TW=0x{st.tw_raw:02X}, 인쇄 가능 높이={st.printable_dots} dots)")
        print(f"  기타 코드 : TR=0x{st.tr_raw:02X}, IR=0x{st.ir_raw:02X}, TC=0x{st.tc_raw:02X}, RC=0x{st.rc_raw:02X}")
        return 0

    if args.feed:
        print(f"{args.port} 테이프 피드 실행 중 (cut={not args.no_cut})...")
        st = printer.feed_tape(cut=not args.no_cut)
        print(f"완료: {st.status_text} / {st.error_text}")
        return 0

    # Render label bitmap if text, line2, qr, or image is specified
    if args.text or args.line2 or args.qr or args.image or args.save_preview:
        tape_code = args.tape_code
        if tape_code == 0 and not args.offline:
            try:
                live_st = printer.get_status()
                if live_st.tape_code in TAPE_PRINTABLE_DOTS:
                    tape_code = live_st.tape_code
                    print(f"감지된 테이프: {live_st.tape_label}")
            except Exception as e:
                print(f"프린터 상태 자동 조회 생략 ({e}) -> 기본 12mm 적용")
        if tape_code not in TAPE_PRINTABLE_DOTS:
            tape_code = 4

        full_text = (args.text or "").replace("\\n", "\n")
        if args.line2:
            full_text = f"{full_text}\n{args.line2}" if full_text else args.line2

        ratio_map = {
            "1:1": LINE_RATIO_MODES[0],
            "6:4": LINE_RATIO_MODES[1],
            "4:6": LINE_RATIO_MODES[2],
        }

        design = LabelDesign(
            text=full_text,
            font_name=args.font,
            auto_font_size=(args.font_size <= 0),
            font_size_px=args.font_size if args.font_size > 0 else 84,
            bold=not args.no_bold,
            invert_colors=args.invert,
            h_align=args.align,
            frame_style=args.frame,
            line_ratio_mode=ratio_map.get(args.line_ratio, LINE_RATIO_MODES[0]),
            line_gap_dots=args.line_gap,
            line_divider=args.line_divider,
            qr_text=args.qr or "",
            qr_position=args.qr_pos,
            image_path=args.image or "",
            tape_code=tape_code,
            margin_mm=args.margin,
            lead_offset_mm=args.lead_offset,
        )
        bmp = render_label_bitmap(design)
        prev = render_preview_image(
            bmp,
            tape_code=tape_code,
            margin_mm=args.margin,
            lead_offset_mm=args.lead_offset,
        )

        if args.save_preview:
            prev.save(args.save_preview)
            print(f"미리보기 저장됨: {args.save_preview} ({bmp.width}x{bmp.height} printable dots)")
            if args.offline:
                return 0

        if not args.offline:
            print(
                f"{args.port}로 라벨 인쇄 중 ({bmp.width}x{bmp.height} dots, 여백 {args.margin}mm + 좌측보정 {args.lead_offset}mm, 매수 {args.copies})..."
            )
            st = printer.print_bitmap(
                label_image=bmp,
                cut=not args.no_cut,
                density=args.density,
                margin_mm=args.margin,
                lead_offset_mm=args.lead_offset,
                copies=args.copies,
                progress_callback=lambda f, m: print(f"  [{int(f*100):3d}%] {m}"),
            )
            print(f"인쇄 완료! 상태: {st.status_text} ({st.error_text})")
        return 0

    return -1


def main() -> None:
    parser = argparse.ArgumentParser(description="Epson LW-C610 (LW-C610@a6db48) PC 라벨프린터 프로그램")
    parser.add_argument("--port", default="COM4", help="블루투스 시리얼 포트 (기본값: COM4)")
    parser.add_argument("--status", action="store_true", help="프린터 실시간 상태 및 테이프 폭 조회")
    parser.add_argument("--feed", action="store_true", help="테이프 피드 (& 커트) 실행")
    parser.add_argument("--text", type=str, default="", help="인쇄할 라벨 텍스트 (1째 줄 또는 \\n 포함 텍스트)")
    parser.add_argument("--line2", type=str, default="", help="2째 줄(하단)에 인쇄할 라벨 텍스트")
    parser.add_argument("--line-ratio", choices=["1:1", "6:4", "4:6"], default="1:1", help="2줄 크기 비율 (1:1=동일, 6:4=1줄크게, 4:6=2줄크게)")
    parser.add_argument("--line-gap", type=int, default=8, help="줄 간격 dots (기본값: 8)")
    parser.add_argument("--line-divider", action="store_true", help="줄 사이 가로 구분선 표시")
    parser.add_argument("--qr", type=str, default="", help="인쇄할 QR 코드 데이터/URL")
    parser.add_argument("--qr-pos", choices=["left", "right", "only"], default="left", help="QR 코드 위치")
    parser.add_argument("--image", type=str, default="", help="인쇄할 이미지 경로")
    parser.add_argument("--font", type=str, default="맑은 고딕 (Malgun Gothic)", help="글꼴 이름")
    parser.add_argument("--font-size", type=int, default=0, help="글꼴 크기 (0=자동 맞춤)")
    parser.add_argument("--no-bold", action="store_true", help="굵게(Bold) 해제")
    parser.add_argument("--invert", action="store_true", help="흑백 반전 인쇄")
    parser.add_argument("--align", choices=["left", "center", "right"], default="center", help="텍스트 정렬")
    parser.add_argument("--frame", type=str, default="없음 (None)", help="테두리 스타일")
    parser.add_argument("--tape-code", type=int, default=0, help="테이프 코드 (0=자동, 1=4mm, 2=6mm, 3=9mm, 4=12mm, 5=18mm, 6=24mm)")
    parser.add_argument("--margin", type=float, default=0.0, help="기본 좌우 여백 mm (0.0~21.0, 기본 0.0)")
    parser.add_argument("--lead-offset", type=float, default=0.0, help="좌측 시작 잘림 방지 추가 여백 mm (기본 0.0)")
    parser.add_argument("--density", type=int, default=0, help="인쇄 농도 (-5 ~ +5)")
    parser.add_argument("--copies", type=int, default=1, help="인쇄 매수 (1~99)")
    parser.add_argument("--no-cut", action="store_true", help="자동 커팅 비활성화")
    parser.add_argument("--save-preview", type=str, default="", help="미리보기 PNG 파일 저장 경로")
    parser.add_argument("--offline", action="store_true", help="프린터 전송 없이 미리보기만 생성")

    args = parser.parse_args()

    # Run CLI if any CLI action flag was passed
    if any([args.status, args.feed, args.text, args.line2, args.qr, args.image, args.save_preview]):
        rc = run_cli(args)
        if rc >= 0:
            sys.exit(rc)

    # Otherwise launch Desktop GUI
    root = tk.Tk()
    app = LWC610App(root, initial_port=args.port)
    root.mainloop()


if __name__ == "__main__":
    main()
