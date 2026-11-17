#!/usr/bin/env bash
# git 認證輔助：執行時才從環境變數讀 GITHUB_TOKEN，不寫入任何檔案或 remote URL。
case "$1" in
  Username*) echo "web-twstockai" ;;
  *) printf '%s\n' "${GITHUB_TOKEN:?GITHUB_TOKEN 未設定}" ;;
esac
