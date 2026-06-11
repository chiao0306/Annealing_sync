import streamlit as st
import pandas as pd
import firebase_admin
from firebase_admin import credentials, firestore

st.set_page_config(page_title="退火紀錄表同步", page_icon="🔥")

st.markdown("### 🔥 退火紀錄增量同步工具")

@st.cache_resource
def get_db():
    if not firebase_admin._apps:
        creds_dict = dict(st.secrets.get("firebase_service_account", st.secrets.get("gcp_service_account")))
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = get_db()

# --- 全域取得目前的系統進度 ---
meta_ref = db.collection("system_meta").document("annealing_sync")
meta_doc = meta_ref.get()
last_sheet_num = meta_doc.to_dict().get("last_sheet", 0) if meta_doc.exists else 0

def extract_ids_from_sheet(df):
    found_ids = set()
    zones = [(6, 21, 2), (6, 21, 6), (6, 21, 10), (32, 47, 2), (32, 47, 6), (32, 47, 10)]
    for rs, re, c in zones:
        for r in range(rs, re):
            if r < len(df) and c < len(df.columns):
                val = str(df.iat[r, c]).strip().upper()
                
                if val and val not in ['NAN', 'NONE', '', 'N/A', 'NA', '-']:
                    safe_rid = val.replace("/", "_").replace("\\", "_")
                    if safe_rid:
                        found_ids.add(safe_rid)
    return list(found_ids)

# ==========================================
# 上傳與匯入區塊
# ==========================================
uploaded_file = st.file_uploader("📂 上傳退火明細表 (Excel)", type=['xlsx', 'xlsm'])

# 直接顯示在檔案上傳區塊的下方，無論有沒有上傳檔案都看得到
st.info(f"📌 目前系統最新進度 (last_sheet)：**{last_sheet_num}**")

if uploaded_file:
    st.write("資料解析中，請先確認下方預覽內容...")
    
    df_dict = pd.read_excel(uploaded_file, sheet_name=None, header=None, dtype=str)
    
    preview_data = []      
    pending_writes = {}    
    max_sheet_processed = last_sheet_num
    
    for sheet_name, df in df_dict.items():
        if not str(sheet_name).strip().isdigit(): continue
        
        sheet_int = int(str(sheet_name).strip())
        # 系統依然會用全域的 last_sheet_num 來做防呆判斷
        if sheet_int <= last_sheet_num: continue
        
        extracted_ids = extract_ids_from_sheet(df)
        if extracted_ids:
            pending_writes[sheet_int] = extracted_ids
            preview_data.append({
                "新增分頁": sheet_int,
                "抓取筆數": len(extracted_ids),
                "編號預覽 (前5筆)": ", ".join(extracted_ids[:5]) + ("..." if len(extracted_ids) > 5 else "")
            })
            if sheet_int > max_sheet_processed:
                max_sheet_processed = sheet_int

    if preview_data:
        st.write("### 👀 寫入前預覽")
        preview_df = pd.DataFrame(preview_data)
        st.dataframe(preview_df, use_container_width=True)
        
        total_updates = sum(d['抓取筆數'] for d in preview_data)
        st.warning(f"⚠️ 確認無誤後，將會把這 {total_updates} 筆更新寫入 Firebase。")
        
        if st.button("🚀 確認無誤，執行增量同步", type="primary"):
            with st.status("寫入 Firebase 中...", expanded=True) as status:
                batch = db.batch()
                updates_count = 0
                
                for sheet_int, ids in pending_writes.items():
                    for rid in ids:
                        doc_ref = db.collection("roll_annealing_index").document(rid)
                        batch.set(doc_ref, {"sheets": firestore.ArrayUnion([sheet_int])}, merge=True)
                        updates_count += 1
                        
                        if updates_count >= 400:
                            batch.commit()
                            batch = db.batch()
                            updates_count = 0
                            
                if updates_count > 0:
                    batch.commit()
                    
                meta_ref.set({"last_sheet": max_sheet_processed}, merge=True)
                status.update(label=f"✅ 完成！最新進度更新至 {max_sheet_processed} 頁", state="complete")
                st.rerun() 
    else:
        st.success("🎉 檔案解析完畢，目前沒有需要新增的分頁資料 (皆已同步過)。")

# ==========================================
# 驗證與查詢區塊
# ==========================================
st.divider()
st.markdown("#### 🔍 驗證與查詢：反查退火紀錄")
st.write("輸入退火編號，即可快速確認該編號被記錄在哪些分頁中。")

col1, col2 = st.columns([3, 1])
with col1:
    search_id = st.text_input("輸入退火編號 (例如：A123_45)：", placeholder="請輸入編號...").strip().upper()

