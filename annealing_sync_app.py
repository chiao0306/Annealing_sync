import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore
import json
import time

# --- 1. 頁面與 Firebase 初始化 ---
st.set_page_config(page_title="退火紀錄表同步工具", page_icon="🔥", layout="centered")
st.title("🔥 退火紀錄表 Firebase 同步工具")
st.caption("建議每月更新一次。系統會自動讀取指定座標的編號，並覆寫至資料庫。")

@st.cache_resource
def get_firebase_db():
    """初始化 Firebase (與你的主程式共用相同的金鑰)"""
    if not firebase_admin._apps:
        # 假設你的 secrets 裡面有 firebase_service_account
        creds_dict = dict(st.secrets.get("firebase_service_account", st.secrets.get("gcp_service_account")))
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = get_firebase_db()

# --- 2. 核心：解析單一分頁的指定座標 ---
def extract_ids_from_sheet(df):
    """
    精準抓取 Excel 指定座標的編號。
    Pandas 索引是從 0 開始：
    C欄=2, G欄=6, K欄=10
    7~21列 = 索引 6~20
    33~47列 = 索引 32~46
    """
    found_ids = set()
    
    # 定義要抓取的區塊：(起始列, 結束列(不含), 欄位索引)
    target_zones = [
        (6, 21, 2),   # C7-C21
        (6, 21, 6),   # G7-G21
        (6, 21, 10),  # K7-K21
        (32, 47, 2),  # C33-C47
        (32, 47, 6),  # G33-G47
        (32, 47, 10)  # K33-K47
    ]
    
    for r_start, r_end, col_idx in target_zones:
        for r_idx in range(r_start, r_end):
            # 防呆：確保座標沒有超出目前這頁的最大範圍
            if r_idx < len(df) and col_idx < len(df.columns):
                val = str(df.iat[r_idx, col_idx]).strip()
                # 排除空值與雜訊
                if val and val.lower() not in ['nan', 'none', '']:
                    found_ids.add(val)
                    
    return list(found_ids)

# --- 3. UI 與 執行邏輯 ---
uploaded_file = st.file_uploader("📂 請上傳最新的退火明細表 (Excel)", type=['xlsx', 'xls', 'xlsm'])

if uploaded_file:
    if st.button("🚀 開始解析並同步至 Firebase", type="primary", use_container_width=True):
        
        with st.status("正在執行同步作業...", expanded=True) as status:
            try:
                # 1. 讀取 Excel (這步最花時間，因為分頁很多)
                st.write("📊 正在讀取 Excel 檔案 (可能需要幾十秒)...")
                start_time = time.time()
                df_dict = pd.read_excel(uploaded_file, sheet_name=None, header=None, dtype=str)
                st.write(f"✅ 讀取完成！共發現 {len(df_dict)} 個分頁。")
                
                # 2. 準備解析與略過清單
                ignore_sheets = ["工作表1", "NG電爐"]
                upload_data = {}
                
                st.write("🔍 正在萃取各分頁編號...")
                progress_bar = st.progress(0)
                total_sheets = len(df_dict)
                
                for idx, (sheet_name, df) in enumerate(df_dict.items()):
                    progress_bar.progress((idx + 1) / total_sheets)
                    
                    if sheet_name in ignore_sheets:
                        continue
                        
                    # 只有該分頁名稱是數字 (例如 2116) 才處理，避免抓到奇怪的隱藏分頁
                    if str(sheet_name).strip().isdigit():
                        extracted_ids = extract_ids_from_sheet(df)
                        if extracted_ids: # 只有裡面有資料的才存
                            upload_data[str(sheet_name)] = extracted_ids
                            
                st.write(f"✅ 萃取完成！共計 {len(upload_data)} 個有效分頁準備寫入。")
                
                # 3. 批次寫入 Firebase (Firestore 限制每次 Batch 最多 500 筆操作)
                st.write("🔥 正在同步至 Firebase...")
                collection_ref = db.collection("annealing_records")
                
                batches = []
                current_batch = db.batch()
                operation_count = 0
                
                for sheet_name, ids_list in upload_data.items():
                    doc_ref = collection_ref.document(sheet_name)
                    # 存入的資料格式：{ "ids": ["28L48", "M30S242", ...], "last_updated": 伺服器時間 }
                    current_batch.set(doc_ref, {
                        "ids": ids_list,
                        "last_updated": firestore.SERVER_TIMESTAMP
                    })
                    operation_count += 1
                    
                    # 滿 400 筆就送出一次 (留一點安全邊際)
                    if operation_count >= 400:
                        batches.append(current_batch)
                        current_batch = db.batch()
                        operation_count = 0
                        
                # 把剩下的也加進去
                if operation_count > 0:
                    batches.append(current_batch)
                    
                # 執行寫入
                for i, b in enumerate(batches):
                    b.commit()
                    st.write(f"  - 批次寫入 {i+1}/{len(batches)} 完成")
                
                end_time = time.time()
                status.update(label=f"🎉 同步大成功！耗時 {end_time - start_time:.1f} 秒", state="complete", expanded=False)
                
                st.success(f"成功更新了 {len(upload_data)} 個退火分頁至 Firebase！")
                st.balloons()
                
            except Exception as e:
                status.update(label="🚨 發生錯誤", state="error")
                st.error(f"詳細錯誤訊息：{e}")
