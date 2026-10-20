# iff-pdf-refine

PDF를 페이지별 Markdown으로 변환하는 HTTP 서비스입니다. [PyMuPDF4LLM](https://github.com/pymupdf/pymupdf4llm)의 레이아웃 분석으로 읽는 순서를 복원하고, 제목과 목록 구조를 유지하며, 반복되는 머리글과 바닥글을 제외합니다. 레이아웃 분석이 찾은 그림(도표, 차트, 사진)은 JPEG 이미지로 잘라 함께 반환합니다.

## 요구 사항

- Python 3.11 이상
- GPU는 필요하지 않습니다. CPU에서 쪽당 약 0.5~1초가 걸립니다.

## 실행

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
PDF_REFINE_KEY=<secret> python app.py
```

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `PDF_REFINE_KEY` | (필수) | 요청 인증에 쓰는 키 |
| `PDF_REFINE_PORT` | `8790` | 수신 포트 |
| `PDF_REFINE_WORKERS` | `2` | 동시에 처리하는 PDF 수. PDF마다 별도 프로세스를 씁니다 |

## API

### `POST /refine`

요청 본문은 PDF 바이너리이고, `Authorization: Bearer <PDF_REFINE_KEY>` 헤더가 필요합니다.

```json
{
  "pages": ["# 제목\n- 항목", "..."],
  "figures": [{ "page": 0, "image": "data:image/jpeg;base64,..." }, { "page": 1, "image": "data:image/jpeg;base64,...", "whole": true }],
  "seconds": 3.47
}
```

| 상태 코드 | 의미 |
|---|---|
| `200` | 성공 |
| `400` | PDF가 아닙니다 |
| `401` | 인증에 실패했습니다 |
| `413` | 본문이 비었거나 60MB를 넘습니다 |
| `422` | PDF를 읽을 수 없습니다(암호화 등) |
| `504` | 600초 안에 끝나지 않았습니다 |

### `GET /health`

`{"ok": true}`를 반환합니다.

## 출력 형식

- 입력 PDF의 쪽마다 문자열 하나를 반환하며, 배열 길이는 쪽 수와 같습니다.
- HTML 태그와 빈 줄을 제거하고, 위첨자는 `^`, 아래첨자는 `_`로 표기합니다.
- OCR은 수행하지 않습니다. 텍스트 레이어가 없는 쪽은 빈 문자열이 됩니다.
- `figures`는 쪽 순서대로이며, `page`는 0부터 센 쪽 번호입니다. 각 그림은 둘레 12pt를 포함해 잘라내고, 긴 변 1024px 이하의 JPEG(품질 75)로 만듭니다.
- 쪽 면적의 3%보다 작거나 한 변이 48pt보다 짧은 그림(아이콘, 로고)과, 여러 쪽에 똑같이 나오는 그림(템플릿 장식)은 제외합니다. 쪽마다 큰 것 2개, 문서 전체에서 40개까지 반환합니다.
- 이렇게 빠지는 그림(쪽마다 큰 2개를 넘는 것, 너무 작아 제외되는 것)이 쪽 면적의 15% 이상을 덮으면, 그 쪽은 그림을 잘라내지 않고 쪽 전체를 긴 변 1536px 이하의 JPEG 한 장으로 반환하며 `"whole": true`를 붙입니다. 수식이나 풀이를 이미지로 붙인 유인물처럼, 그림에만 있는 내용이 많은 쪽을 위한 것입니다. 쪽의 절반 이상을 덮는 배경 그림은 셈에 넣지 않습니다.
- 그림을 잘라내다 오류가 나면 `figures`는 빈 배열이 되고, 텍스트는 그대로 반환합니다.
- 첫 요청은 레이아웃 모델을 불러오느라 몇 초 더 걸립니다.

## 라이선스

이 프로젝트는 [GNU AGPL-3.0](LICENSE)을 따릅니다. 의존하는 PyMuPDF, PyMuPDF4LLM, PyMuPDF Layout(Artifex)이 AGPL-3.0으로 배포되기 때문입니다.
