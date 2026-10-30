# auction_reader

법원경매정보 물건을 조회하고, 지역·유찰 횟수·최저매각가·할인율·면적 기준으로 필터링하는 **Python + Gradio** 웹 UI입니다. 선택한 물건의 소재지를 카카오 지도 API와 연결하고 국토교통부 아파트 매매 실거래가와 비교할 수 있습니다.

## 기능

- 법원경매정보 비공식 웹 검색 API 조회, JSON/CSV/ZIP 불러오기 및 내보내기
- 서울·경기 및 주소 키워드, 유찰 횟수, 최저매각가(억 원), 할인율, 면적(㎡/평) 필터
- 사건 선택 시 경매 물건 상세 요약
- 선택 물건 카카오 지도 좌표 조회 및 국토교통부 아파트 매매 실거래가 비교
- 외부 API 키 없이도 기본 경매 검색·필터 사용 가능

## 로컬 실행

Python 3.10 이상 권장:

```bash
git clone https://github.com/6democratickim9/auction_reader.git
cd auction_reader
python -m pip install -r requirements.txt
python courtauction_insights.py
```

Gradio에서 출력하는 로컬 URL(일반적으로 `http://127.0.0.1:7860`)을 브라우저에서 엽니다.

## Google Colab에서 사용

로컬 실행이 어려우면 [Google Colab](https://colab.research.google.com/)에서 기존의 통합 `.ipynb` 노트북을 업로드해 실행하세요. 저장소의 `courtauction_insights.py`를 Colab에 업로드해서 사용할 수도 있습니다. Gradio의 공유 링크를 사용할 경우 **URL을 아는 사람이 접근할 수 있습니다**.

## API 키 (선택)

- Kakao REST API 키: https://developers.kakao.com/docs/ko/local/dev-guide
- 국토교통부 아파트 매매 실거래가 API 키: https://www.data.go.kr/data/15126469/openapi.do

웹 UI의 비밀번호 입력칸에 키를 입력하거나 다음 환경변수를 설정할 수 있습니다:

- `KAKAO_REST_API_KEY`
- `DATA_GO_KR_SERVICE_KEY`

키는 GitHub 저장소에 올리지 마세요.

## 주의사항

- 법원경매정보의 **공식 공개 API가 아니라 사이트 내부 검색 엔드포인트**를 호출합니다. 사이트 구조 변경·접근 제한으로 조회가 실패할 수 있습니다. 실제 운영 서버 연동은 검증되지 않았습니다.
- 필터링은 **다운로드한 페이지의 결과에만** 적용됩니다. 서울 전체 경매 물건의 누락 없는 조회를 보장하지 않습니다.
- 매각 면적은 전용면적과 다를 수 있으며, 실거래가 비교에서는 동일 단지·유사 면적 여부를 확인해야 합니다.
- 감정가 대비 할인율과 최근 실거래가 대비 가격차는 서로 다른 지표입니다.
- 매각되는 권리 범위·대지권·인수권리 등은 **매각물건명세서·등기사항증명서**로 별도 확인해야 합니다.
