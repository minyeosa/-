import io
import os
import re
import textwrap
import zipfile

import streamlit as st
from PIL import Image, ImageDraw, ImageFont
from google import genai
from google.genai import types

# ─────────────────────────────────────────────
# 기본값
# ─────────────────────────────────────────────

DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image-preview"
DEFAULT_TEXT_MODEL = "gemini-2.0-flash"

DEFAULT_PROMPT_FORMAT = (
    "Upgraded stick-man 2D with thick black outline, pure white faces, "
    "single hard cel shading, thicker torso and neck, flat matte colors; "
    "SCENE: [장면 묘사 영문], no text or letters"
)

SYSTEM_INSTRUCTION = """\
당신은 '2D 스틱맨 애니메이션 전문 프롬프트 디렉터'입니다.

[스타일 가이드]
캐릭터: Pure-white round faces, single hard cel shading(턱 아래 1단 그림자),
        thick black outline, thicker torso and neck, stick limbs, flat matte colors.
배경  : 저채도 평면 블록(Low saturation flat blocks), 글자 절대 금지.
금지  : 3D, photoreal, gradient, soft light, text, letters, speech bubble.

[장면 해석 원칙]
- 감정은 눈썹/입선으로, 동작은 명확한 동사(leans, points, nods, clasps, gestures)로 표현.
- 추상 개념 시각화:
    상승/하락   → 화살표 아이콘
    데이터/실적 → 차트 도형, 기어, 지도 핀
    계약/문서   → 빈 종이 아이콘
  모든 간판·화면·문서에 글자(text) 대신 기호/도형만 사용.

[출력 규칙]
- 반드시 아래 형식으로만 출력. 설명·주석 일절 금지.
- 형식: "Upgraded stick-man 2D with thick black outline, pure white faces, \
single hard cel shading, thicker torso and neck, flat matte colors; \
SCENE: [행동 및 아이콘 묘사(영문)], no text or letters"
"""

# 자막 언어 옵션
LANG_OPTIONS = ["언어없음", "한국어", "일본어", "영어"]

# 자막 폰트 경로 (Windows 기본 폰트)
FONT_MAP = {
    "한국어": [
        "C:/Windows/Fonts/malgun.ttf",       # Malgun Gothic
        "C:/Windows/Fonts/gulim.ttc",
    ],
    "일본어": [
        "C:/Windows/Fonts/msgothic.ttc",      # MS Gothic
        "C:/Windows/Fonts/meiryo.ttc",
        "C:/Windows/Fonts/YuGothM.ttc",
    ],
    "영어": [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
    ],
}


# ─────────────────────────────────────────────
# 페이지 설정
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="이미지 생성기",
    page_icon="🎨",
    layout="wide",
)

st.title("🎨 이미지 생성기")
st.caption("대본 → 분석 → 분할 → 프롬프트 → 이미지 자동 생성 (나노바나나2 기반)")


# ─────────────────────────────────────────────
# 사이드바
# ─────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ 설정")
    api_key = st.text_input("🔑 Gemini API Key", type="password", placeholder="AIza...")
    image_model = st.text_input("🖼 이미지 모델 (나노바나나2)", value=DEFAULT_IMAGE_MODEL)
    text_model = st.text_input("📝 텍스트 모델", value=DEFAULT_TEXT_MODEL)
    st.divider()
    st.markdown("**필요 패키지**")
    st.code("pip install streamlit google-genai Pillow", language="bash")


# ─────────────────────────────────────────────
# 입력 영역
# ─────────────────────────────────────────────

left, right = st.columns([1, 1], gap="large")

with left:
    st.subheader("📝 대본 입력")
    script = st.text_area(
        "대본을 붙여넣으세요",
        height=200,
        placeholder="여기에 대본을 입력하세요...",
        label_visibility="collapsed",
    )

    # 컷당 시간: 5~30초, 5초 단위
    seconds_per_cut = st.select_slider(
        "⏱ 컷당 시간 (초)",
        options=[5, 10, 15, 20, 25, 30],
        value=5,
    )

    # 이미지 자막 언어
    subtitle_lang = st.selectbox(
        "🌐 이미지 자막 언어",
        options=LANG_OPTIONS,
        index=0,
        help="이미지 하단에 자막을 합성합니다. '언어없음'은 자막 없이 생성됩니다.",
    )

with right:
    st.subheader("🎨 이미지 프롬프트 형식")
    prompt_format = st.text_area(
        "프롬프트 형식 (기본값 = 스틱맨 스타일가이드)",
        value=DEFAULT_PROMPT_FORMAT,
        height=260,
        help="이미지 생성 시 따를 프롬프트 형식입니다. [장면 묘사 영문] 부분이 자동 채워집니다.",
        label_visibility="collapsed",
    )

