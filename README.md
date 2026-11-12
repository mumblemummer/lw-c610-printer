# Epson LW-C610 PC 라벨프린터 스튜디오 (`lw-c610-printer`)

스마트폰 전용으로 출시되어 공식 Windows 드라이버가 없는 **Epson LabelWorks LW-C610** 블루투스 라벨프린터를 PC에서 바로 사용할 수 있도록 공식 Android SDK(`com.epson.ilabel` v1.8.0 / `classes2.dex` / `libLWPrint.so`)를 리버스 엔지니어링하여 제작한 전용 데스크톱(GUI) 및 명령줄(CLI) 프로그램입니다.

---

## 주요 기능

- **블루투스 시리얼(SPP) 자동 검색 및 실시간 상태 조회**:
  - `LW-C610` 포트 자동 탐색 및 실시간 상태(`Idle`, `Printing`, 오류 여부)와 장착된 테이프 폭(4mm / 6mm / 9mm / 12mm / 18mm / 24mm) 자동 감지
- **72종+ 큐레이션 글꼴 라이브러리 및 PC 전체 글꼴 탐색**:
  - 한글 고딕/기본, 한글 굵은/타이틀(배민도현/주아/검은고딕/가석체), 한글 명조/궁서, 손글씨/감성(연성체/개구쟁이/동글체/나눔손글씨), 영문/숫자 디자인 폰트 내장
  - 스타일별 카테고리 필터 및 1-클릭 글꼴 이전/다음 탐색 버튼, 외부 폰트 파일(`.ttf`, `.otf`, `.ttc`) 직접 로더
- **전용 2줄 라벨 입력 및 비율/구분선 지원**:
  - 1째 줄 / 2째 줄 전용 입력창 및 다중 행 양방향 동기화
  - 2줄 비율 조절(`1:1`, `6:4 제목+부제`, `4:6 부제+본문`), 줄 간격(dots) 및 줄 사이 가로 구분선 지원
- **CSV 데이터 연동 및 다중 일괄 출력 (객체 위치 자유 배치)**:
  - CSV 파일을 불러와 각 열(Column)을 라벨 위의 원하는 X/Y (mm) 좌표에 객체(텍스트, QR, 구분선, 박스)로 자유 배치
  - 행별 실시간 360 DPI 미리보기 넘겨보기 및 1-클릭 전체 행 자동 순차 인쇄
- **테두리(Frame) · QR 코드 · 이미지 삽입**:
  - 직사각형, 둥근 사각형, 굵은 테두리, 캡슐형 프레임 지원
  - QR 코드 자동 생성 및 배치(좌측 / 우측 / 단독)
  - 사용자 이미지(PNG/JPG/BMP/GIF) 삽입 및 Floyd-Steinberg 디더링 지원
- **여백 및 인쇄 옵션 정밀 제어**:
  - 기본 여백(`0.0 mm` 기본값) 및 좌측 보정(`0.0 mm` 기본값) 세밀 조절
  - 인쇄 농도(`-5` ~ `+5`), 자동 커팅(Auto Cut), 다중 매수(`1`~`99`매), 테이프 피드 & 커트 제어

---

## 1. 설치 및 실행 방법

### 필수 패키지 설치
```powershell
pip install -r requirements.txt
```

### GUI 데스크톱 프로그램 실행
`run_lwc610.bat` 파일을 더블클릭하거나 터미널에서 아래 명령어를 실행합니다:
```powershell
python app.py
```

### CLI (명령줄) 모드 사용법
- **실시간 프린터 상태 및 장착 테이프 확인**:
  ```powershell
  python app.py --status
  ```
- **텍스트 라벨 바로 인쇄**:
  ```powershell
  python app.py --text "안녕하세요 LW-C610" --frame "둥근 사각형 (Rounded)"
  ```
- **QR 코드 + 텍스트 라벨 인쇄**:
  ```powershell
  python app.py --text "Epson LW-C610\n스캔 확인" --qr "https://epson.co.kr"
  ```
- **테이프 피드 & 커트**:
  ```powershell
  python app.py --feed
  ```

---

