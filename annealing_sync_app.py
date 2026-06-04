import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore

st.set_page_config(page_title="退火紀錄表同步", page_icon="🔥")
st.title("🔥 退火紀錄增量同步工具")

@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        cred = credentials.Certificate(dict(st.secrets["gcp_service_account"]))
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = get_db()

def extract_ids_from_sheet(df):
    found_ids = set()
    zones = [(6, 21, 2), (6, 21, 6), (6, 21, 10), (32, 47, 2), (32, 47, 6), (32, 47, 10)]
    for rs, re, c in zones:
        for r in range(rs, re):
            if r < len(df) and c < len(df.columns):
                val = str(df.iat[r, c]).strip()
                if val and val.lower() not in ['nan', 'none', '']:
                    found_ids.add(val.upper()) # 轉大寫防呆
    return list(found_ids)

uploaded_file = st.file_uploader("📂 上傳退火明細表 (Excel)", type=['xlsx'])

if uploaded_file and st.button("🚀 執行增量同步", type="primary"):
    with st.status("執行中...", expanded=True) as status:
        # 1. 取得上次更新進度
        meta_ref = db.collection("system_meta").document("annealing_sync")
        meta_doc = meta_ref.get()
        last_sheet_num = meta_doc.to_dict().get("last_sheet", 0) if meta_doc.exists else 0
        
        st.write(f"📌 上次同步至分頁：{last_sheet_num}，將只處理大於此數字的新分頁。")
        
        # 2. 讀取 Excel
        df_dict = pd.read_excel(uploaded_file, sheet_name=None, header=None, dtype=str)
        
        max_sheet_processed = last_sheet_num
        updates_count = 0
        batch = db.batch()
        
        for sheet_name, df in df_dict.items():
            # 確保分頁名稱是數字
            if not str(sheet_name).strip().isdigit(): continue
            
            sheet_int = int(str(sheet_name).strip())
            
            # 🔥 增量判斷：只處理比上次更新還要大的分頁
            if sheet_int <= last_sheet_num: continue
            
            extracted_ids = extract_ids_from_sheet(df)
            if extracted_ids:
                # 寫入反向索引 (這個編號出現在這個分頁)
                for rid in extracted_ids:
                    doc_ref = db.collection("roll_annealing_index").document(rid)
                    # 使用 ArrayUnion，如果陣列裡沒有這個分頁就加進去，有的話就不動
                    batch.set(doc_ref, {"sheets": firestore.ArrayUnion([sheet_int])}, merge=True)
                    updates_count += 1
                    
                    if updates_count >= 400: # Firebase batch 限制
                        batch.commit()
                        batch = db.batch()
                        updates_count = 0
                        
            if sheet_int > max_sheet_processed:
                max_sheet_processed = sheet_int

        if updates_count > 0:
            batch.commit()
            
        # 3. 更新進度指標
        meta_ref.set({"last_sheet": max_sheet_processed}, merge=True)
        
        status.update(label=f"✅ 完成！最新進度更新至 {max_sheet_processed} 頁", state="complete")