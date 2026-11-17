# src 分支：雲端更新用的程式碼

此分支只放 GitHub Actions 雲端更新（`update.yml`，位於 main 分支）要用的程式碼；main 分支放自動產生的靜態網站。
資料庫不在 repo 內，放在 Release 資產 `db-latest`（上一份 `db-prev`）。內容全部來自證交所、櫃買中心、公開資訊觀測站。
由電腦上的 `tools/cloud/sync_src.sh` 同步；請勿直接在此分支手改。僅供研究參考，不構成投資建議。
