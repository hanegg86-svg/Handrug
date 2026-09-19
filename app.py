import re
import time
import math
import json
import io
import base64
import subprocess
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from playwright.sync_api import sync_playwright

# ตรวจสอบและติดตั้งเบราว์เซอร์ Chromium ของ Playwright อัตโนมัติเมื่อเปิดเซิร์ฟเวอร์ครั้งแรก
@st.cache_resource
def install_playwright_browsers():
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
    except Exception:
        pass

install_playwright_browsers()

# ตั้งค่าหน้าเว็บ
st.set_page_config(page_title="ระบบสืบค้นข้อมูลยา อย.", layout="wide")

st.title("💊 ระบบสืบค้นข้อมูลยา อย. (ค้นหาหลายรายการ)")
st.caption("ดึงข้อมูลจากสำนักงานคณะกรรมการอาหารและยา พร้อมบันทึกผลลัพธ์ลง IndexedDB อัตโนมัติ")

# กำหนดตัวแปร Session State สำหรับจดจำผลลัพธ์ไม่ให้ปุ่มดาวน์โหลดหาย
if "df_final" not in st.session_state:
    st.session_state["df_final"] = None
if "excel_bytes" not in st.session_state:
    st.session_state["excel_bytes"] = None
if "b64_excel" not in st.session_state:
    st.session_state["b64_excel"] = None

# 1. ฟังก์ชันสกัดข้อมูลด้วย Playwright แบบ Native Python
def scrape_drug_data(keywords, progress_bar, status_text):
    base_url = "https://pertento.fda.moph.go.th/FDA_SEARCH_DRUG/SEARCH_DRUG/FRM_SEARCH_DRUG.aspx"
    all_extracted_records = []
    final_headers = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        total_keywords = len(keywords)
        for kw_index, kw in enumerate(keywords, start=1):
            status_text.text(f"🔍 [{kw_index}/{total_keywords}] กำลังค้นหา: {kw}...")
            progress_bar.progress(kw_index / total_keywords)

            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=60000)

                # ระบุช่องกรอกชื่อสารสำคัญ
                search_input = page.locator("tr:has-text('สารสำคัญ') input[type='text']").first
                if search_input.count() == 0:
                    search_input = page.locator("xpath=//*[contains(text(), 'สารสำคัญ')]/following::input[@type='text'][1]").first
                search_input.fill(kw)

                # กดปุ่มค้นหา
                search_button = page.locator("input[type='submit'][value*='ค้นหา'], input[id*='btn_search'], button:has-text('ค้นหา')").first
                search_button.click()

                # รอให้ตารางปรากฏ
                page.wait_for_timeout(3500)
                table = page.locator("table[id*='Grid']").first
                try:
                    table.wait_for(state="visible", timeout=20000)
                except Exception:
                    st.warning(f"❌ ไม่พบผลลัพธ์ หรือเซิร์ฟเวอร์ตอบสนองนานเกินกำหนดสำหรับ: {kw}")
                    continue

                # ตรวจหาตัวเลขเป้าหมายจริง (เช่น 'จำนวนค้นหาทั้งหมด 60 รายการ')
                body_content = page.locator("body").inner_text()
                target_total_items = 0
                match_total = re.search(r"จำนวนค้นหาทั้งหมด\s*(\d+)", body_content)
                if match_total:
                    target_total_items = int(match_total.group(1))
                else:
                    match_pager = re.search(r"in\s+(\d+)\s+pages?", body_content, re.IGNORECASE)
                    if match_pager:
                        target_total_items = int(match_pager.group(1)) * 20
                    else:
                        target_total_items = 60

                if target_total_items == 0:
                    st.warning(f"ไม่พบรายการข้อมูลสำหรับ: {kw}")
                    continue

                total_pages = math.ceil(target_total_items / 20)

                # สกัดชื่อหัวตารางภาษาไทยที่มองเห็นจริงบนหน้าจอ (ตัดคอลัมน์ซ่อน IDA, lcnno, pvncd ออก)
                th_elements = page.locator("table[id*='Grid'] thead tr.rgHeader th, table[id*='Grid'] th.rgHeader").all()
                visible_headers = []
                for th in th_elements:
                    if th.is_visible():
                        txt = th.inner_text().strip()
                        if txt and txt not in visible_headers and "\n" not in txt:
                            visible_headers.append(txt)

                if not visible_headers:
                    visible_headers = ["ลำดับ", "เลขทะเบียนตำรับยา", "ชื่อทางการค้า (ภาษาไทย)", "ชื่อทางการค้า (ภาษาอังกฤษ)", "ชื่อผู้รับอนุญาต"]

                final_headers = ["คำค้นหา (สารสำคัญ)"] + visible_headers

                kw_records = []

                # วนลูปดึงข้อมูลตามจำนวนหน้าจริง
                for current_page in range(1, total_pages + 1):
                    expected_start_seq = (current_page - 1) * 20 + 1
                    status_text.text(f"🔍 [{kw_index}/{total_keywords}] {kw} - กำลังดึงหน้า {current_page}/{total_pages} (ลำดับที่ {expected_start_seq} ขึ้นไป)...")

                    if current_page > 1:
                        page_btn = page.locator(f"xpath=//div[contains(@class, 'rgNumPart')]//a[normalize-space()='{current_page}']").first
                        if page_btn.count() > 0:
                            page_btn.click()
                        else:
                            next_btn = page.locator(".rgPageNext, [title='Next Page'], input.rgPageNext").first
                            if next_btn.count() > 0:
                                next_btn.click()
                            else:
                                break

                        # รอให้ตารางอัปเดตและแสดงตัวเลขลำดับใหม่ประจำหน้านั้น
                        for _ in range(16):
                            page.wait_for_timeout(500)
                            first_td = page.locator("table[id*='Grid'] tbody tr.rgRow td").all()
                            visible_first = [td for td in first_td if td.is_visible()]
                            if visible_first and visible_first[0].inner_text().strip() == str(expected_start_seq):
                                break

                    # สกัดเฉพาะแถวตัวยาจริงจาก tbody เท่านั้น (ไม่นับหัวตารางหรือแถวกรอง)
                    data_rows = page.locator("table[id*='Grid'] tbody tr.rgRow, table[id*='Grid'] tbody tr.rgAltRow").all()
                    for r in data_rows:
                        visible_cells = [td for td in r.locator("td").all() if td.is_visible()]
                        if len(visible_cells) >= 4:
                            row_vals = [td.inner_text().strip() for td in visible_cells]
                            if re.match(r"^\d+$", row_vals[0]):
                                full_row = [kw] + row_vals
                                if full_row not in kw_records:
                                    kw_records.append(full_row)

                    if len(kw_records) >= target_total_items:
                        break

                all_extracted_records.extend(kw_records)

            except Exception as ex:
                st.warning(f"เกิดข้อผิดพลาดในการสืบค้น {kw}: {ex}")

            page.wait_for_timeout(1500)

        browser.close()

    if all_extracted_records:
        num_cols = max(len(r) for r in all_extracted_records)
        if len(final_headers) < num_cols:
            final_headers = final_headers + [f"ข้อมูล_{i+1}" for i in range(len(final_headers), num_cols)]
        else:
            final_headers = final_headers[:num_cols]
        return pd.DataFrame(all_extracted_records, columns=final_headers)
    return pd.DataFrame()

