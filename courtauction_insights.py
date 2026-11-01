#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""법원경매정보 간편 조회 UI (Gradio). Colab / Python에서 실행 가능.

검색 API 비공식 요청이며 호환을 보장할 수 없습니다. 지역·가격·유찰·면적 필터는
현재 내려받은 결과에만 적용됩니다. 면적은 출처/구분을 함께 표시합니다.
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import hashlib
import re
import tempfile
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import gradio as gr
import pandas as pd
import requests

BASE = "https://www.courtauction.go.kr"
ENTRY = BASE + "/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml&pgjId=151F00"
SEARCH = BASE + "/pgj/pgjsearch/searchControllerMain.on"
SEARCH_PAGE = "https://www.courtauction.go.kr/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml#"
OUTPUT_DIR = Path(tempfile.gettempdir()) / "courtauction_gradio_output"
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)

RESULT_COLUMNS = [
    "소재지", "법원", "사건번호", "물건번호", "매각기일", "물건용도",
    "면적_㎡", "면적_평", "면적기준", "유찰횟수",
    "감정가_원", "최저매각가_원", "매각가율_%", "할인율_%", "지분매각", "매각지분",
]
EXPORT_COLUMNS = RESULT_COLUMNS + ["매각지분_원문", "지분판단_근거",
    "시도", "주소_도로명", "주소_지번", "면적출처", "물건비고", "물건설명",
    "상태코드", "전체소유권_확정여부", "권리인수_검토", "saNo",
]

# 기존 Colab 크롤러와 동일한 검색 요청 필드 (현재 API 호환 보장 불가).
SEARCH_FIELDS = [
    "rletDspslSpcCondCd", "rprsAdongSdCd", "rprsAdongSggCd", "rprsAdongEmdCd",
    "rdnmSdCd", "rdnmSggCd", "rdnmNo", "mvprpDspslPlcAdongSdCd",
    "mvprpDspslPlcAdongSggCd", "mvprpDspslPlcAdongEmdCd", "rdDspslPlcAdongSdCd",
    "rdDspslPlcAdongSggCd", "rdDspslPlcAdongEmdCd", "jdbnCd", "execrOfcDvsCd",
    "lclDspslGdsLstUsgCd", "mclDspslGdsLstUsgCd", "sclDspslGdsLstUsgCd",
    "cortAuctnMbrsId", "aeeEvlAmtMin", "aeeEvlAmtMax", "lwsDspslPrcRateMin",
    "lwsDspslPrcRateMax", "flbdNcntMin", "flbdNcntMax", "objctArDtsMin",
    "objctArDtsMax", "mvprpArtclKndCd", "mvprpArtclNm", "mvprpAtchmPlcTypCd",
    "lafjOrderBy", "csNo", "dspslDxdyYmd", "fstDspslHm", "scndDspslHm",
    "thrdDspslHm", "fothDspslHm", "dspslPlcNm", "lwsDspslPrcMin",
    "lwsDspslPrcMax", "grbxTypCd", "gdsVendNm", "fuelKndCd", "carMdyrMax",
    "carMdyrMin", "carMdlNm", "sideDvsCd"
]


def clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    value = html.unescape(str(value))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value)).strip()


def first(item: dict, *keys: str) -> str:
    return next((clean(item.get(k)) for k in keys if clean(item.get(k))), "")


def numeric(value) -> int | None:
    if value in (None, ""):
        return None
    text = clean(value).replace(",", "").replace("원", "").strip()
    return int(text) if re.fullmatch(r"\d+", text) else None


def decimal_number(value) -> float | None:
    """부동소수 면적 및 CSV의 숫자를 읽되, 단위 등이 혼합된 값은 무시한다."""
    if value is None or isinstance(value, bool):
        return None
    raw = clean(value).replace(",", "")
    raw = re.sub(r"\s*(?:㎡|m²|m2|제곱미터|평)\s*$", "", raw, flags=re.I)
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return None
    return float(raw)


def area_info(item: dict) -> tuple[float | None, str, str]:
    """원본 목록에서 면적 1개를 참고값으로 찾는다.

    집합건물의 전용/전유와 토지면적, 공용면적, 대지권 비율을 동일시하지 않는다.
    근거를 읽지 못하면 빈 값으로 유지하며 다른 매물을 추정해서 채우지 않는다.
    """
    # 의미가 비교적 명확한 속성만 우선한다. 'objctAr' 등 범용 필드는 구분 미상.
    fields = [
        ("전용면적", ("exclusiveArea", "prvuseAr", "useArea", "prvuseArDts")),
        ("건물면적", ("buildingArea", "buldAr", "buldArea")),
        ("토지면적", ("landArea", "landAr", "landAreaSqm")),
        ("면적구분 미확인", ("objctAr", "objctArDts")),
    ]
    for label, keys in fields:
        for key in keys:
            val = decimal_number(item.get(key))
            if val is not None and val > 0:
                return round(val, 2), label, key

    sources = ["areaList", "buldList", "pjbBuldList", "gdsDspslObjctNm", "objctDts"]
    # '면적 94.76㎡', '철근콘크리트조 94.76㎡' 같은 문자열을 파싱한다.
    num_unit = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:㎡|m²|m2|제곱미터)", re.I)
    candidates = []
    for order, key in enumerate(sources):
        source = clean(item.get(key))
        if not source:
            continue
        for match in num_unit.finditer(source):
            value = decimal_number(match.group(1))
            if value is None or value <= 0:
                continue
            prefix = source[max(0, match.start() - 42):match.start()]
            prefix = re.split(r"[|;\n,]", prefix)[-1]
            if re.search(r"(?:대지권\s*비율|지분\s*비율)", prefix):
                continue
            if re.search(r"(?:전용|전유)", prefix):
                label, rank = "전용/전유면적", 0
            elif re.search(r"(?:건물|집합건물|철근|콘크리트|구조|층\b)", prefix):
                label, rank = "건물 표기면적", 1
            elif re.search(r"(?:토지|대지|임야|전답|필지)", prefix):
                label, rank = "토지 표기면적", 3
            else:
                label, rank = "면적구분 미확인", 2
            candidates.append((rank, order, match.start(), round(value, 2), label, key))
    if not candidates:
        return None, "미확인", ""
    _, _, _, area, label, source_field = min(candidates)
    return area, label, source_field


def warning_terms(item: dict) -> str:
    source = " ".join(clean(item.get(k)) for k in ("mulBigo", "pjbBuldList", "buldList"))
    warning_specs = [
        ("대지권 문제", r"대지권\s*(?:미등기|없음|미확보)"),
        ("토지별도등기", r"토지\s*별도\s*등기"),
        ("법정지상권", r"법정\s*지상권"),
        ("유치권", r"유치권"),
        ("공유자우선매수", r"공유자\s*우선\s*매수"),
    ]
    return ", ".join(name for name, pattern in warning_specs if re.search(pattern, source))


def addresses(item: dict) -> tuple[str, str, str, str]:
    # 법원 관할과 실제 부동산 소재지는 다름.
    road_no = first(item, "buldNo")
    if first(item, "buldNoSub") not in ("", "0"):
        road_no += "-" + first(item, "buldNoSub")
    road = " ".join(filter(None, (first(item, k) for k in
         ("rd1Nm", "rd2Nm", "rdEubMyun", "rdNm"))))
    if road_no:
        road += " " + road_no
    road = " ".join(filter(None, (road, first(item, "buldList"), first(item, "rdAddrSub")))).strip()
    lot = " ".join(filter(None, (first(item, k) for k in
          ("hjguSido", "hjguSigu", "hjguDong", "daepyoLotno", "buldNm", "buldList"))))
    addr = first(item, "printSt", "userStPrint", "realSt") or road or lot
    sido = first(item, "hjguSido", "rd1Nm")
    if not sido:
        m = re.match(r"^(서울특별시|서울시|서울|경기도|경기|인천광역시|인천|부산광역시|대구광역시|대전광역시|광주광역시|울산광역시|세종특별자치시|제주특별자치도)", addr)
        if m:
            sido = m.group(1)
    return addr, road, lot, sido