# 시작 버튼
ready = bool(api_key and script.strip())
if not api_key:
    st.warning("사이드바에서 Gemini API Key를 입력해주세요.")
elif not script.strip():
    st.info("대본을 입력해주세요.")

generate = st.button(
    "▶ 생성 시작",
    type="primary",
    use_container_width=True,
    disabled=not ready,
)


# ─────────────────────────────────────────────
# 헬퍼 함수
# ─────────────────────────────────────────────

def parse_numbered_list(text: str) -> list[str]:
    """'1. 텍스트' 형태의 줄을 파싱해 리스트로 반환."""
    items = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^\d+[.)]\s*\"?(.+?)\"?\s*$", line)
        if m:
            items.append(m.group(1).strip())
    return items


def parse_prompts(text: str, expected: int) -> list[str]:
    """번호별 프롬프트 파싱. 실패하면 줄 단위 분리로 폴백."""
    items = parse_numbered_list(text)
    if len(items) >= expected:
        return items[:expected]

    items = []
    buf: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"^\d+[.)]", line):
            if buf:
                items.append(" ".join(buf).strip(' "'))
            buf = [re.sub(r"^\d+[.)]\s*", "", line).strip(' "')]
        elif line:
            buf.append(line.strip(' "'))
    if buf:
        items.append(" ".join(buf).strip(' "'))

    return items if items else [text.strip()]