if search_id:
    safe_search_id = search_id.replace("/", "_").replace("\\", "_")
    with st.spinner("查詢 Firebase 中..."):
        doc_ref = db.collection("roll_annealing_index").document(safe_search_id)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            sheets_array = data.get("sheets", [])
            sheets_array.sort()
            st.success(f"✅ 找到了！編號 **{safe_search_id}** 存在於資料庫中。")
            st.info(f"📌 出現分頁： **{', '.join(map(str, sheets_array))}**")
        else:
            st.warning(f"⚠️ 在 Firebase 中找不到編號 **{safe_search_id}** 的紀錄。")

# ==========================================
# 系統維護：還原與刪除區塊
# ==========================================

# 1. 先定義確認彈出視窗的 UI 與執行邏輯
@st.dialog("⚠️ 刪除最終確認")
def confirm_deletion_dialog(start_sheet, end_sheet, target_last_sheet, bad_sheets):
    st.error(f"即將從所有紀錄中移除分頁：**{start_sheet} 到 {end_sheet}**")
    st.warning(f"系統進度將自動退回至：**{target_last_sheet}**")
    st.markdown("🚨 **注意：** 刪除後若該退火編號無其他分頁紀錄，將被徹底移除。此操作無法復原！")
    
    if st.button("🔴 我已確認，執行刪除作業", type="primary", use_container_width=True):
        with st.spinner("掃描資料庫比對與刪除中，這可能需要一點時間..."):
            docs = db.collection("roll_annealing_index").stream()
            
            batch = db.batch()
            updates_count = 0
            deleted_count = 0
            modified_count = 0
            
            for doc in docs:
                data = doc.to_dict()
                current_sheets = data.get("sheets", [])
                
                if any(sheet in current_sheets for sheet in bad_sheets):
                    new_sheets = [s for s in current_sheets if s not in bad_sheets]
                    
                    if not new_sheets:
                        batch.delete(doc.reference)
                        deleted_count += 1
                    else:
                        batch.update(doc.reference, {"sheets": firestore.ArrayRemove(bad_sheets)})
                        modified_count += 1
                        
                    updates_count += 1
                    
                    if updates_count >= 400:
                        batch.commit()
                        batch = db.batch()
                        updates_count = 0

            if updates_count > 0:
                batch.commit()
                
            # 寫入系統自動計算好的退回進度
            meta_ref = db.collection("system_meta").document("annealing_sync")
            meta_ref.set({"last_sheet": target_last_sheet}, merge=True)
            
        # 執行成功後，將結果存入 session_state 以便重整後顯示，然後重整畫面
        st.session_state['delete_msg'] = f"✅ 成功刪除 {deleted_count} 筆空文件，修改 {modified_count} 筆陣列。進度已退回 {target_last_sheet}。"
        st.rerun()

# 2. 實際的頁面介面
st.divider()
st.markdown("#### 🛠️ 系統維護：刪除特定分頁資料")

# 檢查剛剛是否剛執行完刪除，顯示成功訊息
if 'delete_msg' in st.session_state:
    st.success(st.session_state['delete_msg'])
    st.balloons()
    del st.session_state['delete_msg'] # 顯示過就清除

with st.expander("展開執行刪除作業"):
    st.warning("⚠️ **危險操作**：將從資料庫中抽除指定分頁，並自動將進度退回至刪除區間的前一頁。")
    st.info(f"📌 目前系統最新進度 (last_sheet)：**{last_sheet_num}**")
    
    rollback_range = st.text_input("刪除分頁區間 (例如 2100-2116，單頁請填 2116-2116)：", placeholder="開始-結束")
    admin_password = st.text_input("請輸入管理員密碼以解鎖：", type="password")
    
    # --- 密碼狀態檢查與防呆提示 ---
    correct_password = st.secrets.get("admin_password", None)
    
    if correct_password is None:
        st.error("❌ 系統錯誤：無法讀取密碼！請確認已在 `.streamlit/secrets.toml` 或 Streamlit 雲端後台設定好 `admin_password`。")
    elif admin_password and admin_password != correct_password:
        st.error("❌ 密碼錯誤")
    elif admin_password == correct_password:
        st.success("✅ 密碼正確，按鈕已解鎖")
        
    btn_disabled = (not correct_password) or (admin_password != correct_password) or (not rollback_range)
    
    # 3. 觸發彈出視窗的按鈕
    if st.button("🚨 執行刪除", disabled=btn_disabled, type="primary"):
        try:
            val1, val2 = rollback_range.split("-")
            
            start_sheet = min(int(val1.strip()), int(val2.strip()))
            end_sheet = max(int(val1.strip()), int(val2.strip()))
            target_last_sheet = max(0, start_sheet - 1)
            bad_sheets = list(range(start_sheet, end_sheet + 1))
            
            # 呼叫上面定義的 dialog 視窗
            confirm_deletion_dialog(start_sheet, end_sheet, target_last_sheet, bad_sheets)
            
        except ValueError:
            st.error("❌ 格式錯誤：請確保輸入格式為 '數字-數字'，例如 2116-2116。")
        except Exception as e:
            st.error(f"❌ 發生未預期的錯誤：{e}")