def share_info(item):
    """Preserve sale-share evidence; absence is not proof of full ownership."""
    raw = clean(item.get("maejibun")) or clean(item.get("매각지분_원문"))
    note = first(item, "mulBigo", "dspslGdsRmk", "bigo", "물건비고")
    source = raw + " " + note
    fractions = re.findall(r"[\d,]+(?:\.\d+)?\s*분의\s*[\d,]+(?:\.\d+)?", raw)
    explicit = re.search(r"(?:공유)?지분\s*매각", source)
    negated = re.search(r"지분\s*매각\s*(?:아님|아니|이\s*아님)", source)
    status = "지분매각 표기" if fractions or (explicit and not negated) else "미확인"
    return {"지분매각": status, "매각지분": " · ".join(dict.fromkeys(fractions)) or "비율 미확인",
            "매각지분_원문": raw, "지분판단_근거": " | ".join(x for x in (raw, note if explicit else "") if x)}


def parse_record(item: dict) -> dict:
    addr, road, lot, sido = addresses(item)
    disp_case = first(item, "srnSaNo", "printCsNo")
    case_match = re.search(r"\d{4}\s*타경\s*\d+", disp_case)
    case = re.sub(r"\s+", "", case_match.group(0)) if case_match else disp_case
    appraisal = numeric(item.get("gamevalAmt"))
    min_price = numeric(item.get("minmaePrice"))
    fail = numeric(item.get("yuchalCnt"))
    sale_ratio = round(min_price / appraisal * 100, 1) if appraisal and min_price is not None else None
    discount = round(100 - min_price / appraisal * 100, 1) if appraisal and min_price is not None else None
    area, area_kind, area_field = area_info(item)
    date = first(item, "maeGiil", "dspslDxdyYmd")
    if re.fullmatch(r"\d{8}", date):
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return {
        **share_info(item),
        "소재지": addr, "주소_도로명": road, "주소_지번": lot, "시도": sido,
        "법원": first(item, "jiwonNm") or first(item, "printCsNo").split(" ")[0],
        "사건번호": case, "물건번호": first(item, "maemulSer", "mokmulSer"),
        "매각기일": date, "물건용도": first(item, "dspslUsgNm"), "상태코드": first(item, "mulStatcd"),
        "면적_㎡": area, "면적_평": round(area / 3.305785, 1) if area is not None else None,
        "면적기준": area_kind, "면적출처": area_field,
        "유찰횟수": fail, "감정가_원": appraisal, "최저매각가_원": min_price,
        "매각가율_%": sale_ratio, "할인율_%": discount,
        "물건비고": first(item, "mulBigo", "dspslGdsRmk", "bigo"),
        "물건설명": first(item, "pjbBuldList", "buldList"),
        "주의문구_자동감지": warning_terms(item),
        "전체소유권_확정여부": "미확정: 매각물건명세서·등기사항증명서 확인 필요",
        "권리인수_검토": "미검토: 선순위 권리·임차인 현황 등 별도 확인 필요",
        "saNo": first(item, "saNo"),
    }


def extract_rows(payload) -> list[dict]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    preferred = data.get("dlt_srchResult") if isinstance(data, dict) else None
    if isinstance(preferred, list):
        items = preferred
    else:
        items = []
        def visit(v):
            if isinstance(v, dict):
                if ("saNo" in v or "srnSaNo" in v or "printCsNo" in v) and (
                        "gamevalAmt" in v or "minmaePrice" in v):
                    items.append(v)
                else:
                    for child in v.values():
                        visit(child)
            elif isinstance(v, list):
                for child in v:
                    visit(child)
        visit(payload)
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        row = parse_record(item)
        key = (row["saNo"] or row["사건번호"], row["물건번호"], row["소재지"])
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def build_body(court: str, start: str, end: str, page: int, page_size: int) -> dict:
    fields = dict.fromkeys(SEARCH_FIELDS, "")
    fields.update({
        "bidDvsCd": "000331", "mvprpRletDvsCd": "00031R", "cortAuctnSrchCondCd": "0004601",
        "cortOfcCd": court.strip(), "notifyLoc": "off", "pgmId": "PGJ151F01",
        "cortStDvs": "1", "statNum": 1,
        "bidBgngYmd": datetime.strptime(start, "%Y-%m-%d").strftime("%Y%m%d"),
        "bidEndYmd": datetime.strptime(end, "%Y-%m-%d").strftime("%Y%m%d"),
    })
    return {
        "dma_pageInfo": {"pageNo": page, "pageSize": page_size, "bfPageNo": "",
                         "startRowNo": "", "totalCnt": "", "totalYn": "Y", "groupTotalCount": ""},
        "dma_srchGdsDtlSrchInfo": fields,
    }


def check_api(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict) and data.get("ipcheck") is False:
        raise RuntimeError("법원 사이트에서 접속을 제한했습니다. 재시도하지 않습니다.")
    if any(token in json.dumps(payload, ensure_ascii=False) for token in ("비정상적인 접속", "접속이 차단")):
        raise RuntimeError("사이트 차단 메시지가 감지되었습니다. 우회·자동 재시도하지 않습니다.")


