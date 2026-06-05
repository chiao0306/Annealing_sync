import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore

st.set_page_config(page_title="退火紀錄表同步", page_icon="🔥")
st.title("🔥 退火紀錄增量同步工具")

@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        # 這樣寫，無論你的 key 叫 firebase_service_account 還是 gcp_service_account 都抓得到
        creds_dict = dict(st.secrets.get("firebase_service_account", st.secrets.get("gcp_service_account")))
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = get_db()

def extract_ids_from_sheet(df):
    found_ids = set()
    zones = [(6, 21, 2), (6, 21, 6), (6, 21, 10), (32, 47, 2), (32, 47, 6), (32, 47, 10)]
    for rs, re, c in zones:
        for r in range(rs, re):
            if r < len(df) and c < len(df.columns):
                val = str(df.iat[r, c]).strip().upper() # 一律轉大寫
                
                # 1. 排除空值與常見的無效字眼 (包含 N/A)
                if val and val not in ['NAN', 'NONE', '', 'N/A', 'NA', '-']:
                    
                    # 2. 🔥 終極防呆：把斜線替換成底線，絕對禁止斜線進入 Firebase ID
                    safe_rid = val.replace("/", "_").replace("\\", "_")
                    
                    if safe_rid:
                        found_ids.add(safe_rid)
                        
    return list(found_ids)

uploaded_file = st.file_uploader("📂 上傳退火明細表 (Excel)", type=['xlsx', 'xlsm'])

if uploaded_file:
    st.info("資料解析中，請先確認下方預覽內容...")
    
    # 1. 取得上次更新進度
    meta_ref = db.collection("system_meta").document("annealing_sync")
    meta_doc = meta_ref.get()
    last_sheet_num = meta_doc.to_dict().get("last_sheet", 0) if meta_doc.exists else 0
    
    st.write(f"📌 上次同步至分頁：{last_sheet_num}，系統將只處理大於此數字的新分頁。")
    
    # 2. 讀取 Excel 並先進行解析 (Dry Run)
    df_dict = pd.read_excel(uploaded_file, sheet_name=None, header=None, dtype=str)
    
    preview_data = []      # 用來顯示在畫面上給你看的表格資料
    pending_writes = {}    # 實際準備寫入的資料暫存: { sheet_int: [id1, id2, ...] }
    max_sheet_processed = last_sheet_num
    
    for sheet_name, df in df_dict.items():
        # 確保分頁名稱是數字
        if not str(sheet_name).strip().isdigit(): continue
        
        sheet_int = int(str(sheet_name).strip())
        
        # 增量判斷：只處理比上次更新還要大的分頁
        if sheet_int <= last_sheet_num: continue
        
        extracted_ids = extract_ids_from_sheet(df)
        if extracted_ids:
            pending_writes[sheet_int] = extracted_ids
            # 建立預覽用的清單
            preview_data.append({
                "新增分頁": sheet_int,
                "抓取筆數": len(extracted_ids),
                "編號預覽 (前5筆)": ", ".join(extracted_ids[:5]) + ("..." if len(extracted_ids) > 5 else "")
            })
            
            if sheet_int > max_sheet_processed:
                max_sheet_processed = sheet_int

    # 3. 顯示預覽畫面
    if preview_data:
        st.write("### 👀 寫入前預覽")
        preview_df = pd.DataFrame(preview_data)
        st.dataframe(preview_df, use_container_width=True)
        
        total_updates = sum(d['抓取筆數'] for d in preview_data)
        st.warning(f"⚠️ 確認無誤後，將會把這 {total_updates} 筆更新寫入 Firebase。")
        
        # 4. 正式寫入按鈕
        if st.button("🚀 確認無誤，執行增量同步", type="primary"):
            with st.status("寫入 Firebase 中...", expanded=True) as status:
                batch = db.batch()
                updates_count = 0
                
                for sheet_int, ids in pending_writes.items():
                    for rid in ids:
                        doc_ref = db.collection("roll_annealing_index").document(rid)
                        batch.set(doc_ref, {"sheets": firestore.ArrayUnion([sheet_int])}, merge=True)
                        updates_count += 1
                        
                        if updates_count >= 400: # Firebase batch 限制
                            batch.commit()
                            batch = db.batch()
                            updates_count = 0
                            
                if updates_count > 0:
                    batch.commit()
                    
                # 更新進度指標
                meta_ref.set({"last_sheet": max_sheet_processed}, merge=True)
                
                status.update(label=f"✅ 完成！最新進度更新至 {max_sheet_processed} 頁", state="complete")
    else:
        st.success("🎉 檔案解析完畢，目前沒有需要新增的分頁資料 (皆已同步過)。")
        
st.divider()
st.header("🔍 驗證與查詢：反查退火紀錄")
st.write("輸入退火編號，即可快速確認該編號被記錄在哪些分頁中。")

# 建立兩欄排版，讓畫面緊湊一點
col1, col2 = st.columns([3, 1])
with col1:
    search_id = st.text_input("輸入退火編號 (例如：A123_45)：", placeholder="請輸入編號...").strip().upper()

if search_id:
    # 保持與寫入時完全相同的防呆替換邏輯
    safe_search_id = search_id.replace("/", "_").replace("\\", "_")
    
    with st.spinner("查詢 Firebase 中..."):
        # 直接拿這個 ID 去對應的 collection 抓文件
        doc_ref = db.collection("roll_annealing_index").document(safe_search_id)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            sheets_array = data.get("sheets", [])
            # 將分頁數字由小到大排序，閱讀起來更直覺
            sheets_array.sort()
            
            st.success(f"✅ 找到了！編號 **{safe_search_id}** 存在於資料庫中。")
            st.info(f"📌 出現分頁： **{', '.join(map(str, sheets_array))}**")
        else:
            st.warning(f"⚠️ 在 Firebase 中找不到編號 **{safe_search_id}** 的紀錄。")
            st.markdown("""
            *可能原因：*
            * 該編號尚未被上傳同步。
            * 輸入時有錯字或漏字。
            * 在原本的 Excel 中，該欄位為空值或被判定為無效字眼。
            """)