# 2. ฟังก์ชันจัดเก็บข้อมูลลง IndexedDB
def save_to_indexeddb_js(dataframe):
    records_json = dataframe.to_json(orient="records", force_ascii=False)
    js_code = f"""
    <script>
    (function() {{
        const dbName = "DrugScraperDB";
        const storeName = "scraped_results";
        const data = {records_json};

        const request = window.indexedDB.open(dbName, 1);

        request.onupgradeneeded = function(event) {{
            const db = event.target.result;
            if (!db.objectStoreNames.contains(storeName)) {{
                db.createObjectStore(storeName, {{ keyPath: "id", autoIncrement: true }});
            }}
        }};

        request.onsuccess = function(event) {{
            const db = event.target.result;
            const tx = db.transaction(storeName, "readwrite");
            const store = tx.objectStore(storeName);
            data.forEach(item => {{
                store.add({{
                    timestamp: new Date().toISOString(),
                    data: item
                }});
            }});
            console.log("บันทึกข้อมูลลง IndexedDB เรียบร้อยแล้ว จำนวน: " + data.length + " รายการ");
        }};

        request.onerror = function(event) {{
            console.error("IndexedDB Error: ", event.target.error);
        }};
    }})();
    </script>
    <div style="background-color: #e8f5e9; border: 1px solid #4caf50; border-radius: 6px; padding: 10px; margin: 10px 0;">
        <span style="color: #2e7d32; font-weight: bold;">💾 ซิงค์ข้อมูลลง IndexedDB ของอุปกรณ์นี้เรียบร้อยแล้ว</span>
    </div>
    """
    components.html(js_code, height=65)