## 2. 리버스 엔지니어링 프로토콜 명세 (`LW-C610`)

- **분석 아티팩트**:
  - `epson_ilabel.apk` (`com.epson.ilabel` v1.8.0, `sha256: bffeb89371b69b7128dc25588b123ed13876f1a83031ffc2a39ef0c06ce0836f`)
  - `lib/x86_64/libLWPrint.so` (`sha256: 7e7900c2871670ecc31f414be132c366d6d9f89b7620cd4838b3bf64aa80a810`)
- **모델 프로파일 (`LWPrintProductInformation`)**:
  - `Model Name`: `"LW-C610"`
  - `Model Number`: **`1301`** (`PrintCoreModelNumberLWC610`)
  - `Resolution`: **`360` DPI** (`14.173 dots/mm`)
  - `Command Level`: **`5`** (`ProductCommandLevel5`)
  - `Status Length`: **`78` bytes** (`AccessManagerBluetooth2` — 13바이트 `@BDC ST2\r\nP\x00;` 헤더 + 78바이트 상태 페이로드 = **총 91바이트**)
  - `Halfcut`: `"NO"`, `LowSpeed`: `"NO"`
- **360 DPI 테이프 폭별 인쇄 가능 도트 수 (`getPrintableSize`)**:
  | 테이프 코드 | 상태 `TW` 바이트 | 테이프 폭 | 인쇄 가능 높이 (dots) | 1줄당 래스터 바이트 |
  | :---: | :---: | :---: | :---: | :---: |
  | `1` | `0x0B` / `0x5B` | **4 mm** | **44 dots** | 6 bytes |
  | `2` | `0x01` / `0x51` | **6 mm** | **72 dots** | 9 bytes |
  | `3` | `0x02` / `0x52` | **9 mm** | **108 dots** | 14 bytes |
  | `4` | `0x03` / `0x53` | **12 mm** | **144 dots** | 18 bytes |
  | `5` | `0x04` / `0x54` | **18 mm** | **216 dots** | 27 bytes |
  | `6` | `0x05` / `0x55` | **24 mm** | **256 dots** *(모델 1301 전용 예외값)* | 32 bytes |

- **패킷 프레이밍 (`CommandFilter::makeCommand` @ `0x2f0e0`)**:
  - 포맷: `1B 7B [len] [cmd] [payload...] [checksum] 7D`
  - `len = len(payload) + 3`, `checksum = (cmd + sum(payload)) & 0xFF`
- **주요 제어 명령**:
  - 상태 요청 (`Request Status`): `1B 7B 05 49 00 00 49 7D`
  - 인쇄 중 상태 활성화 (`Set Request Status`): `1B 7B 05 49 05 00 4E 7D`
  - 테이프 피드 & 커트 (`Feed & Cut`): `1B 7B 04 2B 01 2C 7D`
  - 테이프 피드만 (`Feed Only`): `1B 7B 04 2B 00 2B 7D`
  - 작업 환경 설정 (`makeJobEnvironmentCommand`):
    - `'@'`: `1B 7B 03 40 40 7D` (초기화)
    - `'{'`: `1B 7B 07 7B 00 00 53 54 22 7D` (프로토콜 모드)
    - `'C'`: `1B 7B 07 43 01 01 01 01 47 7D` (자동 커팅) / `1B 7B 07 43 00 00 00 00 43 7D` (커팅 안 함)
    - `'D'`: `1B 7B 04 44 [density+5] [chk] 7D` (농도 `0..10`, 기본값 `5`)
    - `'G'`: `1B 7B 03 47 47 7D`
  - 페이지 환경 설정 (`makePageEnvironmentCommand`, Level 5):
    - `'L'`: 32-bit LE 전체 페이지 길이 (dots)
    - `'T'`: 16-bit LE 여백 (dots)
  - 래스터 라인 전송 (`makeRasterCommand` + `bitmapToRaster`):
    `1B 2E 00 00 00 01 [dots_low] [dots_high] [MSB-first 1bpp raster bytes...]`
  - 페이지 종료: `0x0C` (Form Feed)
  - 인쇄 작업 종료: `1B 7B 03 40 40 7D`