def search_live(start: str, end: str, court: str, pages: int, page_size: int, progress=gr.Progress()) -> tuple[dict, str]:
    try:
        a, b = datetime.strptime(start.strip(), "%Y-%m-%d"), datetime.strptime(end.strip(), "%Y-%m-%d")
        if a > b:
            raise ValueError("시작일이 종료일보다 늦습니다.")
        pages, page_size = int(pages), int(page_size)
        if pages < 0 or page_size not in (10, 20, 50):
            raise ValueError("조회 페이지는 0(전체) 또는 양수, 페이지 크기는 10·20·50 중 선택하세요.")
        if not re.fullmatch(r"B\d{6}", court.strip()):
            raise ValueError("법원 코드는 B로 시작하는 7자리 형식이어야 합니다. 예: B000210")
    except ValueError as e:
        return {"rows": [], "payloads": []}, f"❌ 조건 오류: {e}"
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": ENTRY, "Origin": BASE, "X-Requested-With": "XMLHttpRequest",
    })
    payloads, rows, messages = [], [], []
    stage = "검색 화면 연결"
    try:
        response = session.get(ENTRY, timeout=20)
        response.raise_for_status()
        page = 1
        seen_pages = set()
        while pages == 0 or page <= pages:
            if page > 1:
                time.sleep(3)
            stage = f"{page}페이지 조회"
            progress(0, desc=f"{page}페이지 조회 중 · 현재 {len(rows)}건 수신")
            response = session.post(SEARCH, json=build_body(court, start, end, page, page_size), timeout=30)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError as e:
                raise RuntimeError(f"{page}페이지: JSON이 아닌 응답입니다. 사이트 제한 또는 변경 가능성") from e
            check_api(payload)
            extracted = extract_rows(payload)
            if extracted:
                signature = hashlib.sha256(json.dumps(extracted, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if signature in seen_pages:
                    raise RuntimeError("서버가 이전 페이지를 반복 반환해 조회를 중단했습니다.")
                seen_pages.add(signature)
            payloads.append((f"page_{page}.json", payload))
            rows.extend(extracted)
            messages.append(f"{page}페이지 {len(extracted)}건")
            if not extracted:
                break
            page_info = (payload.get("data") or {}).get("dma_pageInfo") or {}
            total = numeric(page_info.get("totalCnt"))
            actual_size = numeric(page_info.get("pageSize")) or page_size
            if total is not None and total > 0 and page * actual_size >= total:
                break
            page += 1
        rows = deduplicate(rows)
        status = f"✅ 응답 {len(payloads)}페이지 / {len(rows)}건 (" + ", ".join(messages) + ")"
        if not rows:
            status += " · 조회 결과 없음 또는 API 응답 형식 변경 가능성. JSON 원본도 확인해 주세요."
        return {"rows": rows, "payloads": payloads, "partial": False}, status
    except (requests.RequestException, RuntimeError) as e:
        saved = deduplicate(rows)
        logging.getLogger(__name__).warning("Court search stopped at %s: %s", stage, e)
        if isinstance(e, requests.Timeout):
            reason = "법원 서버의 응답 대기시간을 초과했습니다."
        elif isinstance(e, requests.ConnectionError):
            reason = "법원 서버와의 연결이 응답 전에 끊겼습니다. 일시 장애인지 접속 제한인지는 확인되지 않았습니다."
        else:
            reason = "법원 사이트의 응답을 처리하지 못했습니다. 원본 사이트의 상태를 확인해 주세요."
        heading = f"일부 조회 · {len(saved)}건 보존" if saved else "조회하지 못했습니다"
        status = (f"**{heading}** — {stage}에서 중단되었습니다.\n\n{reason} " +
                  (f"전체 페이지 조회 중 {len(payloads)}페이지의 응답을 받았습니다. " if pages == 0 else f"요청한 {pages}페이지 중 {len(payloads)}페이지의 응답을 받았습니다. ") +
                  "표시된 결과는 전체 조회 결과가 아닙니다. 받은 물건은 필터·내보내기를 계속 사용할 수 있습니다. "
                  f"[법원 사이트에서 확인]({SEARCH_PAGE}) · 자동 재시도하지 않습니다.")
        return {"rows": saved, "payloads": payloads, "partial": True,
                "failed_stage": stage}, status
    finally:
        session.close()


def deduplicate(rows: list[dict]) -> list[dict]:
    result, seen = [], set()
    for row in rows:
        key = (row.get("saNo") or row.get("사건번호"), row.get("물건번호"), row.get("소재지"))
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def upload_files(paths) -> tuple[dict, str]:
    if not paths:
        return {"rows": [], "payloads": []}, "❌ JSON/ZIP/CSV 파일을 선택해 주세요."
    if isinstance(paths, str):
        paths = [paths]
    rows, payloads, errors = [], [], []
    for path in paths[:10]:
        p = Path(path)
        try:
            if p.stat().st_size > 25 * 1024 * 1024:
                raise ValueError("파일이 25MB보다 큽니다.")
            if p.suffix.lower() == ".json":
                obj = json.loads(p.read_text(encoding="utf-8-sig"))
                rows.extend(extract_rows(obj))
                payloads.append((p.name, obj))
            elif p.suffix.lower() == ".zip":
                with zipfile.ZipFile(p) as z:
                    entries = [x for x in z.infolist() if re.search(r"page_\d+\.json$", x.filename)]
                    for member in entries[:5]:
                        if member.file_size > 15 * 1024 * 1024:
                            continue
                        obj = json.loads(z.read(member).decode("utf-8-sig"))
                        rows.extend(extract_rows(obj))
                        payloads.append((Path(member.filename).name, obj))
                    if not entries:
                        errors.append(f"{p.name}: page_N.json 파일이 없습니다.")
            elif p.suffix.lower() == ".csv":
                with p.open(encoding="utf-8-sig", newline="") as f:
                    for item in csv.DictReader(f):
                        if "소재지" in item:
                            normalized = {k: item.get(k, "") for k in EXPORT_COLUMNS}
                            for field in ("주의문구_자동감지",):
                                normalized[field] = item.get(field, "")
                            # 이전 CSV에 면적이 없는 경우 추정하지 않는다.
                            area = decimal_number(item.get("면적_㎡"))
                            if area is None:
                                area = decimal_number(item.get("areaList"))
                            if area is not None:
                                normalized["면적_㎡"] = area
                                normalized["면적_평"] = round(area / 3.305785, 1)
                                normalized["면적기준"] = item.get("면적기준") or "면적구분 미확인"
                            a = numeric(item.get("감정가_원"))
                            m = numeric(item.get("최저매각가_원"))
                            if a and m is not None:
                                normalized["매각가율_%"] = round(m / a * 100, 1)
                                normalized["할인율_%"] = round(100 - m / a * 100, 1)
                            normalized.update(share_info(item))
                            rows.append(normalized)
                        else:
                            rows.append(parse_record(item))
                    errors.append(f"{p.name}: CSV에 없는 원본 주소·면적은 복원할 수 없습니다.")
            else:
                errors.append(f"{p.name}: 지원하지 않는 파일 유형")
        except (OSError, UnicodeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as e:
            errors.append(f"{p.name}: {e}")
    rows = deduplicate(rows)
    status = f"✅ 불러오기 완료: {len(rows)}건" if rows else "⚠️ 불러온 결과가 없습니다."
    if errors:
        status += "\n\n" + "\n\n".join(f"- {x}" for x in errors)
    return {"rows": rows, "payloads": payloads}, status


def in_range(value: float | None, lower: float, upper: float, upper_unbounded: bool = False) -> bool:
    if lower <= 0 and (upper_unbounded or upper <= 0):
        return True
    if value is None:
        return False
    return value >= lower and (upper_unbounded or upper <= 0 or value <= upper)


def filter_rows(data: dict, region: str, keyword: str,
                min_fails: int, max_fails: int,
                min_price_eok: float, max_price_eok: float,
                min_discount: float, max_discount: float,
                min_area: float, max_area: float,
                area_unit: str, order_by: str, building_types=None) -> list[dict]:
    rows = (data or {}).get("rows", [])
    filtered = []
    factor = 3.305785 if area_unit == "평" else 1.0
    area_low = float(min_area or 0) * factor
    area_high = float(max_area or 0) * factor
    price_low = float(min_price_eok or 0) * 100_000_000
    price_high = float(max_price_eok or 0) * 100_000_000
    pct_low = float(min_discount or 0)
    pct_high = float(max_discount if max_discount is not None else 100)
    for r in rows:
        if building_types and (clean(r.get("물건용도")) or "미확인") not in building_types:
            continue
        addr, sido = clean(r.get("소재지")), clean(r.get("시도"))
        if region == "서울특별시" and not (sido.startswith("서울") or addr.startswith("서울")):
            continue
        if region == "경기도" and not (sido.startswith("경기") or addr.startswith("경기")):
            continue
        if keyword and keyword.strip() not in addr:
            continue
        fail = numeric(r.get("유찰횟수"))
        if min_fails > 0 and (fail is None or fail < min_fails):
            continue
        if max_fails > 0 and (fail is None or fail > max_fails):
            continue
        price = numeric(r.get("최저매각가_원"))
        if not in_range(price, price_low, price_high):
            continue
        discount = decimal_number(r.get("할인율_%"))
        if discount is None:
            a, m = numeric(r.get("감정가_원")), numeric(r.get("최저매각가_원"))
            discount = 100 - m / a * 100 if a and m is not None else None
        # 할인율 0%~0%도 유효한 필터다. 최대 100%일 때만 상한 제한 없음.
        if pct_low > 0 or pct_high < 100:
            if discount is None or not (pct_low <= discount <= pct_high):
                continue
        area = decimal_number(r.get("면적_㎡"))
        if not in_range(area, area_low, area_high):
            continue
        filtered.append(r)
    sort_specs = {
        "최저매각가 낮은 순": ("최저매각가_원", False),
        "최저매각가 높은 순": ("최저매각가_원", True),
        "할인율 높은 순": ("할인율_%", True),
        "면적 넓은 순": ("면적_㎡", True),
        "유찰 많은 순": ("유찰횟수", True),
        "매각기일 빠른 순": ("매각기일", False),
    }
    if order_by in sort_specs:
        key, reverse = sort_specs[order_by]
        # 확인 불가능한 값은 정렬 순서와 관계 없이 항상 끝으로 이동한다.
        def sort_value(r):
            value = clean(r.get(key)) if key == "매각기일" else decimal_number(r.get(key))
            if value is None or value == "":
                return (1, 0 if key != "매각기일" else "")
            return (0, -value if reverse and key != "매각기일" else value)
        if key == "매각기일":
            filtered.sort(key=sort_value)
        else:
            filtered.sort(key=sort_value)
    return filtered


def fmt_money(value) -> str:
    n = numeric(value)
    if n is None:
        return "미확인"
    if n == 0:
        return "0원"
    parts = []
    for unit, label in ((10**12, "조"), (10**8, "억"), (10**4, "만")):
        amount, n = divmod(n, unit)
        if amount:
            parts.append(f"{amount:,}{label}")
    if n:
        parts.append(f"{n:,}")
    return " ".join(parts) + " 원"


def make_csv(rows: list[dict]) -> str | None:
    if not rows:
        return None
    out = OUTPUT_DIR / f"auction_filtered_{int(time.time() * 1000)}.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return str(out)


def make_json_zip(data: dict) -> str | None:
    payloads = (data or {}).get("payloads") or []
    if not payloads:
        return None
    out = OUTPUT_DIR / f"auction_raw_{int(time.time() * 1000)}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for index, (name, content) in enumerate(payloads[:5], 1):
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", Path(name).name)
            z.writestr(safe_name or f"page_{index}.json", json.dumps(content, ensure_ascii=False, indent=2))
    return str(out)


def select_detail(rows: list[dict], key: str) -> str:
    if not rows:
        return "검색 결과가 없습니다."
    try:
        idx = int(str(key).split(" · ", 1)[0].replace("#", "")) - 1
        row = rows[idx]
    except (ValueError, IndexError, TypeError):
        return "물건을 선택해 주세요."

    def v(name):
        return clean(row.get(name)) or "미확인"

    area = decimal_number(row.get("면적_㎡"))
    area_label = f"{area:g}㎡ ({area / 3.305785:.1f}평)" if area is not None else "미확인"
    return (f"### {v('사건번호')} · 물건 {v('물건번호')}\n\n"
            f"**소재지:** {v('소재지')}\n\n"
            f"**지분매각:** {v('지분매각')} / **매각지분:** {v('매각지분')}\n\n"
            f"**지분 원문:** {v('매각지분_원문')}\n\n"
            f"**지분 판단 근거:** {v('지분판단_근거')}\n\n"
            f"**면적:** {area_label} / **기준:** {v('면적기준')}\n\n"
            f"**최저매각가:** {fmt_money(row.get('최저매각가_원'))} / "
            f"**감정가:** {fmt_money(row.get('감정가_원'))}\n\n"
            f"**유찰:** {v('유찰횟수')}회 / **매각가율:** {v('매각가율_%')}% / "
            f"**할인율:** {v('할인율_%')}%\n\n"
            f"**매각기일:** {v('매각기일')}\n\n"
            f"**중요:** 면적의 기준은 원본 자료 확인이 필요할 수 있으며, "
            f"매각범위·인수권리·공유지분 등은 이 화면에서 판단하지 않습니다. "
            f"[법원경매정보 검색 화면]({SEARCH_PAGE})에서 "
            f"매각물건명세서·등기사항증명서를 확인하세요.")


def render(data, region, keyword, min_fails, max_fails,
           min_price, max_price, min_discount, max_discount,
           min_area, max_area, area_unit, order_by, building_types=None):
    errors = []
    for lo, hi, unit in [
        (min_price, max_price, "가격(억 원)"),
        (min_discount, max_discount, "할인율(%)"),
        (min_area, max_area, f"면적({area_unit})"),
        (min_fails, max_fails, "유찰 횟수"),
    ]:
        if float(lo or 0) > 0 and float(hi or 0) > 0 and float(lo) > float(hi):
            errors.append(f"{unit}의 최소값이 최대값보다 큽니다.")
    if errors:
        return ([], "### 조건 오류\n" + "\n".join(f"- {msg}" for msg in errors),
                pd.DataFrame(columns=RESULT_COLUMNS), gr.update(choices=[], value=None),
                "조건을 수정해 주세요.", None, make_json_zip(data))

    rows = filter_rows(data, region, keyword, int(min_fails), int(max_fails),
                       min_price, max_price, min_discount, max_discount,
                       min_area, max_area, area_unit, order_by, building_types)
    original_count = len((data or {}).get("rows", []))
    fails = sum((numeric(r.get("유찰횟수")) or 0) >= 1 for r in rows)
    missing_addr = sum(not clean(r.get("소재지")) for r in rows)
    missing_area = sum(decimal_number(r.get("면적_㎡")) is None for r in rows)
    missing_discount = sum(decimal_number(r.get("할인율_%")) is None for r in rows)
    summary = (f"### 조회 결과 **{len(rows)}건** / 수집 {original_count}건\n\n"
               f"유찰 1회 이상 **{fails}건** · 면적 미확인 **{missing_area}건** · "
               f"할인율 미확인 **{missing_discount}건** · 주소 누락 **{missing_addr}건**\n\n"
               "※ 할인율 = `(감정가 − 최저매각가) ÷ 감정가 × 100`. "
               "매각가율 = `최저매각가 ÷ 감정가 × 100`. "
               "면적·할인율이 없으면 해당 범위 필터 적용 시 제외됩니다.")
    display_rows = []
    for r in rows:
        display_rows.append({
            **{col: (r.get(col) if r.get(col) is not None else "") for col in RESULT_COLUMNS},
            "감정가_원": fmt_money(r.get("감정가_원")),
            "최저매각가_원": fmt_money(r.get("최저매각가_원")),
        })
    options = [f"#{i} · {clean(r.get('사건번호'))} · {clean(r.get('소재지'))[:55]}" for i, r in enumerate(rows, 1)]
    first_option = options[0] if options else None
    return (rows, summary, pd.DataFrame(display_rows, columns=RESULT_COLUMNS),
            gr.update(choices=options, value=first_option),
            select_detail(rows, first_option) if options else "선택할 물건이 없습니다.",
            make_csv(rows), make_json_zip(data))



# ──────────────────────────────────────────────────────────────────────────
# 선택한 경매 물건의 카카오 위치 확인 + 국토부 아파트 매매 실거래가 비교
# • 공식 API 활용: Kakao Local REST / 국토교통부 아파트 매매 실거래가
# • 비공식: 위쪽 법원경매 검색 API (실서비스 호환 미검증)
# ──────────────────────────────────────────────────────────────────────────
import os
import statistics
from urllib.parse import quote
from xml.etree import ElementTree as ET

KAKAO_GEO = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_STATIC = "https://dapi.kakao.com/v2/maps/staticmap"
MOLIT_TRADES = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
TRADE_COLUMNS = ["계약일", "아파트", "법정동", "지번", "층", "전용면적_㎡", "거래금액_원", "취소여부", "일치판단"]


def configured_api_keys():
    """Read only supported keys; environment values take precedence over local .env."""
    names = ("KAKAO_REST_API_KEY", "DATA_GO_KR_SERVICE_KEY")
    values = {}
    path = Path(__file__).resolve().parent / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            name = name.strip()
            if sep and name in names:
                values[name] = value.strip().strip("\"'")
    return {name: os.environ.get(name, values.get(name, "")) for name in names}


def normalize_name(text: str) -> str:
    return re.sub(r"[^가-힣a-z0-9]", "", clean(text).lower())


def selected_row(rows: list[dict], key: str) -> dict | None:
    if not rows or not key:
        return None
    try:
        i = int(str(key).split(" · ", 1)[0].replace("#", "")) - 1
        return rows[i] if 0 <= i < len(rows) else None
    except (ValueError, TypeError, IndexError):
        return None


def strip_detail_address(address: str) -> str:
    """지오코딩 질의용; 법원에 표시된 정확한 주소를 임의로 대체하지 않는다."""
    address = clean(address)
    # 면적·건물 구조·등기 내용으로 시작되는 뒷부분 제거
    address = re.split(r"\s+(?:집합건물|철근|콘크리트|매각지분|갑구|대지권|전유부분)\b", address)[0]
    address = re.sub(r"\[[^]]*\]", " ", address)
    return re.sub(r"\s+", " ", address).strip()[:180]


def geocode_kakao(address: str, api_key: str) -> dict:
    if not api_key:
        raise ValueError("카카오 REST API 키가 없습니다. 주소 지도 검색 링크만 이용할 수 있습니다.")
    query = strip_detail_address(address)
    if not query:
        raise ValueError("소재지 주소가 없어서 지도 좌표를 확인할 수 없습니다.")
    response = requests.get(
        KAKAO_GEO,
        params={"query": query, "size": 5},
        headers={"Authorization": f"KakaoAK {api_key.strip()}"},
        timeout=18,
    )
    response.raise_for_status()
    items = response.json().get("documents", [])
    if not items:
        raise ValueError("카카오에서 주소 좌표를 찾지 못했습니다. 주소를 직접 확인해 주세요.")
    # API 첫 결과가 동일 건물인지 보장하지 않는다.
    d = items[0]
    a = d.get("address") or {}
    road = d.get("road_address") or {}
    return {
        "lat": float(d["y"]), "lng": float(d["x"]),
        "lawd_code": str(a.get("b_code") or "")[:5],
        "dong": a.get("region_3depth_name", ""),
        "district": a.get("region_2depth_name", ""),
        "found_address": a.get("address_name") or road.get("address_name") or "",
        "requested": query,
        "count": len(items),
    }


def static_map_kakao(lat: float, lng: float, api_key: str) -> str:
    res = requests.get(
        KAKAO_STATIC,
        headers={"Authorization": f"KakaoAK {api_key.strip()}"},
        params={"center": f"{lng:.7f},{lat:.7f}", "markers": f"location:{lng:.7f},{lat:.7f}|option:false",
                "size": "600x360", "scale": 1, "lv": 3, "format": "png"},
        timeout=20,
    )
    res.raise_for_status()
    if not res.headers.get("Content-Type", "").lower().startswith("image/"):
        raise RuntimeError("카카오 정적 지도 이미지 대신 다른 응답이 도착했습니다.")
    if len(res.content) > 4 * 1024 * 1024:
        raise RuntimeError("지도 이미지가 예상보다 큽니다.")
    out = OUTPUT_DIR / f"kakao_map_{int(time.time()*1000)}.png"
    out.write_bytes(res.content)
    return str(out)


def month_keys(count: int, today=None) -> list[str]:
    """오늘 포함 지난 N개 달. 호출 제한 최대 12개월."""
    if today is None:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    n = max(1, min(int(count), 12))
    m = today.year * 12 + (today.month - 1)
    return [f"{(m-i)//12:04d}{(m-i)%12 + 1:02d}" for i in range(n)]


def parse_trade_xml(xml_bytes: bytes) -> tuple[list[dict], int]:
    root = ET.fromstring(xml_bytes)
    header = root.find(".//header")
    if header is None:
        # 공공데이터포털의 인증/쿼터 오류는 표준 응답 외 형태일 수도 있다.
        reason = root.findtext(".//returnAuthMsg") or root.findtext(".//returnReasonCode") or "알 수 없는 API 응답"
        raise RuntimeError(f"국토부 API의 정상 응답 헤더가 없습니다: {reason[:100]}")
    code = (header.findtext("resultCode") if header is not None else "") or ""
    msg = (header.findtext("resultMsg") if header is not None else "") or ""
    if code and code.strip() not in {"00", "000"}:
        raise RuntimeError(f"국토부 API 오류 코드 {code}: {msg}")
    total_text = root.findtext(".//body/totalCount") or "0"
    total = int(total_text.strip()) if total_text.strip().isdigit() else 0
    entries = []
    for item in root.findall(".//items/item"):
        f = lambda name: (item.findtext(name) or "").strip()
        price_s = f("dealAmount").replace(",", "").replace(" ", "")
        try:
            price = int(price_s) * 10000  # 원본은 만원
            area = float(f("excluUseAr"))
        except (TypeError, ValueError):
            continue
        month, day = f("dealMonth"), f("dealDay")
        year = f("dealYear")
        date = f"{year}-{month.zfill(2)}-{day.zfill(2)}" if year and month and day else ""
        cancel = f("cdealType")
        entries.append({
            "계약일": date, "아파트": f("aptNm"), "법정동": f("umdNm"), "지번": f("jibun"),
            "층": f("floor"), "전용면적_㎡": area, "거래금액_원": price,
            "취소여부": "취소" if cancel.upper() in ("O", "Y", "1") else ("미확인" if cancel and cancel.upper() != "N" else "정상"),
            "일치판단": "미평가",
        })
    return entries, total


def fetch_trades(lawd_code: str, months: int, service_key: str) -> tuple[list[dict], str]:
    if not re.fullmatch(r"\d{5}", lawd_code):
        raise ValueError("법정동 지역코드는 5자리 숫자여야 합니다. 예: 강남구 11680")
    if not service_key.strip():
        raise ValueError("국토부 API 인증키가 없습니다.")
    # 공공데이터포털에서는 디코딩 키를 입력하는 것을 권장. URL 인코딩 키도 받아서 한 번 디코딩.
    from urllib.parse import unquote
    key = unquote(service_key.strip())
    all_trades = []
    notes = []
    for ym in month_keys(months):
        # numOfRows 제한: 월별 1000건까지. 초과 시 페이지 2~3까지만 추가 (상한 3000건/월).
        page = 1
        total_count = None
        while page <= 3:
            resp = requests.get(
                MOLIT_TRADES,
                params={"serviceKey": key, "LAWD_CD": lawd_code, "DEAL_YMD": ym,
                        "pageNo": page, "numOfRows": 1000},
                timeout=25,
            )
            resp.raise_for_status()
            parsed, total = parse_trade_xml(resp.content)
            all_trades.extend(parsed)
            total_count = total
            if page * 1000 >= total or not parsed:
                break
            page += 1
            time.sleep(0.25)
        if total_count is not None and total_count > page * 1000:
            notes.append(f"{ym}: 조회 상한 3,000건을 초과해 일부 거래만 표시")
        if len(month_keys(months)) > 1:
            time.sleep(0.2)
    seen = set()
    unique = []
    for t in all_trades:
        k = tuple(t.get(c) for c in ("계약일", "아파트", "법정동", "지번", "층", "전용면적_㎡", "거래금액_원"))
        if k not in seen:
            seen.add(k)
            unique.append(t)
    return unique, "; ".join(notes)


def comparable_rows(trades: list[dict], dong: str, apt_name: str,
                    auction_area: float | None, verified_area: bool,
                    tolerance: float = 5.0) -> tuple[list[dict], list[dict], str]:
    """통계는 동일 단지명 + 전용면적 확인 + 유사면적일 때만 허용."""
    wanted = normalize_name(apt_name)
    candidates = []
    strict = []
    for t in trades:
        if t.get("취소여부") != "정상":
            continue
        if dong and normalize_name(t.get("법정동", "")) != normalize_name(dong):
            continue
        r = t.copy()
        if wanted and normalize_name(t.get("아파트", "")) == wanted:
            if verified_area and auction_area is not None and abs(float(t["전용면적_㎡"]) - auction_area) <= tolerance:
                r["일치판단"] = "동일 단지명·유사 전용면적"
                strict.append(r)
            else:
                r["일치판단"] = "동일 단지명(면적 미확인/차이)"
        else:
            r["일치판단"] = "동일 법정동의 다른 단지 또는 미검증"
        candidates.append(r)
    candidates.sort(key=lambda x: x["계약일"], reverse=True)
    strict.sort(key=lambda x: x["계약일"], reverse=True)
    if not apt_name.strip():
        reason = "단지명을 입력하지 않아 동일 단지 비교 수치를 계산하지 않았습니다."
    elif not verified_area or auction_area is None:
        reason = "경매 물건의 전용면적이 확정되지 않아 동일 면적 비교 수치를 계산하지 않았습니다."
    elif not strict:
        reason = "동일 단지명·전용면적 ±5㎡ 이내의 거래를 찾지 못했습니다."
    else:
        reason = f"동일 단지명·유사 전용면적 거래 {len(strict)}건 (층·동·권리 범위는 미확인)"
    return candidates, strict, reason


def insights(rows, key, kakao_key, molit_key, manual_code, apt_name, months,
             manual_area_ok):
    row = selected_row(rows, key)
    empty = pd.DataFrame(columns=TRADE_COLUMNS)
    if row is None:
        return "⚠️ 경매 물건을 먼저 선택해 주세요.", None, empty, None
    addr = clean(row.get("소재지")) or clean(row.get("주소_지번"))
    if not addr:
        return "⚠️ 주소가 없는 경매 데이터입니다. 원본 JSON이 필요합니다.", None, empty, None
    link = "https://map.kakao.com/?q=" + quote(strip_detail_address(addr), safe="")
    parts = [f"### {clean(row.get('사건번호'))} · 위치/실거래가", f"**법원 원본 소재지:** {addr}",
             f"[카카오맵에서 주소 검색]({link})"]
    configured = configured_api_keys()
    kakao_key = (kakao_key or configured["KAKAO_REST_API_KEY"]).strip()
    molit_key = (molit_key or configured["DATA_GO_KR_SERVICE_KEY"]).strip()
    point, map_image = None, None
    if kakao_key:
        try:
            point = geocode_kakao(addr, kakao_key)
            parts.append(f"**지도 검색 위치:** {point['found_address']} (자동 매칭, 법원 원주소와 동일 물건인지 확인 필요)")
            try:
                map_image = static_map_kakao(point['lat'], point['lng'], kakao_key)
            except (requests.RequestException, RuntimeError, OSError) as err:
                parts.append(f"⚠️ 정적 지도 이미지 미표시: {type(err).__name__}")
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as err:
            parts.append(f"⚠️ 카카오 좌표 조회 실패: {type(err).__name__}. 인증키 권한과 주소를 확인하세요.")
    else:
        parts.append("카카오 키를 입력하면 지도 미리보기와 지역코드 자동 탐색이 활성화됩니다.")

    lawd = str(manual_code or "").strip() or (point or {}).get("lawd_code", "")
    dong = (point or {}).get("dong", "")
    if not molit_key:
        parts.append("국토부 API 키가 없어 실거래가 조회를 건너뜁니다.")
        return "\n\n".join(parts), map_image, empty, None
    if not re.fullmatch(r"\d{5}", lawd):
        parts.append("⚠️ 유효한 5자리 지역코드를 찾지 못했습니다. '법정동 지역코드 직접 입력'을 사용하세요.")
        return "\n\n".join(parts), map_image, empty, None
    try:
        trades, partial_note = fetch_trades(lawd, int(months), molit_key)
    except (requests.RequestException, ValueError, RuntimeError, ET.ParseError) as err:
        parts.append(f"⚠️ 국토부 조회 실패: {type(err).__name__}. 인증키 권한과 서비스 상태를 확인하세요.")
        return "\n\n".join(parts), map_image, empty, None
    parts.append(f"**국토부 조회 범위:** 지역코드 `{lawd}`, 최근 {int(months)}개월 · 지역 전체 {len(trades)}건")
    if partial_note:
        parts.append("⚠️ " + partial_note)
    area = decimal_number(row.get("면적_㎡"))
    area_source = clean(row.get("면적기준"))
    verified_area = area_source in ("전용면적", "전용/전유면적") or bool(manual_area_ok)
    candidates, strict, msg = comparable_rows(trades, dong, apt_name or "", area, verified_area)
    parts.append(f"**비교 판정:** {msg}")
    min_price = numeric(row.get("최저매각가_원"))
    if row.get("지분매각") == "지분매각 표기":
        strict = []
        parts.append("**지분매각 표기 물건**입니다. 매각지분: " + clean(row.get("매각지분")) + ". 전체 아파트 실거래가와의 가격차 계산은 생략합니다.")
    if strict:
        prices = [r["거래금액_원"] for r in strict]
        median_price = int(statistics.median(prices))
        parts.append(f"**유사 거래 중위가격:** {fmt_money(median_price)} (표본 {len(strict)}건)")
        if min_price is not None:
            gap = (median_price - min_price) / median_price * 100 if median_price else 0
            parts.append(f"**참고 가격차:** {gap:+.1f}% (중위 실거래가와 최저매각가의 단순 비교, 낙찰 수익률 아님)")
    if candidates:
        parts.append(f"동일 법정동 거래 후보 {len(candidates)}건 중 최근 최대 100건을 표시합니다. ")
    else:
        parts.append("선택한 기간·법정동에서 유효한 아파트 거래 후보를 찾지 못했습니다.")
    parts.append("⚠️ **주의:** 일부 지분만 매각되는 물건, 대지권 누락, 인수권리, 층·동 차이, 취소된 계약 등은 별도 확인이 필요합니다. 자동 비교 수치는 투자 판단이나 시세 감정이 아닙니다.")
    df = pd.DataFrame(candidates[:100], columns=TRADE_COLUMNS)
    df["거래금액_원"] = df["거래금액_원"].map(fmt_money)
    download_path = None
    if candidates:
        download_path = OUTPUT_DIR / f"apt_trades_{int(time.time()*1000)}.csv"
        pd.DataFrame(candidates, columns=TRADE_COLUMNS).to_csv(download_path, index=False, encoding="utf-8-sig")
    return "\n\n".join(parts), map_image, df, str(download_path) if download_path else None

def result_dashboard(rows, data):
    def esc(value):
        return html.escape(str(value if value not in (None, "") else "미확인"))
    if not rows:
        return '<div class="empty-state"><span>⌕</span><h2>조건에 맞는 물건을 찾아보세요</h2><p>검색 전이라면 위에서 데이터를 가져오세요.<br>이미 조회했다면 지역·가격 조건을 넓혀보세요.</p></div>'
    partial = bool((data or {}).get("partial"))
    metrics = [("필터 결과", f"{len(rows)}건"), ("가져온 물건", f"{len((data or {}).get('rows', []))}건"),
               ("유찰 1회 이상", f"{sum((numeric(r.get('유찰횟수')) or 0) >= 1 for r in rows)}건")]
    stats = '<div class="metrics">' + ''.join(f'<div><span>{label}</span><strong>{value}</strong></div>' for label, value in metrics) + '</div>'
    notice = '<p class="partial-note">일부 데이터만 조회되었습니다. 아래 목록은 전체 검색 결과가 아닙니다.</p>' if partial else ''
    cards = []
    for i, r in enumerate(rows, 1):
        ownership = f"{esc(r.get('지분매각') or '미확인')} · {esc(r.get('매각지분') or '비율 미확인')}"
        price = numeric(r.get("최저매각가_원"))
        money = fmt_money(price)
        discount = decimal_number(r.get("할인율_%"))
        badge = f"감정가 대비 {discount:g}% 할인" if discount is not None else "할인율 미확인"
        area = decimal_number(r.get("면적_㎡"))
        area_text = f"{area:g}㎡" if area is not None else "면적 미확인"
        cards.append(f'<article class="property-card"><div class="card-top"><span>#{i:02d} · {esc(r.get("물건용도"))}</span><span class="discount">{badge}</span></div>'
                     f'<h3>{esc(r.get("소재지"))}</h3><p class="case">{esc(r.get("사건번호"))} · 물건 {esc(r.get("물건번호"))}</p>'
                     f'<p class="partial-note">지분: {ownership}</p><div class="price-label">최저매각가</div><div class="price">{money}</div>'
                     f'<div class="card-facts"><span>{area_text} · {esc(r.get("면적기준"))}</span><span>유찰 {esc(r.get("유찰횟수"))}회</span></div>'
                     f'<div class="card-bottom"><span>매각 {esc(r.get("매각기일"))}</span><button type="button" data-property-index="{i - 1}">상세조회 →</button></div></article>')
    return stats + notice + '<div class="result-heading"><h2>경매 물건</h2><span>상세조회로 지도와 실거래가를 바로 확인하세요</span></div><div class="property-grid">' + ''.join(cards) + '</div>'


BUILDING_TYPES = ["단독주택", "다가구주택", "단독주택다가구", "아파트", "다세대", "연립주택", "오피스텔", "근린시설", "상가", "사무실", "공장", "창고", "대지", "전", "답", "임야", "미확인"]


def render_dashboard(*args):
    result = list(render(*args))
    if "조건 오류" in result[1]:
        result[1] = '<div class="partial-note">' + html.escape(result[1]).replace("\n", "<br>") + '</div>'
    else:
        result[1] = result_dashboard(result[0], args[0])
    choices = sorted(set(BUILDING_TYPES) | {(clean(r.get("물건용도")) or "미확인") for r in (args[0] or {}).get("rows", [])} | set(args[-1] or []))
    return (*result, gr.update(choices=choices))


CARD_EVENTS = """
element.addEventListener('click', (event) => {
    const button = event.target.closest('[data-property-index]');
    if (button) {
        event.preventDefault();
        trigger('select', {index: Number(button.dataset.propertyIndex), value: button.dataset.propertyIndex});
    }
});
"""


def open_property(rows, key):
    row = selected_row(rows, key)
    if row is None:
        raise gr.Error("물건을 다시 선택해 주세요.")
    return [row], gr.update(selected="compare"), select_detail([row], "#1")


def open_card(rows, evt: gr.SelectData):
    index = int(evt.index)
    if not 0 <= index < len(rows):
        raise gr.Error("검색 결과가 변경되었습니다. 물건을 다시 선택해 주세요.")
    return open_property(rows, f"#{index + 1}")


def cached_insights(cache, rows, kakao_key, molit_key, manual_code, apt_name, months, verified, refresh=False):
    if not rows:
        return cache or {}, "목록에서 상세조회를 눌러 물건을 선택하세요.", None, pd.DataFrame(columns=TRADE_COLUMNS), None
    configured = configured_api_keys()
    credentials = [kakao_key or configured["KAKAO_REST_API_KEY"], molit_key or configured["DATA_GO_KR_SERVICE_KEY"]]
    fingerprint = hashlib.sha256(json.dumps([rows[0], manual_code, apt_name, months, verified, credentials],
                                           sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cache = dict(cache or {})
    if refresh or fingerprint not in cache:
        cache[fingerprint] = insights(rows, "#1", kakao_key, molit_key, manual_code, apt_name, months, verified)
    return (cache, *cache[fingerprint])


def refresh_insights(*args):
    return cached_insights(*args, refresh=True)


APP_CSS = """
.gradio-container {max-width: 1480px !important; margin:auto; background:var(--body-background-fill);}
#hero {padding:22px 26px; border-radius:16px; background:#112f35; color:white; margin-bottom:6px;}
#hero h1 {color:white; font-size:28px; letter-spacing:-1px; margin:6px 0;}
#hero p {color:#c7dcdf; margin:0; font-size:14px;}
#hero .eyebrow {color:#80d9bd; font-size:11px; letter-spacing:2px; font-weight:700;}
#status {padding:14px 18px; border-left:4px solid #147d68; background:var(--block-background-fill); border-radius:8px; color:var(--body-text-color);}
#filter-panel {padding:18px; border:1px solid var(--border-color-primary); border-radius:16px; background:var(--block-background-fill);}
#summary {padding:0; background:transparent; border:0;}
.metrics {display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:24px;}
.metrics>div {background:var(--block-background-fill);border:1px solid var(--border-color-primary);border-radius:14px;padding:18px 22px;}
.metrics span {display:block;color:var(--body-text-color-subdued);font-size:13px;}
.metrics strong {display:block;color:var(--body-text-color);font-size:30px;margin-top:8px;}
.result-heading {display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 14px;}
.result-heading h2 {font-size:20px;margin:0;color:var(--body-text-color);}
.result-heading>span {font-size:12px;color:var(--body-text-color-subdued);}
.property-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;max-height:790px;overflow:auto;padding:2px 4px 8px 0;}
.property-card {background:var(--block-background-fill);border:1px solid var(--border-color-primary);border-radius:15px;padding:22px;transition:box-shadow .15s;}
.property-card:hover {box-shadow:0 6px 22px #163b3912;border-color:#80b7a9;}
.card-top {display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;color:var(--body-text-color-subdued);}
.discount {background:var(--background-fill-secondary);color:var(--body-text-color);border-radius:5px;padding:5px 7px;font-weight:700;}
.property-card h3 {font-size:17px;line-height:1.6;min-height:54px;margin:16px 0 4px;color:var(--body-text-color);word-break:keep-all;overflow-wrap:anywhere;}
.case {color:var(--body-text-color-subdued);font-size:12px;margin:0 0 18px;}
.price-label {color:var(--body-text-color-subdued);font-size:12px;}
.price {font-size:clamp(20px, 2.3vw, 29px);overflow-wrap:anywhere;font-weight:750;color:var(--body-text-color);margin:4px 0 14px;}
.price small {font-size:15px;font-weight:500;margin-left:5px;}
.card-facts {display:flex;justify-content:space-between;color:var(--body-text-color-subdued);font-size:12px;gap:8px;}
.card-bottom {display:flex;justify-content:space-between;border-top:1px solid var(--border-color-primary);margin-top:18px;padding-top:14px;font-size:12px;color:var(--body-text-color-subdued);}
.card-bottom button {cursor:pointer;background:transparent;border:0;color:var(--link-text-color);text-decoration:none;font-weight:700;}
.empty-state {text-align:center;background:var(--block-background-fill);border:1px dashed var(--border-color-primary);border-radius:16px;padding:60px 20px;color:var(--body-text-color-subdued);}
.empty-state>span {font-size:48px;color:var(--link-text-color);}.empty-state h2{font-size:22px;color:var(--body-text-color);}.empty-state p{line-height:1.8;}
.partial-note {background:var(--background-fill-secondary);color:var(--body-text-color);border:1px solid var(--border-color-primary);border-radius:10px;padding:14px;margin-bottom:16px;}
#detail {padding:20px;background:var(--block-background-fill);border:1px solid var(--border-color-primary);border-radius:14px;scroll-margin-top:20px;}
#results-table table {min-width:1200px;}
@media(max-width:900px){.property-grid{grid-template-columns:1fr;}.metrics>div{padding:14px;}.metrics strong{font-size:24px;}}
@media(max-width:600px){.gradio-container{padding:12px!important;}#hero{padding:20px;}#hero h1{font-size:23px;}.metrics{gap:6px;}.metrics>div{padding:12px 8px;}.result-heading{display:block;}}
"""



def ui_options():
    return dict(css=APP_CSS, theme=gr.themes.Base(primary_hue="teal", secondary_hue="slate", neutral_hue="slate", font=["system-ui", "sans-serif"]))


def build_app() -> gr.Blocks:
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    with gr.Blocks(title="Auction Reader · 경매 탐색", **(ui_options() if int(gr.__version__.split(".")[0]) < 6 else {})) as app:
        gr.HTML('<div class="eyebrow">AUCTION READER / 경매 탐색</div><h1>나의 조건에 맞는 경매 물건 찾기</h1><p>지역과 가격으로 후보를 좁히고, 지도와 실거래가로 한 번 더 살펴보세요.</p>', elem_id="hero")
        gr.Markdown("**경매 탐색 워크스페이스** · 법원에서 가져온 물건을 비교하고 검토하세요.")
        data_state = gr.State({"rows": [], "payloads": []})
        visible_state = gr.State([])
        active_property = gr.State([])
        comparison_cache = gr.State({})
        with gr.Accordion("검색 범위 · 법원 조회 / 파일 불러오기", open=True):
            with gr.Tab("법원에서 조회"):
                with gr.Row():
                    start = gr.Textbox(label="매각기일 시작 (YYYY-MM-DD)", value=today.isoformat())
                    end = gr.Textbox(label="매각기일 종료 (YYYY-MM-DD)", value=(today + timedelta(days=14)).isoformat())
                    court = gr.Textbox(label="법원 코드", value="B000210", info="서울중앙지방법원 예시. 서울 전체 법원과 다름")
                with gr.Row():
                    pages = gr.State(0)
                    gr.Markdown("**전체 페이지 조회** · 선택한 법원·매각기일의 마지막 페이지까지 가져옵니다. 중단 시 받은 결과를 보존합니다.")
                    page_size = gr.Dropdown(choices=[10, 20, 50], value=50, label="페이지당 건수")
                live_button = gr.Button("법원 사이트 조회", variant="primary")
            with gr.Tab("파일에서 가져오기"):
                files = gr.File(label="원본 JSON / 기존 ZIP / CSV", file_types=[".json", ".zip", ".csv"], file_count="multiple", type="filepath")
                gr.Markdown("원본 JSON이 가장 정확합니다. **예전 CSV에 없는 면적·주소는 복원되지 않습니다.**")
                upload_button = gr.Button("파일 불러오기", variant="primary")
        status = gr.Markdown("**시작하기** · 매각기일과 법원을 확인해 조회하거나, 저장해 둔 파일을 불러오세요.", elem_id="status")

        with gr.Tabs(selected="browse") as workspace_tabs:
            with gr.Tab("물건 탐색", id="browse"):
                with gr.Row():
                    with gr.Column(scale=1, min_width=300, elem_id="filter-panel"):
                        gr.Markdown("### 탐색 조건\n현재 가져온 물건에 적용됩니다. "
                                    "상한을 `0`으로 두면 제한이 없습니다(할인율 상한만 `100%`가 전체).")
                        with gr.Row():
                            region = gr.Dropdown(["전체", "서울특별시", "경기도"], value="전체", label="소재지")
                            keyword = gr.Textbox(label="주소 키워드", placeholder="예: 강남구, 역삼동, 분당구")
                        with gr.Row():
                            building_type = gr.Dropdown(BUILDING_TYPES, value=[], multiselect=True, label="물건 용도", info="선택하지 않으면 전체 · 여러 용도 선택 가능 · 조회된 새 용도도 자동 추가")
                        with gr.Row():
                            min_price = gr.Number(label="최저매각가 최소 (억 원)", value=0, minimum=0, precision=2)
                            max_price = gr.Number(label="최저매각가 최대 (억 원, 0=제한 없음)", value=0, minimum=0, precision=2)
                        with gr.Accordion("상세 조건 · 유찰 / 할인율 / 면적", open=False):
                            with gr.Row():
                                min_fails = gr.Slider(minimum=0, maximum=10, step=1, value=0, label="최소 유찰 횟수")
                                max_fails = gr.Slider(minimum=0, maximum=10, step=1, value=0, label="최대 유찰 횟수 (0=제한 없음)")
                            with gr.Row():
                                min_discount = gr.Slider(minimum=0, maximum=100, step=1, value=0, label="최소 할인율 (%)")
                                max_discount = gr.Slider(minimum=0, maximum=100, step=1, value=100, label="최대 할인율 (%)")
                            with gr.Row():
                                area_unit = gr.Radio(["㎡", "평"], value="㎡", label="면적 단위")
                                min_area = gr.Number(label="최소 면적 (선택 단위)", value=0, minimum=0, precision=2)
                                max_area = gr.Number(label="최대 면적 (0=제한 없음)", value=0, minimum=0, precision=2)
                        with gr.Row():
                            order_by = gr.Dropdown(["기본 순서", "최저매각가 낮은 순", "최저매각가 높은 순", "할인율 높은 순", "면적 넓은 순", "유찰 많은 순", "매각기일 빠른 순"], value="기본 순서", label="정렬")
                            filter_button = gr.Button("필터 적용", variant="primary")
                    with gr.Column(scale=3, min_width=300):
                        summary = gr.HTML(result_dashboard([], {}), elem_id="summary", js_on_load=CARD_EVENTS)
                        with gr.Accordion("전체 데이터 표로 보기", open=False):
                            table = gr.Dataframe(value=pd.DataFrame(columns=RESULT_COLUMNS), interactive=False, label="경매 물건", wrap=True, elem_id="results-table")
                        with gr.Accordion("결과 내보내기 · CSV / 원본 ZIP", open=False):
                            with gr.Row():
                                csv_download = gr.File(label="현재 필터 결과 CSV")
                                json_download = gr.File(label="원본 검색 응답 ZIP")
                        gr.Markdown("### 물건 간단 보기")
                        selected = gr.Dropdown(choices=[], label="사건 선택")
                        detail = gr.Markdown("결과에서 물건을 선택하면 상세 요약이 표시됩니다.", elem_id="detail")
            with gr.Tab("지도 · 실거래가 비교", id="compare"):
                comparison_detail = gr.Markdown("목록에서 상세조회를 누르면 선택한 물건이 표시됩니다.")
                gr.Markdown("## 📍 선택 물건 지도·실거래가 조회")
                gr.Markdown("선택한 **한 건**의 주소를 카카오에서 좌표 변환하고, 해당 구의 국토부 아파트 매매 거래를 조회합니다. "
                            "API 키가 없을 때는 위 경매 검색·필터와 카카오 지도 검색 링크만 사용 가능합니다. "
                            "**상세조회 시 자동으로 조회**하며, 같은 물건·조건의 결과는 이 화면을 사용하는 동안 유지합니다.")
                with gr.Accordion("API 키 설정 · 로컬 .env 자동 사용", open=False):
                    gr.Markdown("프로젝트의 `.env` 또는 환경변수에서 키를 자동으로 읽습니다. 아래 입력칸은 다른 키로 조회할 때만 사용하세요. 저장된 키는 화면에 표시하지 않습니다.")
                    with gr.Row():
                        kakao_key = gr.Textbox(label="Kakao REST API 키 (선택)", type="password", placeholder="Kakao Developers 앱 키")
                        molit_key = gr.Textbox(label="공공데이터포털 Decoding 인증키 (선택)", type="password", placeholder="아파트 매매 실거래가 API 신청 후 발급")
                    gr.Markdown("키는 파일·CSV에 저장하지 않습니다. Colab에서는 환경변수 KAKAO_REST_API_KEY / DATA_GO_KR_SERVICE_KEY로 설정해도 됩니다. "
                                "Gradio 공개 공유 링크는 타인이 접근할 수 있으므로 인증키 및 작업 데이터 보안에 유의하세요.")
                with gr.Row():
                    manual_code = gr.Textbox(label="법정동 지역코드 5자리 (선택)", placeholder="예: 강남구 11680 (카카오 조회 시 자동 탐색)")
                    apt_name = gr.Textbox(label="동일 단지 비교용 아파트명 (선택)", placeholder="예: 래미안대치팰리스")
                    months = gr.Slider(1, 12, value=6, step=1, label="실거래가 조회 기간 (개월)")
                manual_area_ok = gr.Checkbox(label="경매 물건 면적이 전용면적임을 원본 서류에서 확인했음", value=False)
                compare_btn = gr.Button("현재 조건으로 새로 조회", variant="primary")
                compare_info = gr.Markdown("조회 버튼을 누르면 결과가 표시됩니다.")
                compare_map = gr.Image(label="카카오 정적 지도 · 자동 좌표 매칭", interactive=False, type="filepath")
                trade_table = gr.Dataframe(label="국토부 아파트 매매 실거래가 (동일 법정동 기준)", value=pd.DataFrame(columns=TRADE_COLUMNS), interactive=False)
                trade_csv = gr.File(label="실거래가 후보 CSV")
        gr.Markdown("---\n⚠️ 법원 검색결과의 면적은 물건에 따라 건물·토지·전유 등 기준이 다를 수 있습니다. "
                    "입찰 전 법원 상세서류에서 면적·기일변경·권리관계를 반드시 확인하세요. "
                    "사이트가 요청을 차단하면 자동 우회·재시도하지 않습니다.")

        filters = [data_state, region, keyword, min_fails, max_fails, min_price, max_price,
                   min_discount, max_discount, min_area, max_area, area_unit, order_by, building_type]
        displays = [visible_state, summary, table, selected, detail, csv_download, json_download, building_type]
        live_button.click(search_live, [start, end, court, pages, page_size], [data_state, status]).then(render_dashboard, filters, displays)
        upload_button.click(upload_files, [files], [data_state, status]).then(render_dashboard, filters, displays)
        filter_button.click(render_dashboard, filters, displays)
        selected.change(select_detail, [visible_state, selected], [detail])
        comparison_inputs = [comparison_cache, active_property, kakao_key, molit_key, manual_code, apt_name, months, manual_area_ok]
        comparison_outputs = [comparison_cache, compare_info, compare_map, trade_table, trade_csv]
        summary.select(open_card, [visible_state], [active_property, workspace_tabs, comparison_detail],
                       concurrency_limit=1, concurrency_id="comparison").then(
            cached_insights, comparison_inputs, comparison_outputs, concurrency_limit=1, concurrency_id="comparison")
        selected.input(open_property, [visible_state, selected], [active_property, workspace_tabs, comparison_detail],
                       concurrency_limit=1, concurrency_id="comparison").then(
            cached_insights, comparison_inputs, comparison_outputs, concurrency_limit=1, concurrency_id="comparison")
        compare_btn.click(refresh_insights, comparison_inputs, comparison_outputs,
                          concurrency_limit=1, concurrency_id="comparison")
    return app


if __name__ == "__main__":
    build_app().launch(share=False, **(ui_options() if int(gr.__version__.split(".")[0]) >= 6 else {}))