# 3. ส่วนควบคุมหน้าจอ UI
input_option = st.radio("เลือกรูปแบบการป้อนข้อมูล:", ["พิมพ์ชื่อยาเอง (ทีละตัวหรือหลายตัว)", "อัปโหลดไฟล์ Excel / CSV"])
search_list = []

if input_option == "พิมพ์ชื่อยาเอง (ทีละตัวหรือหลายตัว)":
    text_input = st.text_area(
        "กรอกชื่อสารสำคัญ (คั่นด้วยเครื่องหมายจุลภาค , หรือขึ้นบรรทัดใหม่):",
        value="Meropenem",
        height=100
    )
    if text_input:
        raw_items = re.split(r"[\n,]+", text_input)
        search_list = [item.strip() for item in raw_items if item.strip()]
        st.write(f"รายการที่จะค้นหา ({len(search_list)} รายการ):", search_list)

else:
    uploaded_file = st.file_uploader("อัปโหลดไฟล์ Excel (.xlsx) หรือ CSV ที่มีรายชื่อยา", type=["xlsx", "csv"])
    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith(".csv"):
                df_upload = pd.read_csv(uploaded_file)
            else:
                df_upload = pd.read_excel(uploaded_file)

            st.write("ตัวอย่างข้อมูลจากไฟล์ที่อัปโหลด:")
            st.dataframe(df_upload.head(3))

            target_col = st.selectbox("เลือกคอลัมน์ที่มีชื่อสารสำคัญ/ชื่อยา:", df_upload.columns)
            search_list = df_upload[target_col].dropna().astype(str).str.strip().unique().tolist()
            st.write(f"ตรวจพบรายชื่อยา ({len(search_list)} รายการ):", search_list)
        except Exception as e:
            st.error(f"ไม่สามารถอ่านไฟล์ได้: {e}")

# ปุ่มเริ่มค้นหา
st.divider()
if st.button("🚀 เริ่มดึงข้อมูลทั้งหมด", type="primary"):
    if not search_list:
        st.warning("กรุณาระบุชื่อยาอย่างน้อย 1 รายการก่อนเริ่มค้นหา")
    else:
        # รีเซ็ตผลลัพธ์เดิมออกเมื่อกดค้นหาใหม่
        st.session_state["df_final"] = None
        st.session_state["excel_bytes"] = None
        st.session_state["b64_excel"] = None

        prog_bar = st.progress(0.0)
        status_lbl = st.empty()

        df_result = scrape_drug_data(search_list, prog_bar, status_lbl)

        prog_bar.progress(1.0)
        status_lbl.text("✅ ทำการสืบค้นข้อมูลยาครบทุกรายการเรียบร้อยแล้ว!")

        if not df_result.empty:
            # เก็บผลลัพธ์ลง Session State
            st.session_state["df_final"] = df_result

            # บันทึกลง IndexedDB
            save_to_indexeddb_js(df_result)

            # เตรียมไฟล์ Excel เก็บไว้ใน Session State
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df_result.to_excel(writer, index=False, sheet_name="FDA_Drug_Results")
            excel_bytes = buffer.getvalue()
            st.session_state["excel_bytes"] = excel_bytes
            st.session_state["b64_excel"] = base64.b64encode(excel_bytes).decode()
        else:
            st.error("ไม่พบรายการข้อมูลยาใดๆ จากคำค้นหาที่ระบุ")

# 4. ส่วนแสดงผลลัพธ์และปุ่มดาวน์โหลด (แสดงค้างไว้ตลอดจนกว่าจะกดค้นหาใหม่)
if st.session_state["df_final"] is not None and not st.session_state["df_final"].empty:
    df_display = st.session_state["df_final"]
    st.success(f"ดึงข้อมูลสำเร็จรวมทั้งหมด {len(df_display)} รายการ")
    st.write("ตัวอย่างข้อมูลผลลัพธ์ (10 รายการแรก):")
    st.table(df_display.head(10))

    st.download_button(
        label="📥 ดาวน์โหลดไฟล์ Excel ผลลัพธ์รวม (.xlsx)",
        data=st.session_state["excel_bytes"],
        file_name="fda_combined_drug_results.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    download_html = f'<p style="margin-top: 5px;"><a href="data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{st.session_state["b64_excel"]}" download="fda_combined_drug_results.xlsx" style="color: #1976d2; font-weight: bold; text-decoration: underline;">👉 หากปุ่มด้านบนกดไม่ติด ให้แตะดาวน์โหลดไฟล์ Excel โดยตรงที่นี่</a></p>'
    st.markdown(download_html, unsafe_allow_html=True)