def get_font(lang: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """언어에 맞는 PIL 폰트 반환. 없으면 기본 폰트."""
    for path in FONT_MAP.get(lang, []):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def add_subtitle(img_bytes: bytes, text: str, lang: str) -> bytes:
    """이미지 하단에 반투명 자막 바를 합성한 PNG bytes 반환."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    w, h = img.size

    font_size = max(20, h // 22)
    font = get_font(lang, font_size)

    # 텍스트 줄 나누기 (최대 너비 기준)
    max_chars = max(10, w // (font_size // 2 + 2))
    lines = textwrap.wrap(text, width=max_chars) or [text]

    line_h = font_size + 6
    bar_h = line_h * len(lines) + 16
    padding = 8

    # 반투명 검정 바
    overlay = Image.new("RGBA", (w, bar_h), (0, 0, 0, 180))
    img.paste(overlay, (0, h - bar_h), overlay)

    draw = ImageDraw.Draw(img)
    y = h - bar_h + padding
    for line in lines:
        try:
            bbox = draw.textbbox((0, 0), line, font=font)
            text_w = bbox[2] - bbox[0]
        except AttributeError:
            text_w = draw.textlength(line, font=font)
        x = (w - text_w) // 2
        # 그림자
        draw.text((x + 1, y + 1), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────
# 파이프라인
# ─────────────────────────────────────────────

if generate and ready:
    client = genai.Client(api_key=api_key)

    # ── Step 1: 대본 분석 ──────────────────────
    st.divider()
    with st.expander("✅ Step 1 — 대본 분석", expanded=True):
        with st.spinner("대본을 분석하는 중..."):
            try:
                r = client.models.generate_content(
                    model=text_model,
                    contents=(
                        "다음 대본을 분석해서 핵심 주제, 등장 캐릭터, "
                        "주요 장면, 전체 감정 톤을 간략히 정리해주세요.\n\n"
                        f"대본:\n{script}"
                    ),
                )
                st.markdown(r.text)
            except Exception as e:
                st.error(f"오류: {e}")
                st.stop()

    # ── Step 2: 초 단위 분할 ───────────────────
    with st.expander("✅ Step 2 — 초 단위 분할", expanded=True):
        with st.spinner("대본을 분할하는 중..."):
            try:
                chars_per_cut = int(seconds_per_cut * 4.5)
                r = client.models.generate_content(
                    model=text_model,
                    contents=(
                        f"다음 대본을 한 컷당 {seconds_per_cut}초 기준으로 분할하세요.\n"
                        f"한국어 1초 ≈ 4~5글자(공백 포함) → 컷당 약 {chars_per_cut}글자.\n"
                        "자연스러운 의미 단위로 자르고 '1. 텍스트' 형식으로만 출력하세요.\n"
                        "번호와 텍스트 외 설명은 일절 출력하지 마세요.\n\n"
                        f"대본:\n{script}"
                    ),
                )
                segments = parse_numbered_list(r.text.strip())
                if not segments:
                    segments = [l.strip() for l in r.text.splitlines() if l.strip()]

                for i, seg in enumerate(segments, 1):
                    st.write(f"**{i}.** {seg}")
                st.success(f"총 {len(segments)}개 컷으로 분할 완료")
            except Exception as e:
                st.error(f"오류: {e}")
                st.stop()

    # ── 자막 텍스트 번역 (한국어 외 선택 시) ──
    subtitle_texts: list[str] = []
    if subtitle_lang == "언어없음":
        subtitle_texts = [""] * len(segments)
    elif subtitle_lang == "한국어":
        subtitle_texts = segments[:]
    else:
        lang_name = {"일본어": "Japanese", "영어": "English"}.get(subtitle_lang, subtitle_lang)
        with st.spinner(f"자막 번역 중 ({subtitle_lang})..."):
            try:
                segments_joined = "\n".join(f"{i+1}. {s}" for i, s in enumerate(segments))
                r = client.models.generate_content(
                    model=text_model,
                    contents=(
                        f"아래 번호별 한국어 텍스트를 {lang_name}로 번역하세요.\n"
                        "'1. 번역문' 형식으로만 출력하세요. 설명 금지.\n\n"
                        f"{segments_joined}"
                    ),
                )
                subtitle_texts = parse_numbered_list(r.text.strip())
                if len(subtitle_texts) < len(segments):
                    # 부족하면 원본으로 채움
                    subtitle_texts += segments[len(subtitle_texts):]
            except Exception as e:
                st.warning(f"번역 오류({e}), 한국어 원문으로 대체합니다.")
                subtitle_texts = segments[:]

    # ── Step 3: 이미지 프롬프트 생성 ──────────
    with st.expander("✅ Step 3 — 이미지 프롬프트 생성", expanded=True):
        with st.spinner("이미지 프롬프트를 생성하는 중..."):
            try:
                segments_text = "\n".join(
                    f"{i+1}. {s}" for i, s in enumerate(segments)
                )
                r = client.models.generate_content(
                    model=text_model,
                    contents=(
                        f"{SYSTEM_INSTRUCTION}\n\n"
                        f"[사용자 지정 프롬프트 형식]\n{prompt_format}\n\n"
                        "위 스타일 가이드와 형식을 따라 아래 각 장면의 영문 이미지 프롬프트를 작성하세요.\n"
                        "'1. 프롬프트' 형식으로만 출력하세요. 설명·주석 금지.\n\n"
                        f"[장면 목록]\n{segments_text}"
                    ),
                )
                prompts = parse_prompts(r.text.strip(), len(segments))

                for i, p in enumerate(prompts, 1):
                    st.markdown(f"**컷 {i}**")
                    st.code(p, language="text")
                st.success(f"총 {len(prompts)}개 프롬프트 생성 완료")
            except Exception as e:
                st.error(f"오류: {e}")
                st.stop()

    # ── Step 4: 이미지 생성 ────────────────────
    with st.expander("🖼 Step 4 — 이미지 생성", expanded=True):
        n = len(prompts)
        progress = st.progress(0, text="이미지 생성 준비 중...")
        generated: list[tuple[int, bytes, str]] = []  # (번호, bytes, 세그먼트 텍스트)

        cols = st.columns(3)

        for i, prompt in enumerate(prompts):
            progress.progress(i / n, text=f"이미지 생성 중... ({i+1}/{n})")
            seg_text = segments[i] if i < len(segments) else ""
            sub_text = subtitle_texts[i] if i < len(subtitle_texts) else ""

            try:
                img_response = client.models.generate_content(
                    model=image_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                    ),
                )

                img_data: bytes | None = None
                for part in img_response.candidates[0].content.parts:
                    if part.inline_data is not None:
                        img_data = part.inline_data.data
                        break

                if img_data:
                    # 자막 합성
                    if subtitle_lang != "언어없음" and sub_text:
                        img_data = add_subtitle(img_data, sub_text, subtitle_lang)

                    generated.append((i + 1, img_data, seg_text))
                    with cols[i % 3]:
                        pil_img = Image.open(io.BytesIO(img_data))
                        caption = f"컷 {i+1}  {seg_text[:28]}{'…' if len(seg_text) > 28 else ''}"
                        st.image(pil_img, caption=caption, use_container_width=True)
                        st.download_button(
                            label=f"⬇ 컷 {i+1} 저장",
                            data=img_data,
                            file_name=f"cut_{i+1:03d}.png",
                            mime="image/png",
                            key=f"dl_img_{i}",
                            use_container_width=True,
                        )
                else:
                    st.warning(f"컷 {i+1}: 이미지 데이터를 받지 못했습니다.")

            except Exception as e:
                st.error(f"컷 {i+1} 생성 오류: {e}")

        progress.progress(1.0, text="이미지 생성 완료!")

        # ── ZIP 다운로드 ──
        if generated:
            st.divider()
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for num, data, _ in generated:
                    zf.writestr(f"cut_{num:03d}.png", data)
            zip_buf.seek(0)

            st.download_button(
                label=f"📦 전체 이미지 ZIP 다운로드 ({len(generated)}장)",
                data=zip_buf.getvalue(),
                file_name="generated_images.zip",
                mime="application/zip",
                use_container_width=True,
                type="primary",
            )
